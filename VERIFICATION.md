# BT11 verification record

The following checks were performed on the connected FiiO BT11 (`0a12:4007`)
and its connected WF-1000XM5 receiver.

## Environment

- NixOS system generation: `26.11pre1068089.c043004d1c69`
- BT11 firmware: `1.1.4`
- HID control node: `/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw`
- Normal-user permissions: `root:input`, mode `0660`

## Passed checks

- `python3 -m unittest -v`: 7 protocol tests passed.
- `bt11-control self-test`: status, name, brightness, codec selection,
  aptX Adaptive mode, LDAC mode and pairing-mode round trips passed.
- Live status readback: aptX Adaptive mode `19` (Lossless), LDAC mode `0`
  (High Quality), receiver connected.
- Live connect/disconnect/reconnect cycle passed.
- Live pair command path passed using the already paired receiver.
- Live scan start/stop passed; no additional discoverable receiver was found
  during the scan.
- `bt11-control firmware-probe`: DFU connection and device-info reports were
  received.
- Official FiiO V1.1.4 firmware was actually transferred in full and the
  updater completed the reboot, hidraw re-open, commit, and complete stages.
  The device still reports firmware `1.1.4` afterward.
- `bt11-control reset --yes` was executed with authorization. It cleared the
  paired-device list and disconnected the receiver; codec/mode state was then
  read back and the selectable codec set plus LDAC mode were restored.
- `bt11-control forget-all --yes` was executed once on the empty list and
  once with the receiver present; the populated-list run removed the receiver
  and returned an empty list.
- A delete request for the confirmed-nonexistent address
  `00:00:00:00:00:00` did not receive a device acknowledgement, but the
  existing `WF-1000XM5` record and connection remained unchanged.
- GUI launched successfully and created the `FiiO BT11 Linux Control` window.
- udev permissions survived a precise USB re-enumeration.
- Firmware update, delete-all-pairings, and factory-reset guards reject the
  operation unless explicit confirmation is supplied.

## Not yet restored / remaining physical step

The WF-1000XM5 address is still known (`80:99:E7:6A:8F:CC`), but after the
populated-list delete it is not advertising and BT11's scan found no matching
device. After the headphones were placed into physical pairing mode, the
controller paired the recorded address directly and restored the connection;
the final status shows the receiver connected again.

## Firmware pairing-state observation

After the live `pair` test on an already paired receiver, the firmware kept
the pairing mode at `automatic`. Sending `close` and re-enumerating the USB
device did not change that readback; the program reports the device's actual
state rather than hiding this firmware behavior.

## aptX Adaptive auto mode (2026-09-12)

Verified on the connected BT11 (`0a12:4007`, firmware `1.1.4`) driving the
MOMENTUM 5, with the `bt11-auto-mode` service and its path unit.

### How the detector works

A lossy encoder puts a hard wall at the top of the band; lossless content rolls
off smoothly. The trigger is the largest step between neighbouring 1 kHz bands
from 8 kHz up, and it is only searched below 19 kHz: a 44.1 kHz source resampled
up to 48 kHz ends at 22.05 kHz, and Chromium's resampler transition adds a 19 dB
step at 20-21 kHz that would otherwise be indistinguishable from a codec wall.

Measured distributions (service log plus direct captures, 2026-09-12):

| content | step |
| --- | --- |
| lossless PCM 44.1 / 48 kHz | 0.3 - 0.5 dB |
| Chromium playing a 44.1 kHz lossless file | 0.5 dB |
| Spotify lossless, real music, several sessions | 3.8 - 15 dB (median ~5) |
| bilibili HiRes in Chromium (AAC) | 22 - 55 dB |
| MP3 128k / AAC 128k | 66 / 54 dB |

Real music narrows the gap that synthetic noise suggests: Spotify reaches ~15 dB and
bilibili starts at ~22 dB, so the usable window is only about 7 dB wide. Finer bands
do not help -- 250 Hz bands smear the wall and *reduce* the lossy readings (MP3 128k
66 -> 36 dB, AAC 128k 54 -> 23 dB), so 1 kHz bands are kept.

The log history is what motivates hysteresis. Mining the journal over four hours:
tilt for Spotify sat at -30 to -2 dB (median -26) while Chromium/bilibili sat at
-76 to -2 dB (median -62); the two *tails* overlap near -30 dB, so no single
threshold separates them -- moving it either way makes one of the two flap. The
step metric separates them by ~18 dB instead.

### Hysteresis (variable thresholds)

| | meaning | value |
| --- | --- | --- |
| M_l | step needed to **enter** Low Latency | 16 dB |
| M_h | step the content must fall to before Low Latency is **left** | 8 dB |
| dead band | 8-16 dB: the mode is left alone | - |

Margins: Spotify's worst window is 9.5 dB, 6.5 dB below the trigger, so it never
enters Low Latency; bilibili's best is 22 dB, 6 dB above the trigger, and far
above the 8 dB release, so once it enters it stays. A change also needs 2.5 s of
agreement and is followed by a 3 s cooldown.

Verified after the change: Spotify playing continuously for 100 s produced
**0 mode changes** and the dongle stayed on mode 19.

### Mode change latency (measured 2026-09-12)

The first version decided every ~6 s and needed three agreeing verdicts, so a
content change took about 18 s to reach the dongle; an 8 s lossy file did not
even trigger a switch. The loop now records the monitor continuously into a
rolling buffer and analyses the most recent window on every poll:

| Step | Time |
| --- | --- |
| `bt11-control aptx-mode` read | 0.12 s |
| `bt11-control aptx-mode <n>` write | 0.14 s |
| `pw-dump` | 0.01 s |
| window / poll / debounce | 1.2 s / 0.4 s / 3 agreements |
| **lossless -> lossy, measured end to end** | **2.7 s** |
| **lossy -> lossless, measured end to end** | **2.7 s** |

The window was tested at 1.0 s, 1.5 s and 2.0 s against a lossy file, a
lossless 44.1 kHz file and a lossless 48 kHz file: all nine combinations gave
the correct verdict, because the wall contrast is about 60 dB and does not
depend on averaging. 1.2 s was chosen for margin.

Two guards keep transitions from causing churn: the verdict history is cleared
and decisions are held for one window whenever the set of playing streams
changes, and a mode write is followed by a 2 s cooldown. Without them the log
showed `19 -> 2 -> 19 -> 2` inside three seconds while files were swapped.

Anything beyond the ~2.7 s above is the BT11 and the headset re-negotiating the
Bluetooth link after the HID write; that is not observable from the host.

### Physical plug test

Unplugging and replugging the dongle by hand produced exactly the intended
sequence, with nothing running in between:

```text
13:09:06 bt11-auto-mode: BT11 removed; exiting
13:09:06 systemd: bt11-auto-mode.service: Consumed 1.248s CPU time over 2min 36s
13:09:08 systemd: Started Match the FiiO BT11 aptX Adaptive mode ...
13:09:09 bt11-auto-mode[1743037]: watching the BT11 (sample 2.0s, interval 3.0s, wall threshold 35.0 dB, apply=True)
```

Note that a verdict can jitter when two streams play into the BT11 at once
(for example Spotify plus a local test file): the summed stream really does
have content above the lossy file's cutoff, so "no wall" is the correct reading
for what the dongle receives, and the debounce keeps the mode from flapping.
