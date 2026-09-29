#!/usr/bin/env python3
"""Select the FiiO BT11's aptX Adaptive mode from sample rate only.

The BT11 receives decoded PCM over USB.  This service deliberately does not
try to infer the source codec or audio quality from that PCM.  It reads the
sample rate of the active PipeWire playback streams and, only while the BT11
is already in a non-Low-Latency aptX Adaptive mode, keeps the dongle in one of
these two modes:

  44.1 kHz or 88.2 kHz -> 19  aptX Lossless
  every other rate     -> 3   High Quality

Mode 2 (Low Latency) is never selected by this program.  If the current BT11
mode is Low Latency, automatic rate selection is paused and the mode is left
alone.  The service keeps polling in that state so it can resume if the user
manually changes back to High Quality or Lossless.

The sample rate is read once per *playback session*, not on every poll.  A
session starts when audio begins to play and its owning streams are recorded;
while at least one of those streams keeps playing, extra streams (a 48 kHz
notification sound over 44.1 kHz music, or the other way round) are ignored and
the mode is left alone.  Only when the session's own streams have been gone for
``--idle-grace`` seconds does the next playback choose the mode again.

It is started by a systemd path unit when the BT11 is plugged in and exits as
soon as the dongle disappears.  No audio capture, FFT, quality classifier, or
third-party Python package is needed.

Usage:
  bt11-auto-mode run                 service loop (default)
  bt11-auto-mode once [--apply]      one sample-rate decision
  bt11-auto-mode analyse FILE.wav    inspect a WAV header's sample rate
  bt11-auto-mode self-test           check the rate-to-mode decision table
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time

# --- BT11 constants ---------------------------------------------------------

VENDOR_ID = "0a12"
PRODUCT_ID = "4007"
HIDRAW_LINK = "/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw"
SINK_NAME = "BT11"

APTX_LOW_LATENCY = 2
APTX_HIGH_QUALITY = 3
APTX_LOSSLESS = 19
APTX_GOOD_MODES = frozenset({APTX_HIGH_QUALITY, APTX_LOSSLESS})
LOSSLESS_SAMPLE_RATES = frozenset({44100, 88200})

# How long the streams of a playback session may be absent before the session
# is considered over.  It absorbs a player's momentary hiccup so that a
# notification landing in that gap cannot change the mode.
DEFAULT_IDLE_GRACE = 1.5
MODE_NAMES = {
    APTX_LOW_LATENCY: "Low Latency",
    APTX_HIGH_QUALITY: "High Quality",
    APTX_LOSSLESS: "aptX Lossless",
}

# The user can switch automatic mode selection off; bt11-control (CLI and GUI)
# owns the flag file.  The path is duplicated there on purpose: that module
# needs tkinter, this one must stay usable as a small service dependency.
AUTO_MODE_FLAG = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "bt11-control",
    "auto-mode-disabled",
)


# --- common helpers ---------------------------------------------------------


def auto_mode_disabled() -> bool:
    return os.path.exists(AUTO_MODE_FLAG)


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
        match = re.match(
            r"^[0-9A-Fa-f]{4}:([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})\.",
            component,
        )
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
    """Parse PipeWire rates such as ``1/44100`` or ``48000``."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(round(value))
    if not isinstance(value, str):
        return None
    value = value.strip()
    if "/" in value:
        numerator, _, denominator = value.partition("/")
        try:
            numerator_i, denominator_i = int(numerator), int(denominator)
        except ValueError:
            return None
        if numerator_i > 0 and denominator_i > 0:
            return int(round(denominator_i / numerator_i))
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return int(round(parsed)) if parsed > 0 else None


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


@dataclasses.dataclass(frozen=True)
class Stream:
    """One running playback stream feeding the BT11 sink."""

    key: tuple[str, str]   # (node.name, application.name): stable across id churn
    name: str              # what the logs show
    rate: int | None       # node.rate / audio.rate, in Hz

    def pair(self) -> tuple[str, int | None]:
        return (self.name, self.rate)


