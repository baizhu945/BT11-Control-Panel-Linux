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
import statistics
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
# How lossy content is told apart from lossless content.
#
# A lossy encoder puts a hard wall at the top of the band; a natural, lossless
# rolloff never does.  The trigger is therefore the largest step between
# neighbouring 1 kHz bands, searched only below WALL_MAX_KHZ: a 44.1 kHz source
# resampled up to 48 kHz ends at 22.05 kHz, and a resampler's transition band
# around 20-22 kHz would otherwise look like a wall (measured: a 19 dB step for
# a *lossless* file played through Chromium).
#
# Measured distributions (1 kHz bands; finer bands only smear the wall):
#   lossless  PCM 44.1k/48k, Chromium 44.1k       step 0.3 -  2.7 dB
#   Spotify lossless, real music, live            step 3.8 - 15   dB
#   lossy     bilibili AAC 48k                    step 22  - 55   dB
#   lossy     MP3 128k / AAC 128k                 step 66  / 54   dB
# Real music closes the gap that synthetic noise suggests: the classes are only
# about 7 dB apart (Spotify reaches ~15 dB, bilibili starts at ~22 dB).
#
# The two thresholds are deliberately biased towards *not* degrading lossless
# audio, because the two mistakes do not cost the same: entering Low Latency on
# lossless content loses quality, while staying in Lossless/High Quality on
# lossy content only costs a little latency.
#
#   M_l: to *enter* Low Latency the step must reach WALL_ENTER_LL_DB   (20 dB)
#   M_h: to *leave* Low Latency the step must fall to WALL_LEAVE_LL_DB (12 dB)
#
# In quality terms (how intact the top of the band is) that is M_l = -20 dB and
# M_h = -12 dB: hysteresis, so entering needs clearly bad content, returning
# needs clearly good content, and the 12-20 dB dead band leaves the mode alone.
# That dead band is what stops the two modes from flapping and interrupting the
# audio, and it also makes bilibili sticky: one clear wall puts it in Low
# Latency and the dead band keeps it there.
WALL_ENTER_LL_DB = 20.0
WALL_LEAVE_LL_DB = 12.0
WALL_START_KHZ = 8.0
WALL_BAND_KHZ = 1.0
WALL_MAX_KHZ = 19.0
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


def wall_step_db(samples, capture_rate: float,
                 content_rate: int | None) -> tuple[float, float | None]:
    """Largest band-to-band step below WALL_MAX_KHZ, and where it sits.

    Lossless content rolls off smoothly, so this stays at a few dB; a lossy
    encoder's wall shows up as tens of dB.
    """

    rate = content_rate or capture_rate
    limit = min(capture_rate, rate) / 2.0
    top = min(WALL_MAX_KHZ, limit - WALL_BAND_KHZ / 2.0)
    bands: list[tuple[float, float]] = []
    low = WALL_START_KHZ * 1000.0
    while low + WALL_BAND_KHZ * 1000.0 <= top * 1000.0 + 1.0:
        bands.append((low, low + WALL_BAND_KHZ * 1000.0))
        low += WALL_BAND_KHZ * 1000.0
    if len(bands) < 2:
        return 0.0, None
    freqs, spectra = _power_spectrum(samples, capture_rate)
    levels = [10.0 * __import__("math").log10(
        _band_power(freqs, spectra, low, high) + EPS) for low, high in bands]
    worst, position = 0.0, None
    for index in range(len(levels) - 1):
        step = levels[index] - levels[index + 1]
        if step > worst:
            worst, position = step, bands[index][1] / 1000.0
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

