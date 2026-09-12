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

Lossy encoders put a brick wall at the top of the band; lossless content does
not. The detector therefore looks for the largest **step between neighbouring
1 kHz bands** in the top of the spectrum, which is independent of absolute
level -- real music rolls off by tens of dB across the top octave without ever
stepping. Steps are only accepted below 0.95 of the content Nyquist, because a
44.1 kHz source resampled up to 48 kHz also ends abruptly at 22.05 kHz.

An earlier version compared a high band against a mid band with a fixed
threshold. That was wrong: calibrated on pink noise it separated the cases, but
real music rolls off far more steeply than pink noise, so a genuine lossless
stream (Spotify lossless, measured below) fell under the threshold and was
reported as lossy. The step measure has no such dependence.

Band levels come from an averaged 8192-point FFT, not from biquad filters: a
cascade of second-order sections has a shallow skirt, so content just below a
band leaks into it and a real 60 dB codec wall measures as barely 13 dB.

### Calibration and live results

| Source | Largest band step | Verdict | Mode |
| --- | --- | --- | --- |
| pink noise 44.1 kHz, PCM | 0 dB | near-lossless | 19 |
| pink noise 48 kHz, PCM | 0 dB | near-lossless | 3 |
| same, MP3 128k | 66 dB at 17 kHz | lossy | 2 |
| same, MP3 320k | 52 dB at 21 kHz | lossy | 2 |
| pink noise 48 kHz, AAC 128k | 54 dB at 18 kHz | lossy | 2 |
| **Spotify lossless, live** | **6 dB at 21 kHz** | **near-lossless** | **19** |

The threshold is 35 dB, which leaves at least 29 dB of margin on both sides.
The live row is the one that matters: Spotify's stream reaches the BT11 with
its content intact up to 22.05 kHz and no codec wall, so it is correctly
treated as lossless. Its spectrum is also plotted in `spectrum-compare.png`
(live capture against PCM, MP3 128k, MP3 320k and AAC 128k references): the
lossy references each show a vertical brick wall and then the -77 dB digital
floor, while the live stream declines smoothly and is still 28 dB above that
floor at 21.8 kHz.

Decisions are debounced (two identical readings by default) and only written
when the mode actually differs, so a paused or silent stream never changes
anything.

### Two capture gotchas worth remembering, both found the hard way

- `pw-record --target <numeric node id>` silently links the **default source**
  (the internal microphone) instead of the sink monitor, which records digital
  silence. The working form is the node *name* plus the sink-capture property:
  `pw-record -P stream.capture.sink=true --target alsa_output.usb-FIIO_...`.
- `bt11-control aptx-mode` prints a bare number (`19`), while `status` prints
  `19 (aptX Lossless)`; the reader accepts both.

### Self-test

`bt11-auto-mode self-test` checks the decision table and the detector end to
end, including a synthetic brick wall built from tones below 15 kHz (detected
at 16 kHz with a 132 dB step) against flat noise (0 dB, correctly ignored).

### Second condition, added after watching real content

A single 13-36 dB step occasionally appeared while ordinary music played (well
below the 52-66 dB of a real wall, but well above the 0 dB of lossless
content), which made the verdict jitter. A codec wall is not just a step: the
encoder *zeroes* the spectrum above its cutoff, so the band above the step must
sit at the digital floor. Measured levels relative to the mid band:

| Source | Band above the step | Verdict |
| --- | --- | --- |
| MP3 320k | -77 dB | wall |
| MP3 128k | -76 dB | wall |
| AAC 128k | -76 dB | wall |
| live Spotify lossless | -43 dB (its own top band) | not a wall |

Both conditions (step >= 35 dB **and** the band above at or below 60 dB under
the mid band) are now required, the analysis window is 3 s for more averaging,
and three identical readings are needed before a mode is written.

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