def _stream_key(props: dict, peer_id: int) -> tuple[str, str]:
    """Identify a stream by name, not by node id.

    PipeWire node ids change whenever a player recreates its stream, while
    node.name/application.name survive that.  A session must not be considered
    over just because a player re-created its node.
    """

    node_name = str(props.get("node.name") or "")
    app_name = str(props.get("application.name") or "")
    if not node_name and not app_name:
        return (f"id:{peer_id}", "")
    return (node_name, app_name)


def active_playback(document: list, sink_id: int) -> list[Stream]:
    """Return every running stream feeding the sink, with its sample rate."""

    nodes = {
        item["id"]: item
        for item in document
        if item.get("type") == "PipeWire:Interface:Node"
    }
    found: dict[int, Stream] = {}
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
        # One entry per stream: each stream has one link per channel.
        found[peer_id] = Stream(key=_stream_key(props, peer_id), name=name, rate=rate)
    return list(found.values())


def players_text(players: list[Stream]) -> str:
    """Compact rendering used in the log lines."""

    return "[" + ", ".join(
        f"({player.name!r}, {player.rate})" for player in players) + "]"


def content_rate(players: list[Stream]) -> int | None:
    """Return an effective rate for all known streams feeding the BT11.

    PipeWire can mix more than one playback stream.  If any known stream uses
    a rate other than 44.1/88.2 kHz, prefer the highest such rate so the mode
    mapping conservatively selects High Quality even when that stream is below
    an exact lossless rate (for example 32 kHz mixed with 44.1 kHz).  Only when
    every known stream is 44.1/88.2 kHz is the highest known rate returned for
    Lossless.  Unknown streams are ignored; an all-unknown set returns None.
    """

    rates = [player.rate for player in players
             if player.rate is not None and player.rate > 0]
    if not rates:
        return None
    non_lossless = [rate for rate in rates if rate not in LOSSLESS_SAMPLE_RATES]
    return max(non_lossless or rates)


class SessionTracker:
    """Decide which sample rate owns the current playback session.

    The mode follows the audio that *started* a session, not whatever happens
    to be mixed into it later.  While at least one stream of the session keeps
    playing, the session rate is frozen, so a 48 kHz notification over 44.1 kHz
    music (or the reverse) cannot change the mode.  The session ends once its
    own streams have been absent for ``idle_grace`` seconds; the playback that
    is running then starts a fresh session and picks the mode.
    """

    def __init__(self, idle_grace: float = DEFAULT_IDLE_GRACE):
        self.idle_grace = max(0.0, float(idle_grace))
        self.keys: frozenset[tuple[str, str]] | None = None
        self.rate: int | None = None
        self.absent_since: float | None = None

    @property
    def active(self) -> bool:
        return self.keys is not None

    @property
    def target_rate(self) -> int | None:
        return self.rate

    def _start(self, keys, rate: int | None) -> None:
        self.keys = frozenset(keys)
        self.rate = rate
        self.absent_since = None

    def _end(self) -> None:
        self.keys, self.rate, self.absent_since = None, None, None

    def update(self, keys, rate: int | None, now: float) -> str:
        """Advance the state machine and describe what happened.

        Events: ``idle`` (nothing playing, no session), ``started`` (a session
        began), ``continues`` (the session's own audio is still playing),
        ``rate-known`` (a session with an unknown rate learned it), ``fading``
        (the session's audio stopped, but not for long enough yet), ``ended``
        (the session ended and nothing is playing), ``replaced`` (the session
        ended and different audio is playing).
        """

        keys = frozenset(keys)
        if self.keys is not None:
            if keys & self.keys:
                self.absent_since = None
                if self.rate is None and rate is not None:
                    self.rate = rate
                    return "rate-known"
                return "continues"
            # None of this session's own streams is playing any more.
            if self.absent_since is None:
                self.absent_since = now
            if now - self.absent_since < self.idle_grace:
                return "fading"
            self._end()
            if keys:
                self._start(keys, rate)
                return "replaced"
            return "ended"
        if not keys:
            return "idle"
        self._start(keys, rate)
        return "started"


