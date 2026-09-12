#!/usr/bin/env python3
"""Keep the FiiO BT11's aptX Adaptive mode matched to what is being played.

The BT11 is a USB audio device, so the host always hands it plain PCM; which
aptX Adaptive flavour the *link* uses is a device setting.  This program
watches the audio that is actually being sent to the BT11 and picks:

  near-lossless content at 44.1 kHz  -> 19  aptX Lossless
  near-lossless content at other rate -> 3  High Quality
  lossy content                       -> 2  Low Latency

It is meant to be started by a systemd path unit that fires when the BT11 is
plugged in, and to exit as soon as the device goes away, so the unit set is
"asleep" whenever the dongle is absent.

Only the Python standard library is used.  Audio is read straight from
``pw-record`` as raw s16le on stdout, and analysed with two cascaded biquad
bands: a mid reference band (1-6 kHz) and a high band starting at 0.85 of
Nyquist, which is where lossy encoders have already run out of spectrum.

usage:
  bt11-auto-mode run                 service loop (default)
  bt11-auto-mode once [--apply]      one decision, print it
  bt11-auto-mode analyse FILE.wav    measure one wav file
  bt11-auto-mode self-test           check the decisions and the DSP
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time

# --- BT11 constants ---------------------------------------------------------

VENDOR_ID = "0a12"
PRODUCT_ID = "4007"
HIDRAW_LINK = "/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw"
SINK_NAME = "BT11"

APTX_LOW_LATENCY = 2
APTX_HIGH_QUALITY = 3
APTX_LOSSLESS = 19
MODE_NAMES = {
    APTX_LOW_LATENCY: "Low Latency",
    APTX_HIGH_QUALITY: "High Quality",
    APTX_LOSSLESS: "aptX Lossless",
}

# --- analysis constants -----------------------------------------------------

CAPTURE_RATE = 48000          # what we ask pw-record for, in Hz
CAPTURE_CHANNELS = 2
MID_BAND = (1000.0, 6000.0)   # reference band for the "is anything playing" test
# Lossy encoders put a brick wall at the top of the band; lossless content does
# not.  So the detector looks for a *step* between neighbouring narrow bands,
# which is independent of absolute level -- real music rolls off by tens of dB
# across the top octave without ever stepping.  Measured on this machine
# (2026-09-12, see VERIFICATION.md): a lossless stream steps by at most 5 dB
# between 2 kHz bands, while MP3 128k steps 63 dB, AAC 128k 66 dB and MP3 320k
# 67 dB at its 20.3 kHz wall.
CLIFF_START_KHZ = 10.0
CLIFF_BAND_KHZ = 1.0
CLIFF_MIN_STEP_DB = 35.0      # at or above this a step can only be a codec wall
# ...and the band above it must be at the digital floor: a codec zeroes the
# spectrum above its cutoff (measured -75 to -77 dB relative to the mid band),
# while lossless content still has energy there even when it rolls off hard
# (measured -43 dB for a live lossless stream).
CLIFF_FLOOR_MARGIN_DB = 60.0
# A 44.1 kHz source resampled up to 48 kHz ends abruptly at 22.05 kHz, which is
# the source's own Nyquist and not a codec.  Steps that lie entirely inside the
# top few percent of the band are therefore ignored; a codec wall sits lower
# down than that and is still caught.
CLIFF_EDGE_GUARD = 0.95
SILENCE_FLOOR_DBFS = -70.0    # below this the mid band counts as no content


def log(message: str) -> None:
    print(f"bt11-auto-mode: {message}", file=sys.stderr, flush=True)


# --- device presence --------------------------------------------------------

def _hidraw_has_bt11(path: str) -> bool:
    """True when this /dev/hidraw* node belongs to the BT11 vendor interface."""

    name = os.path.basename(path)
    sysfs = f"/sys/class/hidraw/{name}/device"
    try:
        target = os.path.realpath(sysfs)
    except OSError:
        return False
    parts = target.split(os.sep)
    for component in parts:
        match = re.match(r"^[0-9A-Fa-f]{4}:([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})\.", component)
        if match and match.group(1).lower() == VENDOR_ID and match.group(2).lower() == PRODUCT_ID:
            return True
    return False


def device_present() -> bool:
    """Is the BT11 plugged in (vendor HID interface reachable)?"""

    if os.path.exists(HIDRAW_LINK):
        return True
    for candidate in sorted(__import__("glob").glob("/dev/hidraw*")):
        if _hidraw_has_bt11(candidate):
            return True
    return False


# --- PipeWire introspection -------------------------------------------------

def pw_dump() -> list:
    result = subprocess.run(
        ["pw-dump"], capture_output=True, text=True, timeout=20, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"pw-dump failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _parse_rate(value) -> int | None:
    """``node.rate`` is a fraction such as ``1/44100``."""

    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and "/" in value:
        num, _, den = value.partition("/")
        try:
            num_i, den_i = int(num), int(den)
        except ValueError:
            return None
        if num_i > 0 and den_i > 0:
            return int(round(den_i / num_i))
    return None


def find_sink(document: list) -> tuple[int, str] | None:
    """Return (id, node name) of the BT11 sink."""

    for item in document:
        if item.get("type") != "PipeWire:Interface:Node":
            continue
        props = item.get("info", {}).get("props", {})
        if props.get("media.class") != "Audio/Sink":
            continue
        if SINK_NAME in str(props.get("node.name", "")):
            return item["id"], str(props["node.name"])
    return None


def active_playback(document: list, sink_id: int) -> list[tuple[str, int | None]]:
    """Return (name, sample rate) for every running stream feeding the sink."""

    nodes = {
        item["id"]: item
        for item in document
        if item.get("type") == "PipeWire:Interface:Node"
    }
    found: dict[int, tuple[str, int | None]] = {}
    for item in document:
        if item.get("type") != "PipeWire:Interface:Link":
            continue
        info = item.get("info", {})
        if info.get("input-node-id") != sink_id:
            continue
        peer_id = info.get("output-node-id")
        if peer_id == sink_id:
            continue  # monitor link (sink -> capture client)
        peer = nodes.get(peer_id)
        if peer is None:
            continue
        if peer.get("info", {}).get("state") != "running":
            continue
        props = peer.get("info", {}).get("props", {})
        name = str(props.get("node.name") or props.get("application.name") or peer_id)
        rate = _parse_rate(props.get("node.rate")) or _parse_rate(props.get("audio.rate"))
        # one entry per stream: each stream has one link per channel
        found[peer_id] = (name, rate)
    return list(found.values())


# --- analysis ---------------------------------------------------------------
#
# Band levels come from an averaged FFT, not from biquad filters: a cascade of
# second-order sections has a shallow skirt, so content just below a band leaks
# into it and a real 60 dB codec wall measures as barely 10 dB.  With 8192-point
# windows a two-second capture is measured in a few milliseconds and the numbers
# match the offline analysis in VERIFICATION.md.

NFFT = 8192
EPS = 1e-30


def _power_spectrum(samples, rate: float):
    """Average power spectrum of `samples` (one-sided, Hann windowed)."""

    import numpy as np

    data = np.asarray(samples, dtype=np.float64)
    window = np.hanning(NFFT)
    if len(data) < NFFT:
        padded = np.zeros(NFFT)
        padded[:len(data)] = data
        spectra = np.abs(np.fft.rfft(padded * window)) ** 2
        count = 1
    else:
        acc = np.zeros(NFFT // 2 + 1)
        count = 0
        for start in range(0, len(data) - NFFT + 1, NFFT // 2):
            segment = data[start:start + NFFT] * window
            acc += np.abs(np.fft.rfft(segment)) ** 2
            count += 1
        spectra = acc / max(count, 1)
    return np.fft.rfftfreq(NFFT, 1.0 / rate), spectra


def _band_power(freqs, spectra, low: float, high: float) -> float:
    mask = (freqs >= low) & (freqs < high)
    return float(spectra[mask].mean()) if mask.any() else 0.0


def mid_power_db(samples, rate: float) -> float:
    """Level of the 1-6 kHz reference band, in dB (arbitrary but consistent)."""

    import math

    freqs, spectra = _power_spectrum(samples, rate)
    return 10.0 * math.log10(_band_power(freqs, spectra, *MID_BAND) + EPS)


def codec_cliff_db(samples, capture_rate: float,
                   content_rate: int | None) -> tuple[float, float | None]:
    """Largest band-to-band step that can only be a lossy encoder's wall.

    Returns (drop in dB, position in kHz).  Steps are only accepted below
    `CLIFF_EDGE_GUARD` of the content Nyquist, because the source's own rate
    limit also looks like a wall in a resampled capture.  (0.0, None) means the
    top of the band is continuous, i.e. nothing points at a lossy encoder.
    """

    import math

    rate = content_rate or capture_rate
    limit = min(capture_rate, rate) / 2.0
    guard = CLIFF_EDGE_GUARD * rate / 2.0

    bands: list[tuple[float, float]] = []
    low = CLIFF_START_KHZ * 1000.0
    while low < limit - 500.0:
        high = min(low + CLIFF_BAND_KHZ * 1000.0, limit)
        bands.append((low, high))
        low = high
    if len(bands) < 2:
        return 0.0, None

    freqs, spectra = _power_spectrum(samples, capture_rate)
    levels = [_band_power(freqs, spectra, low, high) for low, high in bands]
    floor = 10.0 * math.log10(_band_power(freqs, spectra, *MID_BAND) + EPS)
    worst, position = 0.0, None
    for index in range(len(levels) - 1):
        if bands[index][0] >= guard:
            continue                      # could be the source-rate edge
        if levels[index] <= 0.0 or levels[index + 1] <= 0.0:
            continue
        above = 10.0 * math.log10(levels[index + 1])
        if above > floor - CLIFF_FLOOR_MARGIN_DB:
            continue                      # something is still there: not a wall
        drop = 10.0 * math.log10(levels[index] / levels[index + 1])
        if drop > worst:
            worst, position = drop, bands[index][1] / 1000.0
    return worst, position


def s16_to_float(raw: bytes) -> list[float]:
    import numpy as np

    return (np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0).tolist()


# --- capture ----------------------------------------------------------------

class SinkMonitor:
    """Keep the BT11 monitor recording into a rolling buffer.

    A one-shot capture costs a ``pw-record`` start-up plus the whole window
    before the first verdict, which is what made mode changes take many
    seconds.  Recording continuously and analysing the most recent window
    instead gives a verdict on every loop iteration.
    """

    def __init__(self, sink_name: str, seconds: float, rate: int = CAPTURE_RATE):
        self.sink_name = sink_name
        self.rate = rate
        self.chunk = rate * CAPTURE_CHANNELS * 2
        self.keep = int(self.chunk * seconds)
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.stop()
        with self.lock:
            self.buffer = bytearray()
        command = [
            "pw-record",
            # WirePlumber only honours a monitor capture when the target is
            # given by *name* and the stream is marked as a sink capture.  With
            # a numeric id it silently links the default source (the
            # microphone) instead, which records silence.
            "-P", "stream.capture.sink=true",
            "--target", self.sink_name,
            "--format", "s16",
            "--rate", str(self.rate),
            "--channels", str(CAPTURE_CHANNELS),
            "-",
        ]
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        while True:
            try:
                chunk = process.stdout.read(8192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self.lock:
                self.buffer.extend(chunk)
                if len(self.buffer) > self.keep:
                    del self.buffer[:len(self.buffer) - self.keep]

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def full(self) -> bool:
        with self.lock:
            return len(self.buffer) >= self.keep

    def mono_samples(self) -> list[float]:
        with self.lock:
            data = bytes(self.buffer)
        return s16_to_float(data)[0::CAPTURE_CHANNELS]

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self.thread = None


def capture_pcm(seconds: float, sink_name: str,
                rate: int = CAPTURE_RATE) -> tuple[list[float], int]:
    """One-shot capture of the sink monitor (used by `once` and friends)."""

    monitor = SinkMonitor(sink_name, seconds, rate)
    monitor.start()
    try:
        deadline = time.monotonic() + seconds + 5.0
        while not monitor.full() and time.monotonic() < deadline:
            time.sleep(0.1)
        return monitor.mono_samples(), rate
    finally:
        monitor.stop()


# --- BT11 control -----------------------------------------------------------

MODE_LINE = re.compile(r"\d+")


def current_mode() -> int | None:
    """Read the BT11's current aptX Adaptive mode.

    `bt11-control aptx-mode` prints a bare number (``19``); the `status`
    command prints ``19 (aptX Lossless)``.  Both are handled by taking the
    first integer.
    """

    result = subprocess.run(
        ["bt11-control", "aptx-mode"], capture_output=True, text=True, timeout=25,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bt11-control aptx-mode failed: {result.stderr.strip()}")
    match = MODE_LINE.search(result.stdout)
    return int(match.group(0)) if match else None


def set_mode(mode: int) -> None:
    result = subprocess.run(
        ["bt11-control", "aptx-mode", str(mode)], capture_output=True, text=True,
        timeout=30, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bt11-control aptx-mode {mode} failed: {result.stderr.strip()}")


# --- decision ---------------------------------------------------------------

def decide(cliff_db: float, cliff_khz: float | None, mid_dbfs: float,
           rate: int | None,
           min_step_db: float = CLIFF_MIN_STEP_DB) -> tuple[int | None, str]:
    """Map a measurement to an aptX Adaptive mode."""

    if rate is None:
        return None, "no running stream with a known sample rate"
    if mid_dbfs < SILENCE_FLOOR_DBFS:
        return None, f"no usable content (mid {mid_dbfs:.1f} dBFS)"
    if cliff_khz is not None and cliff_db >= min_step_db:
        return APTX_LOW_LATENCY, (f"lossy {rate} Hz, {cliff_db:.0f} dB wall at "
                                  f"{cliff_khz:.0f} kHz -> Low Latency")
    detail = f"no codec wall (largest step {cliff_db:.0f} dB)"
    if rate == 44100:
        return APTX_LOSSLESS, f"near-lossless 44.1 kHz, {detail} -> aptX Lossless"
    return APTX_HIGH_QUALITY, f"near-lossless {rate} Hz, {detail} -> High Quality"


def content_rate(players: list[tuple[str, int | None]]) -> int | None:
    rate = None
    for _name, player_rate in players:
        if player_rate:
            rate = max(rate or 0, player_rate)
    return rate


def measure_samples(samples: list[float], capture_rate: int, rate: int | None,
                    min_step_db: float):
    cliff_db, cliff_khz = codec_cliff_db(samples, capture_rate, rate)
    mid_dbfs = mid_power_db(samples, capture_rate)
    return (decide(cliff_db, cliff_khz, mid_dbfs, rate, min_step_db),
            cliff_db, cliff_khz, mid_dbfs, rate)


def measure(sink_name: str, seconds: float, min_step_db: float,
            players: list[tuple[str, int | None]]):
    rate = content_rate(players)
    samples, capture_rate = capture_pcm(seconds, sink_name)
    return measure_samples(samples, capture_rate, rate, min_step_db)


# --- commands ---------------------------------------------------------------

def command_once(args) -> int:
    document = pw_dump()
    sink = find_sink(document)
    if sink is None:
        log("BT11 sink not in the PipeWire graph")
        return 1
    sink_id, sink_name = sink
    players = active_playback(document, sink_id)
    if not players:
        log("nothing is playing into the BT11")
        return 0
    (mode, reason), cliff_db, cliff_khz, mid_dbfs, rate = measure(
        sink_name, args.seconds, args.min_step, players
    )
    where = "none" if cliff_khz is None else f"{cliff_khz:.0f} kHz"
    log(f"players={players} largest band step={cliff_db:.0f} dB at {where} "
        f"mid={mid_dbfs:.1f} dBFS -> {reason}")
    if mode is None:
        return 0
    was = current_mode()
    log(f"current mode {was} ({MODE_NAMES.get(was, '?')}), wanted {mode} "
        f"({MODE_NAMES[mode]})")
    if was != mode and args.apply:
        set_mode(mode)
        log(f"applied {mode}")
    return 0


def command_run(args) -> int:
    if not device_present():
        log("BT11 is not plugged in; exiting so the path unit can re-arm")
        return 0
    log(f"watching the BT11 (window {args.seconds}s, poll {args.interval}s, "
        f"wall threshold {args.min_step} dB, apply={not args.dry_run})")

    history: collections.deque = collections.deque(maxlen=args.debounce)
    last_applied: int | None = None
    last_message: str | None = None
    monitor: SinkMonitor | None = None
    sink_name: str | None = None
    # Switching streams (or a gap between them) leaves a mixed window; waiting
    # one window after the set of playing streams changes keeps those
    # transitions from flipping the mode back and forth.
    settle_until = 0.0
    last_players: tuple | None = None
    next_allowed = 0.0

    def drop_monitor() -> None:
        nonlocal monitor, sink_name
        if monitor is not None:
            monitor.stop()
        monitor, sink_name = None, None

    try:
        while True:
            if not device_present():
                log("BT11 removed; exiting")
                return 0
            document = pw_dump()
            sink = find_sink(document)
            if sink is None:
                drop_monitor()
                if last_message != "no sink":
                    log("BT11 sink absent from the graph; waiting")
                    last_message = "no sink"
                time.sleep(args.interval)
                continue
            sink_id, name = sink
            if monitor is None or sink_name != name or not monitor.alive():
                drop_monitor()
                monitor = SinkMonitor(name, args.seconds)
                monitor.start()
                sink_name = name
                time.sleep(0.2)          # let the first samples arrive
                if last_message != "monitoring":
                    log(f"monitoring {name} through the BT11")
                    last_message = "monitoring"
            players = active_playback(document, sink_id)
            key = tuple(sorted(players))
            if key != last_players:
                last_players = key
                history.clear()
                settle_until = time.monotonic() + args.seconds
            if not players or not monitor.full():
                history.clear()
                if last_message != "idle":
                    log("idle: nothing playing into the BT11")
                    last_message = "idle"
                time.sleep(args.interval)
                continue
            (mode, reason), _cliff_db, _cliff_khz, _mid, _rate = measure_samples(
                monitor.mono_samples(), CAPTURE_RATE, content_rate(players),
                args.min_step
            )
            if reason != last_message:       # one line per state, not per poll
                log(reason)
                last_message = reason
            if mode is None or time.monotonic() < settle_until:
                history.clear()
                time.sleep(args.interval)
                continue
            history.append(mode)
            if len(history) < args.debounce or len(set(history)) != 1:
                time.sleep(args.interval)
                continue
            if (not args.dry_run and last_applied != mode
                    and time.monotonic() >= next_allowed):
                was = current_mode()
                if was != mode:
                    set_mode(mode)
                    log(f"mode {was} -> {mode} ({MODE_NAMES[mode]})")
                else:
                    log(f"mode already {mode} ({MODE_NAMES[mode]})")
                last_applied = mode
                next_allowed = time.monotonic() + args.cooldown
            time.sleep(args.interval)
    finally:
        drop_monitor()


def command_analyse(args) -> int:
    import wave

    with wave.open(args.file, "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width == 2:
        samples = s16_to_float(raw)
    elif width == 3:
        values = []
        for index in range(0, len(raw) - 2, 3):
            value = int.from_bytes(raw[index:index + 3], "little", signed=True)
            values.append(value / 8388608.0)
        samples = values
    else:
        raise SystemExit(f"unsupported sample width {width}")
    left = samples[0::channels]
    content = args.content_rate or rate
    cliff_db, cliff_khz = codec_cliff_db(left, rate, content)
    mid_dbfs = mid_power_db(left, rate)
    print(f"{args.file}: rate={rate} content={content} mid={mid_dbfs:.1f} dB "
          f"largest step={cliff_db:.0f} dB"
          f"{'' if cliff_khz is None else f' at {cliff_khz:.0f} kHz'} "
          f"verdict={'near-lossless' if (cliff_khz is None or cliff_db < args.min_step) else 'lossy'}")
    return 0


def command_self_test(_args) -> int:
    """Check the decision table and the wall detector end to end."""

    import random

    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 1 if not ok else 0
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")

    # decision table: (largest step dB, position kHz, mid dBFS, rate)
    check("no wall, 44.1 kHz", decide(5.0, None, -25.0, 44100)[0], APTX_LOSSLESS)
    check("no wall, 48 kHz", decide(4.0, None, -25.0, 48000)[0], APTX_HIGH_QUALITY)
    check("no wall, 96 kHz", decide(5.0, None, -25.0, 96000)[0], APTX_HIGH_QUALITY)
    check("wall at 18 kHz", decide(63.0, 18.0, -25.0, 44100)[0], APTX_LOW_LATENCY)
    check("wall at 20 kHz (320k)", decide(67.0, 20.0, -25.0, 44100)[0],
          APTX_LOW_LATENCY)
    check("musical rolloff is not a wall", decide(12.0, 20.0, -25.0, 44100)[0],
          APTX_LOSSLESS)
    check("silence decides nothing", decide(0.0, None, -80.0, 44100)[0], None)
    check("unknown rate decides nothing", decide(0.0, None, -25.0, None)[0], None)

    # Detector end to end.  A codec wall is a spectrum that simply stops, so
    # the test signal is built from tones below 15 kHz only; a gradual filter
    # rolloff would not be a fair model of one.
    rate = 48000
    length = rate // 5                       # 0.2 s
    random.seed(7)
    flat = [random.uniform(-0.5, 0.5) for _ in range(length)]
    flat_db, flat_khz = codec_cliff_db(flat, rate, rate)
    check("flat noise: no wall", flat_khz is None or flat_db < CLIFF_MIN_STEP_DB,
          True)
    walled = [0.0] * length
    tones = 150
    for index in range(1, tones + 1):
        frequency = 100.0 * index            # 100 Hz .. 15 kHz
        phase = random.uniform(0.0, 2.0 * math.pi)
        amplitude = 0.4 / math.sqrt(tones)
        for i in range(length):
            walled[i] += amplitude * math.sin(
                2.0 * math.pi * frequency * i / rate + phase)
    wall_db, wall_khz = codec_cliff_db(walled, rate, rate)
    check("15 kHz brick wall: detected",
          wall_db >= CLIFF_MIN_STEP_DB and wall_khz is not None
          and 14.0 <= wall_khz <= 17.0, True)
    print(f"  (measured: flat {flat_db:.0f} dB"
          f"{'' if flat_khz is None else f' at {flat_khz:.0f} kHz'}, "
          f"walled {wall_db:.0f} dB at {wall_khz:.0f} kHz)")
    print(f"self-test: {'PASS' if not failures else f'{failures} failure(s)'}")
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BT11 aptX Adaptive auto mode")
    parser.add_argument("--seconds", type=float, default=1.2,
                        help="audio analysed per decision (default 1.2 s; the "
                             "wall contrast is ~60 dB, so a short window is "
                             "enough and it keeps the reaction time low)")
    parser.add_argument("--interval", type=float, default=0.4,
                        help="seconds between decisions (default 3)")
    parser.add_argument("--cooldown", type=float, default=2.0,
                        help="seconds to wait after a mode change before the "
                             "next one, so a transition cannot cause churn")
    parser.add_argument("--debounce", type=int, default=3,
                        help="identical decisions needed before switching (default 2)")
    parser.add_argument("--min-step", type=float, default=CLIFF_MIN_STEP_DB,
                        help="band-to-band step, in dB, at or above which a lossy "
                             "encoder wall is assumed")
    parser.add_argument("--dry-run", action="store_true",
                        help="log decisions without writing to the BT11")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="service loop (default)")
    once = sub.add_parser("once", help="one decision")
    once.add_argument("--apply", action="store_true")
    analyse = sub.add_parser("analyse", help="measure a wav file")
    analyse.add_argument("file")
    analyse.add_argument("--content-rate", type=int, default=None,
                         help="sample rate of the original content, when the "
                              "file was captured at another rate")
    sub.add_parser("self-test", help="check decisions and DSP")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if not shutil.which("pw-dump"):
        log("pw-dump is not on PATH")
        return 2
    command = args.command or "run"
    if command == "run":
        return command_run(args)
    if command == "once":
        return command_once(args)
    if command == "analyse":
        return command_analyse(args)
    if command == "self-test":
        return command_self_test(args)
    raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    raise SystemExit(main())
