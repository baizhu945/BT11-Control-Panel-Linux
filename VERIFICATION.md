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