# --- sample-rate decision ---------------------------------------------------


def mode_for_rate(rate: int | None) -> int | None:
    """Map a known positive sample rate to the requested good mode."""

    if rate is None or rate <= 0:
        return None
    return APTX_LOSSLESS if rate in LOSSLESS_SAMPLE_RATES else APTX_HIGH_QUALITY


def decide(rate: int | None, current: int | None = None) -> tuple[int | None, str, str]:
    """Return (wanted mode, explanation, state label) for one poll.

    The only writable states are High Quality and aptX Lossless.  Low Latency
    is a user-selected/manual state from the service's point of view: while it
    is active, no automatic transition is allowed in either direction.
    """

    if current == APTX_LOW_LATENCY:
        return (
            None,
            "current mode is Low Latency; automatic sample-rate switching is paused",
            "paused-ll",
        )
    if rate is None:
        return None, "no running stream with a known sample rate", "no-rate"
    if current not in APTX_GOOD_MODES:
        return (
            None,
            f"current mode {current!r} is not High Quality/Lossless; leaving it alone",
            "unknown-current",
        )

    wanted = mode_for_rate(rate)
    assert wanted is not None
    if current == wanted:
        return (
            None,
            f"{rate} Hz -> {MODE_NAMES[wanted]}; mode already matches",
            "holding",
        )
    return (
        wanted,
        f"{rate} Hz -> {MODE_NAMES[wanted]}",
        "switch",
    )


# --- BT11 control -----------------------------------------------------------


MODE_LINE = re.compile(r"\d+")


