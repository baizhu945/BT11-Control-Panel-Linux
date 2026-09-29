# BT11 verification record

The following checks were performed on the connected FiiO BT11 (`0a12:4007`)
and its connected Bluetooth receivers.

## Environment

- NixOS system generation: `26.11pre1068089.c043004d1c69`
- BT11 firmware: `1.1.4`
- HID control node: `/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw`
- Normal-user permissions: `root:input`, mode `0660`

## Existing BT11 checks

- `bt11-control self-test`: status, name, brightness, codec selection,
  aptX Adaptive mode, LDAC mode and pairing-mode round trips passed.
- Live status readback: aptX Adaptive mode `19` (Lossless), LDAC mode `0`
  (High Quality), receiver connected.
- Live connect/disconnect/reconnect and pairing paths passed.
- Live scan start/stop passed.
- `bt11-control firmware-probe`: DFU connection and device-info reports were
  received.
- Official FiiO V1.1.4 firmware was transferred in full and the updater
  completed reboot, HID re-open, commit, and complete stages. The device still
  reports firmware `1.1.4` afterward.
- Destructive firmware, delete-all-pairings, and factory-reset operations
  require explicit confirmation.
- GUI launched successfully and created the `FiiO BT11 Linux Control` window.
- udev permissions survived precise USB re-enumeration.

## aptX Adaptive automatic mode: sample-rate-only policy

The current implementation intentionally does **not** inspect PCM, run an FFT,
classify audio quality, estimate bitrate, or choose Low Latency. It reads only
running PipeWire playback links connected to the BT11 sink:

| Active known rate | Automatic target |
| --- | --- |
| 44100 Hz or 88200 Hz | `19` aptX Lossless |
| Any other positive rate | `3` High Quality |
| No stream / no known rate | no change |

For multiple active streams at the moment a session starts, a known
non-44.1/88.2 rate is preferred over the lossless rates, and the highest such
rate is used. Thus 44.1 + 88.2 kHz remains Lossless, while 44.1 + 48 kHz and
44.1 + 32 kHz select High Quality. If all rates are unknown, the service makes
no decision.

### The rate is latched per playback session

The rate is read once, when playback starts, and the streams that started it
are remembered as the session's own streams:

- A stream that joins a running session never changes the mode. 44.1 kHz music
  with a 48 kHz notification sound mixed in stays on Lossless, and 48 kHz video
  with a 44.1 kHz notification stays on High Quality. This is what prevents the
  audible interruption caused by a link re-negotiation.
- The session ends only after its own streams have been gone for `--idle-grace`
  seconds (default 1.5 s). The playback that runs *then* starts a fresh session
  and decides the mode, which implements "48 kHz that starts while nothing else
  is playing switches the mode", and the reverse.
- A momentary dropout shorter than the grace keeps the session, so a
  notification landing in that gap cannot change the mode either.
- Streams are identified by `node.name`/`application.name`, not by PipeWire node
  id, so a player re-creating its node does not end the session.

The state machine is deliberately fail-closed:

- Current hardware mode `2` (Low Latency) pauses automatic selection and leaves
  it unchanged. It does not inspect the PipeWire graph while paused.
- Only current modes `3` and `19` may be automatic source states.
- Automatic writes are restricted to modes `3` and `19`; mode `2` is never a
  target.
- The hardware mode is read again immediately before a write, so a manual
  Low-Latency selection is not overwritten.
- An unknown current mode or a failed mode read causes no write.
- The `auto-mode-disabled` flag remains a higher-priority manual override.

The service still uses a short confirmation interval (a ding that lasts less
than `--confirm` seconds cannot rewrite the mode) and a post-write cooldown.
These are timing guards only; they are not audio quality thresholds. The service
does not start `pw-record`, keep an audio buffer, or import numpy.

## Automated checks for the current policy

The test suite covers:

- 44.1/88.2 kHz -> Lossless and all other valid rates -> High Quality;
- invalid, zero, and unknown rates -> no decision;
- `3` <-> `19` transitions only;
- current Low Latency and unknown/unsupported current modes -> no write;
- `node.rate` fraction parsing, malformed rates, and conservative mixed-stream
  handling (including 44.1 + 32 kHz);