def decide(step_db: float | None, mid_db: float, rate: int | None,
           current: int | None = None,
           enter_ll: float = WALL_ENTER_LL_DB,
           leave_ll: float = WALL_LEAVE_LL_DB) -> tuple[int | None, str]:
    """Map a wall-step measurement to an aptX Adaptive mode, with hysteresis.

    `current` is the mode last written.  Entering Low Latency needs a step of at
    least `enter_ll` dB; leaving it needs the step to fall to `leave_ll` dB.
    Anything in between keeps the current mode, so borderline content cannot
    make the two modes flap (every change costs a link re-negotiation).
    """

    if rate is None:
        return None, "no running stream with a known sample rate", "no-rate"
    if mid_db < SILENCE_FLOOR_DBFS or step_db is None:
        return None, f"no usable content (reference band {mid_db:.1f} dB)", "silent"
    good = APTX_LOSSLESS if rate == 44100 else APTX_HIGH_QUALITY
    if current == APTX_LOW_LATENCY:
        if step_db <= leave_ll:
            return good, (f"top of band recovered (step {step_db:.0f} dB <= "
                          f"{leave_ll:.0f} dB) -> {MODE_NAMES[good]}"), "release"
        return None, (f"staying in Low Latency: step {step_db:.0f} dB has not "
                      f"fallen to {leave_ll:.0f} dB"), "hold-ll"
    if step_db >= enter_ll:
        return APTX_LOW_LATENCY, (f"lossy {rate} Hz: {step_db:.0f} dB wall -> "
                                  f"Low Latency"), "enter-ll"
    # On the good side the sample rate is a *fact*, not a measurement, so the
    # Lossless/High-Quality choice tracks it on every poll.  (Tracking it only
    # when leaving Low Latency left the dongle stuck on High Quality forever.)
    if current != good:
        return good, (f"near-lossless {rate} Hz, step {step_db:.0f} dB -> "
                      f"{MODE_NAMES[good]}"), "good-mode"
    return None, (f"holding {MODE_NAMES[good]}: step {step_db:.0f} dB is below "
                  f"trigger (release at {leave_ll:.0f} dB)"), "hold-good"


def content_rate(players: list[tuple[str, int | None]]) -> int | None:
    """Sample rate of the content, taken from the playing PipeWire streams."""

    rate = None
    for _name, player_rate in players:
        if player_rate:
            rate = max(rate or 0, player_rate)
    return rate


def measure_samples(samples: list[float], capture_rate: int, rate: int | None,
                    args, current: int | None = None):
    step_db, position = wall_step_db(samples, capture_rate, rate)
    mid_db = mid_power_db(samples, capture_rate)
    return (decide(step_db, mid_db, rate, current,
                   getattr(args, "enter_ll", WALL_ENTER_LL_DB),
                   getattr(args, "leave_ll", WALL_LEAVE_LL_DB)),
            step_db, mid_db, rate, position, current)


def measure(sink_name: str, seconds: float, args, players):
    rate = content_rate(players)
    samples, capture_rate = capture_pcm(seconds, sink_name)
    result = measure_samples(samples, capture_rate, rate, args, current_mode())
    return result[:4]


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
    (mode, reason, _label), step_db, mid_db, rate, position = measure(
        sink_name, args.seconds, args, players)
    shown = "n/a" if step_db is None else f"{step_db:.1f}"
    where = "n/a" if position is None else f"{position:.0f} kHz"
    log(f"players={players} step={shown} dB at {where} ref={mid_db:.1f} dB "
        f"-> {reason}")
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
        f"enter LL at {args.enter_ll} dB step, leave LL at "
        f"{args.leave_ll} dB step, "
        f"confirm {args.confirm}s, apply={not args.dry_run})")

    last_written: int | None = None
    last_message: str | None = None
    candidate: int | None = None
    candidate_since = 0.0
    settle_until = 0.0
    last_players: tuple | None = None
    next_allowed = 0.0
    monitor: SinkMonitor | None = None
    sink_name: str | None = None

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
            now = time.monotonic()
            if key != last_players:
                # A new set of streams leaves a window that mixes old and new
                # audio; start clean and wait one window before deciding.
                last_players = key
                candidate, candidate_since = None, 0.0
                settle_until = now + args.seconds
            if not players or not monitor.full():
                candidate, candidate_since = None, 0.0
                if last_message != "idle":
                    log("idle: nothing playing into the BT11")
                    last_message = "idle"
                time.sleep(args.interval)
                continue

            samples = monitor.mono_samples()
            rate = content_rate(players)
            step_db, _position = wall_step_db(samples, CAPTURE_RATE, rate)
            mode, reason, label = decide(step_db,
                                         mid_power_db(samples, CAPTURE_RATE),
                                         rate, last_written, args.enter_ll,
                                         args.leave_ll)
            key = f"{label}:{mode}"
            if key != last_message:          # one line per state, not per poll
                log(reason)
                last_message = key
            if mode is None or now < settle_until:
                candidate, candidate_since = None, 0.0
                time.sleep(args.interval)
                continue
            if mode != candidate:
                candidate, candidate_since = mode, now
            if now - candidate_since < args.confirm:
                time.sleep(args.interval)
                continue
            if (not args.dry_run and last_written != mode
                    and now >= next_allowed):
                was = current_mode()
                if was != mode:
                    set_mode(mode)
                    log(f"mode {was} -> {mode} ({MODE_NAMES[mode]})")
                else:
                    log(f"mode already {mode} ({MODE_NAMES[mode]})")
                last_written = mode
                next_allowed = now + args.cooldown
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
    step_db, position = wall_step_db(left, rate, content)
    mid = mid_power_db(left, rate)
    _mode, reason, _label = decide(step_db, mid, content, None,
                                   args.enter_ll, args.leave_ll)
    shown = "n/a" if step_db is None else f"{step_db:.1f}"
    where = "n/a" if position is None else f"{position:.0f} kHz"
    print(f"{args.file}: rate={rate} content={content} ref={mid:.1f} dB "
          f"step={shown} dB at {where} -> {reason}")
    return 0