def current_mode() -> int | None:
    """Read the BT11's current aptX Adaptive mode."""

    result = subprocess.run(
        ["bt11-control", "aptx-mode"],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bt11-control aptx-mode failed: {result.stderr.strip()}")
    match = MODE_LINE.search(result.stdout)
    return int(match.group(0)) if match else None


def set_mode(mode: int) -> None:
    """Write only the two modes allowed by automatic sample-rate selection."""

    if mode not in APTX_GOOD_MODES:
        raise ValueError("automatic selection may write only High Quality or Lossless")
    result = subprocess.run(
        ["bt11-control", "aptx-mode", str(mode)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bt11-control aptx-mode {mode} failed: {result.stderr.strip()}")


# --- commands ---------------------------------------------------------------


def _mode_text(mode: int | None) -> str:
    return "unknown" if mode is None else f"{mode} ({MODE_NAMES.get(mode, 'unknown')})"


def _rate_text(rate: int | None) -> str:
    return "unknown" if rate is None else f"{rate} Hz"


def command_once(args) -> int:
    if auto_mode_disabled():
        log("automatic mode selection is disabled")
        if args.apply:
            log("refusing to write: run 'bt11-control auto-mode on' first")
            return 1
        return 0

    document = pw_dump()
    sink = find_sink(document)
    if sink is None:
        log("BT11 sink not in the PipeWire graph")
        return 1
    sink_id, _sink_name = sink
    players = active_playback(document, sink_id)
    if not players:
        log("nothing is playing into the BT11")
        return 0

    rate = content_rate(players)
    current = current_mode()
    mode, reason, _label = decide(rate, current)
    log(f"players={players_text(players)} rate={_rate_text(rate)} "
        f"current={_mode_text(current)} -> {reason}")
    if mode is None:
        return 0

    # Re-read immediately before an applied write.  This prevents a manual
    # switch to Low Latency during the decision from being overwritten.
    if args.apply and args.dry_run:
        log("dry-run: not applying the requested mode")
        return 0
    if args.apply:
        was = current_mode()
        if was == APTX_LOW_LATENCY:
            log("current mode became Low Latency; leaving it alone")
            return 0
        if was not in APTX_GOOD_MODES:
            log(f"current mode {_mode_text(was)} is not an automatic target; leaving it alone")
            return 0
        if was != mode:
            set_mode(mode)
            log(f"applied {was} -> {mode} ({MODE_NAMES[mode]})")
        else:
            log(f"mode already {mode} ({MODE_NAMES[mode]})")
    return 0


def _sleep_interval(interval: float) -> None:
    time.sleep(max(0.05, interval))


def command_run(args) -> int:
    if not device_present():
        log("BT11 is not plugged in; exiting so the path unit can re-arm")
        return 0
    log(
        f"watching the BT11 (sample rate per playback session, interval "
        f"{args.interval}s, confirm {args.confirm}s, cooldown {args.cooldown}s, "
        f"idle grace {args.idle_grace}s, apply={not args.dry_run})"
    )

    last_message: str | None = None
    last_players: tuple | None = None
    candidate: int | None = None
    candidate_since = 0.0
    next_allowed = 0.0
    tracker = SessionTracker(args.idle_grace)

    while True:
        if not device_present():
            log("BT11 removed; exiting")
            return 0
        if auto_mode_disabled():
            candidate, candidate_since = None, 0.0
            if last_message != "disabled":
                log(
                    "automatic mode selection is disabled "
                    "(re-enable with 'bt11-control auto-mode on'); leaving the mode alone"
                )
                last_message = "disabled"
            _sleep_interval(args.interval)
            continue

        try:
            current = current_mode()
        except (OSError, RuntimeError) as exc:
            candidate, candidate_since = None, 0.0
            if last_message != "mode-error":
                log(f"cannot read current BT11 mode; waiting: {exc}")
                last_message = "mode-error"
            _sleep_interval(args.interval)
            continue

        # This check comes before PipeWire inspection on purpose: Low Latency
        # disables this logic completely, including any audio-side detection.
        if current == APTX_LOW_LATENCY:
            candidate, candidate_since = None, 0.0
            if last_message != "paused-ll":
                log(
                    "current mode is Low Latency; automatic sample-rate switching "
                    "is paused and the mode will be left alone"
                )
                last_message = "paused-ll"
            _sleep_interval(args.interval)
            continue
        if current not in APTX_GOOD_MODES:
            candidate, candidate_since = None, 0.0
            if last_message != "unsupported-current":
                log(f"unsupported current mode {_mode_text(current)}; leaving it alone")
                last_message = "unsupported-current"
            _sleep_interval(args.interval)
            continue

        try:
            document = pw_dump()
            sink = find_sink(document)
            if sink is None:
                candidate, candidate_since = None, 0.0
                if last_message != "no-sink":
                    log("BT11 sink absent from the PipeWire graph; waiting")
                    last_message = "no-sink"
                _sleep_interval(args.interval)
                continue
            sink_id, _sink_name = sink
            players = active_playback(document, sink_id)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            candidate, candidate_since = None, 0.0
            if last_message != "pipewire-error":
                log(f"cannot inspect PipeWire; waiting: {exc}")
                last_message = "pipewire-error"
            _sleep_interval(args.interval)
            continue

        now = time.monotonic()
        rate = content_rate(players)
        event = tracker.update({player.key for player in players}, rate, now)
        session_rate = tracker.target_rate
        player_key = tuple(sorted(player.pair() for player in players))

        # The session rate belongs to the audio that started the session.  Any
        # stream that joins later -- a notification sound at another rate, for
        # instance -- is deliberately ignored until the session is over.
        if event in {"started", "replaced", "rate-known"}:
            last_message = "session"
            headline = {
                "started": "playback started",
                "replaced": "new playback started",
                "rate-known": "session sample rate is now known",
            }[event]
            log(f"{headline}: players={players_text(players)} "
                f"session rate={_rate_text(session_rate)}")
        elif event == "ended":
            last_message = "idle"
            log("playback ended; the next playback start chooses the mode")
        elif event == "idle":
            if last_message != "idle":
                last_message = "idle"
                log("idle: nothing is playing into the BT11")
        elif event == "fading":
            if last_message != "fading":
                last_message = "fading"
                log("the session's own audio stopped; "
                    f"waiting {tracker.idle_grace}s before a new session may decide")
        elif event == "continues" and player_key != last_players:
            last_message = "session"
            log(f"playback continues: players={players_text(players)}; "
                f"keeping the mode for session rate={_rate_text(session_rate)}")
        last_players = player_key

        if session_rate is None:
            candidate, candidate_since = None, 0.0
            # Only complain while a session is actually running: when nothing
            # plays at all the "idle" line above already says everything.
            if tracker.active and last_message != "no-rate":
                last_message = "no-rate"
                log("the playback session has no known sample rate yet; no decision")
            _sleep_interval(args.interval)
            continue

        mode, reason, label = decide(session_rate, current)
        message_key = f"{label}:{mode}:{session_rate}:{current}"
        if message_key != last_message:
            log(f"session rate={session_rate} Hz players={players_text(players)} "
                f"current={_mode_text(current)} -> {reason}")
            last_message = message_key

        now = time.monotonic()
        if mode is None:
            candidate, candidate_since = None, 0.0
            _sleep_interval(args.interval)
            continue
        if mode != candidate:
            candidate, candidate_since = mode, now
        if now - candidate_since < args.confirm:
            # A very short session (a notification ding) must not rewrite the
            # mode; the verdict has to hold for the confirmation time.
            _sleep_interval(args.interval)
            continue
        if now < next_allowed:
            _sleep_interval(args.interval)
            continue

        if auto_mode_disabled():
            candidate, candidate_since = None, 0.0
            _sleep_interval(args.interval)
            continue
        try:
            # Do not overwrite a manual Low Latency selection that happened
            # while the candidate was being confirmed.
            was = current_mode()
            if was == APTX_LOW_LATENCY:
                candidate, candidate_since = None, 0.0
                if last_message != "paused-ll":
                    log("current mode became Low Latency; leaving it alone")
                    last_message = "paused-ll"
            elif was not in APTX_GOOD_MODES:
                candidate, candidate_since = None, 0.0
            elif args.dry_run:
                log(f"dry-run: would change mode {was} -> {mode} ({MODE_NAMES[mode]})")
                next_allowed = time.monotonic() + args.cooldown
            elif was != mode:
                set_mode(mode)
                log(f"mode {was} -> {mode} ({MODE_NAMES[mode]})")
                next_allowed = time.monotonic() + args.cooldown
            else:
                next_allowed = time.monotonic() + args.cooldown
        except (OSError, RuntimeError, ValueError) as exc:
            candidate, candidate_since = None, 0.0
            if last_message != "write-error":
                log(f"mode update failed; waiting: {exc}")
                last_message = "write-error"
        _sleep_interval(args.interval)


def command_analyse(args) -> int:
    import wave

    with wave.open(args.file, "rb") as handle:
        file_rate = handle.getframerate()
        channels = handle.getnchannels()
        frames = handle.getnframes()
    content = args.content_rate or file_rate
    wanted = mode_for_rate(content)
    rate_text = "unknown" if content is None else f"{content} Hz"
    print(
        f"{args.file}: file_rate={file_rate} Hz content_rate={rate_text} "
        f"channels={channels} frames={frames} -> "
        f"{MODE_NAMES[wanted] if wanted is not None else 'no decision'}"
    )
    return 0


def command_self_test(_args) -> int:
    """Check the sample-rate mapping and Low-Latency guard."""

    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        if not ok:
            failures += 1
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")

    LL, HQ, LS = APTX_LOW_LATENCY, APTX_HIGH_QUALITY, APTX_LOSSLESS
    check("44.1 kHz selects Lossless", mode_for_rate(44100), LS)
    check("88.2 kHz selects Lossless", mode_for_rate(88200), LS)
    check("48 kHz selects High Quality", mode_for_rate(48000), HQ)
    check("96 kHz selects High Quality", mode_for_rate(96000), HQ)
    check("HQ changes to Lossless at 44.1 kHz", decide(44100, HQ)[0], LS)
    check("Lossless changes to HQ at 48 kHz", decide(48000, LS)[0], HQ)
    check("matching HQ is held", decide(48000, HQ)[0], None)
    check("matching Lossless is held", decide(88200, LS)[0], None)
    check("Low Latency is held at 44.1 kHz", decide(44100, LL)[0], None)
    check("Low Latency is held at 48 kHz", decide(48000, LL)[0], None)
    check("unknown rate decides nothing", decide(None, HQ)[0], None)
    check("unknown current mode decides nothing", decide(44100, None)[0], None)
    check("fractional 1/44100 parses", _parse_rate("1/44100"), 44100)
    check("fractional 2/88200 parses", _parse_rate("2/88200"), 44100)

    # A notification at another rate must not change the session's rate.
    music = frozenset({("mpv", "mpv")})
    ding = frozenset({("pw-play", "pw-play")})
    tracker = SessionTracker(1.5)
    check("session starts at 44.1 kHz", tracker.update(music, 44100, 0.0), "started")
    check("session rate frozen at 44.1 kHz", tracker.target_rate, 44100)
    check("48 kHz insert keeps the session",
          tracker.update(music | ding, 48000, 0.4), "continues")
    check("48 kHz insert keeps 44.1 kHz", tracker.target_rate, 44100)
    check("ding leaving keeps the session",
          tracker.update(music, 44100, 0.8), "continues")
    check("session survives a short gap",
          tracker.update(frozenset(), None, 1.2), "fading")
    check("music returning resumes the session",
          tracker.update(music, 44100, 2.0), "continues")
    check("session ends after the grace",
          tracker.update(frozenset(), None, 6.0), "fading")
    check("late poll past the grace ends it",
          tracker.update(frozenset(), None, 9.0), "ended")
    check("silence without a session is idle",
          tracker.update(frozenset(), None, 10.0), "idle")
    check("playback after silence starts a new session",
          tracker.update(ding, 48000, 11.0), "started")
    check("new session uses the new rate", tracker.target_rate, 48000)
    check("handover waits for the grace",
          tracker.update(music, 44100, 20.0), "fading")
    check("handover restarts the session after the grace",
          tracker.update(music, 44100, 22.0), "replaced")
    check("handover adopts the new rate", tracker.target_rate, 44100)

    print(f"self-test: {'PASS' if not failures else f'{failures} failure(s)'}")
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BT11 aptX Adaptive sample-rate auto mode")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.4,
        help="seconds between polls (default 0.4)",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=3.0,
        help="seconds to wait after a mode change (default 3)",
    )
    parser.add_argument(
        "--confirm",
        type=float,
        default=1.0,
        help="seconds a new sample-rate verdict must persist (default 1)",
    )
    parser.add_argument(
        "--idle-grace",
        type=float,
        default=DEFAULT_IDLE_GRACE,
        help="seconds a playback session's own streams may be absent before a "
             "new playback session decides the mode again (default "
             f"{DEFAULT_IDLE_GRACE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log decisions without writing to the BT11",
    )
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="service loop (default)")
    run.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="log decisions without writing to the BT11",
    )
    once = sub.add_parser("once", help="one sample-rate decision")
    once.add_argument("--apply", action="store_true")
    once.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="do not write even if --apply is supplied",
    )
    analyse = sub.add_parser("analyse", help="inspect a WAV header's sample rate")
    analyse.add_argument("file")
    analyse.add_argument(
        "--content-rate",
        type=int,
        default=None,
        help="original content rate when the WAV was captured after resampling",
    )
    sub.add_parser("self-test", help="check sample-rate decisions")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "run"
    if command in {"run", "once"} and not shutil.which("pw-dump"):
        log("pw-dump is not on PATH")
        return 2
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