- session latching: a 48 kHz insert over 44.1 kHz keeps 44.1 kHz and vice versa,
  a short gap keeps the session, a new rate after the grace starts a new
  session, and a stream key survives node id changes;
- the automatic-mode flag round trip and idempotence;
- the existing BT11 HID protocol layout and command behavior.

Run locally:

```text
python3 -m unittest -v
bt11-auto-mode self-test
bt11-auto-mode once
bt11-auto-mode run --dry-run
```

## Automatic mode switch

`bt11-control auto-mode on|off|toggle` (and the matching GUI checkbox) writes
or removes `~/.local/state/bt11-control/auto-mode-disabled`. The service reads
the flag on every poll, so it is honored after a path-unit restart and works
with the dongle unplugged. `once --apply` refuses to write while the flag is
set.

## Physical plug behavior

`bt11-auto-mode.path` watches the vendor HID node
`/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw`:

- while unplugged, only the path unit is loaded;
- insertion starts `bt11-auto-mode.service` immediately;
- removal makes the service exit, allowing the path unit to re-arm;
- if the dongle is in Low Latency, the service remains idle until the user
  changes it back to High Quality or Lossless.

## Known limitations

- The service can only see the rate exposed by the active PipeWire playback
  stream. If a player or PipeWire has already resampled the source, the
  original rate is not recoverable here.
- While a session runs, every stream that joins it is ignored by design; that is
  what stops a notification sound from interrupting the music.
- A missing or malformed rate never causes a mode write.
- Bluetooth link renegotiation time after a mode write is controlled by the
  BT11 and receiver, not by this metadata-only detector.

## Current execution result

- `python3 -m unittest -v`: **27 tests passed**.
- `bt11-auto-mode self-test`: **PASS**.
- `home-manager build switch -I home-manager=<channel>`: **exit 0** (see the
  environment note below).
- The active user service runs the metadata-only executable and has no
  `pw-record` child.

### Live session test (2026-09-29, BT11 + MOMENTUM 5, aptX Adaptive)

Players were mpv (44.1 kHz WAV) and pw-play (48 kHz WAV) pointed at the BT11
sink, with the mode read back from the dongle itself after every step:

| step | players | mode before | mode after |
| --- | --- | --- | --- |
| 1 | mpv 44.1 kHz | 3 | **19** Lossless |
| 2 | mpv 44.1 kHz + pw-play 48 kHz inserted | 19 | **19** (unchanged) |
| 3 | all playback stopped | 19 | **19** (unchanged) |
| 4 | pw-play 48 kHz alone | 19 | **3** High Quality |
| 5 | pw-play 48 kHz + mpv 44.1 kHz inserted | 3 | **3** (unchanged) |

The service log for steps 1-2 shows the mechanism directly:

```text
playback started: players=[('mpv', 44100)] session rate=44100 Hz
session rate=44100 Hz players=[('mpv', 44100)] current=3 (High Quality) -> 44100 Hz -> aptX Lossless
mode 3 -> 19 (aptX Lossless)
playback continues: players=[('mpv', 44100), ('pw-play', 48000)]; keeping the mode for session rate=44100 Hz
session rate=44100 Hz players=[('mpv', 44100), ('pw-play', 48000)] current=19 (aptX Lossless) -> 44100 Hz -> aptX Lossless; mode already matches
```

Pausing mpv was checked separately: its stream leaves the BT11 sink links
entirely, so a paused player does not hold a session open.

### Environment note

`home-manager build switch` currently fails in a plain shell with
`error: file 'home-manager/home-manager/home-manager.nix' was not found in the
Nix search path`. The system `nix.conf` sets an explicit `nix-path` (root
channels only) and `pure-eval`, so `$NIX_PATH`'s user-channel entry is ignored
and `<home-manager>` no longer resolves. Passing the channel explicitly works:

```text
home-manager build switch -I home-manager=$(readlink -f ~/.nix-defexpr/channels/home-manager)
```