def command_self_test(_args) -> int:
    """Check the decision table (including hysteresis) and the metric."""

    import random

    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 1 if not ok else 0
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")

    LL, HQ, LS = APTX_LOW_LATENCY, APTX_HIGH_QUALITY, APTX_LOSSLESS
    enter, leave = WALL_ENTER_LL_DB, WALL_LEAVE_LL_DB
    dead = (enter + leave) / 2.0

    # from a good mode: only a clear wall triggers Low Latency
    check("lossless 44.1k, no wall", decide(0.0, -25.0, 44100, LS)[0], None)
    check("lossless 48k, no wall", decide(4.0, -25.0, 48000, HQ)[0], None)
    check("spotify worst case, no wall", decide(15.0, -25.0, 44100, LS)[0], None)
    check("bilibili wall from good mode", decide(22.0, -25.0, 48000, HQ)[0], LL)
    check("dead band from good mode", decide(dead, -25.0, 48000, HQ)[0], None)
    check("just below M_l", decide(enter - 1, -25.0, 48000, HQ)[0], None)
    check("at M_l", decide(enter, -25.0, 48000, HQ)[0], LL)
    # from Low Latency: only clearly good content releases it
    check("stays LL in the dead band", decide(dead, -25.0, 48000, LL)[0], None)
    check("stays LL on a wall", decide(40.0, -25.0, 48000, LL)[0], None)
    check("stays LL above M_h", decide(leave + 1, -25.0, 48000, LL)[0], None)
    check("leaves LL at M_h", decide(leave, -25.0, 48000, LL)[0], HQ)
    check("leaves LL for 44.1k", decide(0.0, -25.0, 44100, LL)[0], LS)
    # the good mode tracks the sample rate on every poll, not just at release
    check("HQ upgrades to Lossless at 44.1k", decide(6.0, -25.0, 44100, HQ)[0], LS)
    check("Lossless falls to HQ at 48k", decide(6.0, -25.0, 48000, LS)[0], HQ)
    check("unknown current sets the good mode", decide(6.0, -25.0, 44100, None)[0], LS)
    # no content
    check("silence decides nothing", decide(0.0, -80.0, 44100, LS)[0], None)
    check("unknown rate decides nothing", decide(0.0, -25.0, None, LS)[0], None)

    # Metric end to end: flat noise has no wall, a signal built only from tones
    # below 15 kHz does.
    rate = 48000
    length = rate // 5
    random.seed(7)
    flat = [random.uniform(-0.5, 0.5) for _ in range(length)]
    flat_step, _pos = wall_step_db(flat, rate, rate)
    check("flat noise: no wall", flat_step < WALL_ENTER_LL_DB, True)
    walled = [0.0] * length
    tones = 150
    for index in range(1, tones + 1):
        frequency = 100.0 * index            # 100 Hz .. 15 kHz
        phase = random.uniform(0.0, 2.0 * math.pi)
        amplitude = 0.4 / math.sqrt(tones)
        for i in range(length):
            walled[i] += amplitude * math.sin(
                2.0 * math.pi * frequency * i / rate + phase)
    wall_step, wall_pos = wall_step_db(walled, rate, rate)
    check("15 kHz limited signal: wall found", wall_step >= WALL_ENTER_LL_DB, True)
    print(f"  (measured step: flat {flat_step:.0f} dB, limited {wall_step:.0f} dB "
          f"at {wall_pos} kHz)")
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
    parser.add_argument("--cooldown", type=float, default=3.0,
                        help="seconds to wait after a mode change before the "
                             "next one, so a transition cannot cause churn")
    parser.add_argument("--enter-ll", type=float, default=WALL_ENTER_LL_DB,
                        help="M_l: band-to-band step in dB that Low Latency "
                             "needs before it is entered")
    parser.add_argument("--leave-ll", type=float, default=WALL_LEAVE_LL_DB,
                        help="M_h: band-to-band step in dB the content must "
                             "fall back to before Low Latency is left")
    parser.add_argument("--confirm", type=float, default=2.5,
                        help="seconds a new verdict must persist before a mode "
                             "is written")
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
