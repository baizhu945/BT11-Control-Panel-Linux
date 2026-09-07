#!/usr/bin/env python3
"""Linux control panel for the FiiO BT11 USB Bluetooth transmitter.

The BT11 exposes a vendor HID interface in addition to its USB audio
interface.  This module talks to that HID interface directly through
hidraw, so it does not need a browser or a third-party Python package.

The command layout is based on the public FiiO web driver's BT11 path.  It
is intentionally kept in one small module so the CLI and Tk GUI cannot
silently implement different device behaviour.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import glob
import os
import select
import sys
import threading
import time
from typing import Iterable, Optional, Sequence

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # CLI users do not need the Tk extension installed.
    tk = None
    filedialog = None
    messagebox = None
    ttk = None


VENDOR_ID = 0x0A12
PRODUCT_ID = 0x4007
REPORT_ID = 7
INPUT_REPORT_IDS = {6, 8, 9}
DFU_FEATURE_REPORT_ID = 3
DFU_OUTPUT_REPORT_ID = 5
DFU_INPUT_REPORT_ID = 6
DFU_FEATURE_LENGTH = 62
DFU_OUTPUT_LENGTH = 254
DFU_CHUNK_SIZE = 249

CODEC_IDS = {
    "sbc": 1,
    "aptx": 3,
    "aptx-ll": 5,
    "aptx-hd": 6,
    "aptx-adaptive": 7,
    "ldac": 8,
    "lhdc": 9,
}
CODEC_LABELS = {
    "aptx-adaptive": "aptX Adaptive",
    "aptx-hd": "aptX HD",
    "aptx-ll": "aptX LL",
    "aptx": "aptX",
    "ldac": "LDAC",
    "lhdc": "LHDC",
    "sbc": "SBC",
}
USER_CODEC_NAMES = ("ldac", "aptx-adaptive", "aptx-hd", "aptx", "aptx-ll")
CODEC_NAMES_BY_ID = {value: key for key, value in CODEC_IDS.items()}

APTX_MODES = {2: "Low Latency", 3: "High Quality", 19: "aptX Lossless"}
LDAC_MODES = {0: "High Quality", 1: "Standard Quality", 2: "Mobile Quality"}
PAIRING_MODES = {0: "closed", 1: "automatic", 2: "manual"}
PAIRING_MODE_IDS = {value: key for key, value in PAIRING_MODES.items()}

# FiiO's web panel uses these commands for the BT11's app feature.
APP_FEATURE = 24
CORE_FEATURE = 0
CMD_GET_FIRMWARE = 5
CMD_GET_LOCAL_NAME = 0
CMD_SET_LOCAL_NAME = 1
CMD_RESET_SETTINGS = 121
CMD_GET_CODECS = 6
CMD_SET_CODECS = 7
CMD_GET_APTX_MODE = 64
CMD_SET_APTX_MODE = 65
CMD_GET_LDAC_MODE = 66
CMD_SET_LDAC_MODE = 67
CMD_GET_PAIRING_MODE = 10
CMD_SET_PAIRING_MODE = 11
CMD_GET_CONNECTED_STATUS = 12
CMD_GET_PAIRED_LIST = 14
CMD_GET_REMOTE_NAME = 15
CMD_CONNECT_DEVICE = 16
CMD_DISCONNECT_DEVICE = 17
CMD_PAIR_DEVICE = 18
CMD_DELETE_PAIRED_DEVICE = 19
CMD_DELETE_ALL_PAIRED = 20
CMD_REBOOT = 122
CMD_GET_BRIGHTNESS = 82
CMD_SET_BRIGHTNESS = 83

DFU_MSG_DEVICE_INFO_REQ = 25
DFU_MSG_DEVICE_INFO_CFM = 26
DFU_HOST_SYNC_REQ = 52
DFU_HOST_SYNC_CFM = 20
DFU_HOST_START_REQ = 34
DFU_HOST_START_CFM = 2
DFU_HOST_START_DATA_REQ = 54
DFU_HOST_DATA_BYTES_CFM = 3
DFU_HOST_DATA = 37
DFU_HOST_ABORT_REQ = 40
DFU_HOST_ABORT_CFM = 8
DFU_HOST_TRANSFER_COMPLETE_IND = 11
DFU_HOST_TRANSFER_COMPLETE_RES = 45
DFU_HOST_PROCEED_TO_COMMIT = 47
DFU_HOST_COMMIT_REQ = 49
DFU_HOST_COMMIT_CFM = 15
DFU_HOST_COMPLETE = 18
DFU_HOST_ERRORWARN_IND = 17
DFU_HOST_IS_CSR_VALID_DONE_REQ = 55
DFU_HOST_IS_CSR_VALID_DONE_CFM = 23
DFU_HOST_VERSION_REQ = 58
DFU_UPDATE_ID_KEYS = bytes([1, 2, 3, 4])

# Special scan frames used by the official BT11 page.  They do not use the
# normal feature/command framing and generate asynchronous input reports.
SCAN_START = bytes([0xFF, 0x03, 0x00, 0x01, 0x00, 0x29, 0x00, 0x07, 0x24])
SCAN_STOP = bytes([0xFF, 0x03, 0x00, 0x01, 0x00, 0x29, 0x00, 0x08, 0x24])

DEFAULT_DEVICE_LINK = "/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw"


class Bt11Error(RuntimeError):
    """A device, permission, protocol, or timeout error."""


@dataclasses.dataclass(frozen=True)
class PairingDevice:
    address: bytes
    name: str = ""
    connected: bool = False
    connect_type: int = 0
    profiles: bytes = b""

    @property
    def address_text(self) -> str:
        return ":".join(f"{byte:02X}" for byte in self.address)


@dataclasses.dataclass(frozen=True)
class ScanResult:
    address: bytes
    name: str
    rssi: int

    @property
    def address_text(self) -> str:
        return ":".join(f"{byte:02X}" for byte in self.address)


@dataclasses.dataclass(frozen=True)
class DeviceStatus:
    firmware: str
    name: str
    codecs: tuple[str, ...]
    unknown_codec_ids: tuple[int, ...]
    aptx_mode: Optional[int]
    ldac_mode: Optional[int]
    pairing_mode: int
    connected: bool
    connected_headsets: int
    connected_le: int
    brightness: int
    paired_devices: tuple[PairingDevice, ...]


def _sysfs_hidraw_info(path: str) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Return (vendor, product, interface) from a hidraw sysfs path."""

    name = os.path.basename(os.path.realpath(f"/sys/class/hidraw/{os.path.basename(path)}"))
    # The realpath ends in ...:<interface>/0003:VVVV:PPPP.NNNN/hidraw/hidrawX.
    target = os.path.realpath(f"/sys/class/hidraw/{name}/device")
    vendor = product = interface = None
    for component in target.split(os.sep):
        if component.startswith("0003:"):
            parts = component.split(":")
            if len(parts) >= 3:
                try:
                    vendor = int(parts[1], 16)
                    product = int(parts[2].split(".")[0], 16)
                except ValueError:
                    pass
        if ":1." in component:
            try:
                interface = int(component.rsplit(":1.", 1)[1])
            except ValueError:
                pass
    return vendor, product, interface


def find_device(explicit: Optional[str] = None) -> str:
    """Find the BT11 vendor HID interface, preferring interface 1."""

    if explicit:
        return explicit

    candidates: list[str] = []
    if os.path.exists(DEFAULT_DEVICE_LINK):
        candidates.append(DEFAULT_DEVICE_LINK)
    candidates.extend(sorted(glob.glob("/dev/input/by-id/*FIIO*BT11*-if01-hidraw")))
    candidates.extend(sorted(glob.glob("/dev/hidraw*")))

    seen: set[str] = set()
    fallback: list[str] = []
    for candidate in candidates:
        real = os.path.realpath(candidate)
        if real in seen or not os.path.exists(real):
            continue
        seen.add(real)
        vendor, product, interface = _sysfs_hidraw_info(real)
        if vendor == VENDOR_ID and product == PRODUCT_ID and interface == 1:
            return candidate
        if vendor == VENDOR_ID and product == PRODUCT_ID:
            fallback.append(candidate)
    if fallback:
        return fallback[0]
    raise Bt11Error(
        "未找到 FiiO BT11 的 vendor HID interface。请确认 BT11 已通过 USB 连接，"
        "并且使用的是 interface 1（通常是 /dev/input/by-id/*BT11*-if01-hidraw）。"
    )


def build_frame(feature: int, command: int, payload: bytes = b"") -> bytes:
    """Build the normal BT11 command frame used by FiiO's web driver."""

    if not 0 <= feature <= 0x7F or not 0 <= command <= 0xFF:
        raise ValueError("feature and command are out of range")
    if len(payload) > 0xFF:
        raise ValueError("payload is too large")
    return bytes([0xFF, 0x03, 0x00, len(payload), 0x00, 0x1D, feature << 1, command]) + payload


def _strip_report_id(raw: bytes) -> tuple[int, bytes]:
    """Return (report_id, data_without_report_id) for a hidraw input report."""

    if raw and raw[0] in INPUT_REPORT_IDS:
        return raw[0], raw[1:]
    return 0, raw


def _payload(data: bytes) -> bytes:
    if len(data) < 8 or data[0:3] != b"\xff\x03\x00":
        return b""
    length = data[3]
    start = 8
    end = min(len(data), start + length)
    return data[start:end]


def _decode_text(value: bytes) -> str:
    return value.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _parse_address(value: str) -> bytes:
    compact = value.replace(":", "").replace("-", "").strip()
    if len(compact) != 12:
        raise ValueError("蓝牙地址应为 12 个十六进制字符，例如 AA:BB:CC:DD:EE:FF")
    try:
        return bytes.fromhex(compact)
    except ValueError as exc:
        raise ValueError("蓝牙地址包含非法字符") from exc


class Bt11Hid:
    """Synchronous hidraw transport and BT11 command API."""

    def __init__(self, path: Optional[str] = None, timeout: float = 2.0):
        self.path = path or find_device()
        self.timeout = timeout
        self._fd: Optional[int] = None

    def __enter__(self) -> "Bt11Hid":
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def open(self) -> None:
        if self._fd is not None:
            return
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise Bt11Error(
                f"无法打开 {self.path}：权限不足。请安装项目 README 中的 udev 规则，"
                "或暂时以 root 运行 CLI 验证。"
            ) from exc
        except OSError as exc:
            raise Bt11Error(f"无法打开 {self.path}: {exc}") from exc

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _require_fd(self) -> int:
        if self._fd is None:
            self.open()
        assert self._fd is not None
        return self._fd

    def _send(self, frame: bytes) -> None:
        fd = self._require_fd()
        # hidraw includes the report number as byte 0 of a write buffer.
        report = bytes([REPORT_ID]) + frame
        try:
            written = os.write(fd, report)
        except OSError as exc:
            raise Bt11Error(f"写入 BT11 HID 失败: {exc}") from exc
        if written != len(report):
            raise Bt11Error(f"BT11 HID 短写：{written}/{len(report)} 字节")

    def _read(self, deadline: float) -> tuple[int, bytes]:
        fd = self._require_fd()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Bt11Error(f"等待 BT11 响应超时（命令通道 {self.path}）")
            try:
                readable, _, _ = select.select([fd], [], [], remaining)
            except OSError as exc:
                raise Bt11Error(f"等待 BT11 HID 输入失败: {exc}") from exc
            if not readable:
                raise Bt11Error(f"等待 BT11 响应超时（命令通道 {self.path}）")
            try:
                raw = os.read(fd, 2048)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise Bt11Error(f"读取 BT11 HID 失败: {exc}") from exc
            if not raw:
                continue
            return _strip_report_id(raw)

    def request(self, feature: int, command: int, payload: bytes = b"", timeout: Optional[float] = None) -> bytes:
        frame = build_frame(feature, command, payload)
        self._send(frame)
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            _, data = self._read(deadline)
            # Asynchronous scan/status reports must not be mistaken for the
            # response to a normal request.
            if len(data) < 9 or data[0:3] != b"\xff\x03\x00":
                continue
            # The response sets the low flag bit in the feature byte.  For
            # example, a CORE request uses 0x00 and returns 0x01; the BT11
            # app feature uses 0x30 and returns 0x31.
            if data[5] != 0x1D or data[6] not in (feature << 1, (feature << 1) | 1) or data[7] != command:
                continue
            return _payload(data)

    def send_special(self, frame: bytes) -> None:
        self._send(frame)

    def read_event(self, deadline: float) -> tuple[int, bytes]:
        return self._read(deadline)

    def get_firmware(self) -> str:
        return _decode_text(self.request(CORE_FEATURE, CMD_GET_FIRMWARE))

    def get_name(self) -> str:
        return _decode_text(self.request(APP_FEATURE, CMD_GET_LOCAL_NAME))

    def set_name(self, name: str) -> str:
        encoded = name.encode("utf-8")
        if len(encoded) > 32:
            raise ValueError("BT11 名称最多 32 个 UTF-8 字节")
        self.request(APP_FEATURE, CMD_SET_LOCAL_NAME, encoded)
        return self.get_name()

    def get_codecs(self) -> tuple[tuple[str, ...], tuple[int, ...]]:
        values = self.request(APP_FEATURE, CMD_GET_CODECS)
        names: list[str] = []
        unknown: list[int] = []
        for value in values:
            name = CODEC_NAMES_BY_ID.get(value)
            if name is None:
                unknown.append(value)
            elif name not in names:
                names.append(name)
        return tuple(names), tuple(unknown)

    def set_codecs(self, codecs: Iterable[str]) -> tuple[str, ...]:
        requested = set(codecs)
        invalid = requested - (set(USER_CODEC_NAMES) | {"sbc"})
        if invalid:
            if "lhdc" in invalid:
                raise ValueError("BT11 官方固件不支持 LHDC")
            raise ValueError(f"未知 codec: {', '.join(sorted(invalid))}")
        # Keep the same order as the official web driver.  SBC is not shown
        # by that UI, but preserve it when explicitly requested.
        order = ("ldac", "aptx-adaptive", "aptx-hd", "aptx", "aptx-ll", "lhdc", "sbc")
        payload = bytes(CODEC_IDS[name] for name in order if name in requested) + bytes([1, 0])
        self.request(APP_FEATURE, CMD_SET_CODECS, payload)
        return self.get_codecs()[0]

    def get_aptx_mode(self) -> Optional[int]:
        values = self.request(APP_FEATURE, CMD_GET_APTX_MODE)
        return values[0] if values else None

    def set_aptx_mode(self, mode: int) -> Optional[int]:
        if mode not in APTX_MODES:
            raise ValueError(f"aptX Adaptive 模式必须是: {', '.join(map(str, APTX_MODES))}")
        self.request(APP_FEATURE, CMD_SET_APTX_MODE, bytes([mode]))
        return self.get_aptx_mode()

    def get_ldac_mode(self) -> Optional[int]:
        values = self.request(APP_FEATURE, CMD_GET_LDAC_MODE)
        return values[0] if values else None

    def set_ldac_mode(self, mode: int) -> Optional[int]:
        if mode not in LDAC_MODES:
            raise ValueError(f"LDAC 模式必须是: {', '.join(map(str, LDAC_MODES))}")
        self.request(APP_FEATURE, CMD_SET_LDAC_MODE, bytes([mode]))
        return self.get_ldac_mode()

    def get_pairing_mode(self) -> int:
        values = self.request(APP_FEATURE, CMD_GET_PAIRING_MODE)
        if not values:
            raise Bt11Error("BT11 返回了空的配对模式")
        return values[0]

    def set_pairing_mode(self, mode: int) -> int:
        if mode not in PAIRING_MODES:
            raise ValueError("配对模式必须是 0（关闭）、1（自动）或 2（手动）")
        self.request(APP_FEATURE, CMD_SET_PAIRING_MODE, bytes([mode]))
        return self.get_pairing_mode()

    def get_connected_status(self) -> tuple[bool, int, int]:
        values = self.request(APP_FEATURE, CMD_GET_CONNECTED_STATUS)
        values += bytes(max(0, 3 - len(values)))
        return values[0] == 1, values[1], values[2]

    def get_paired_devices(self, resolve_names: bool = True) -> list[PairingDevice]:
        values = self.request(APP_FEATURE, CMD_GET_PAIRED_LIST)
        if not values:
            return []
        count = values[0]
        devices: list[PairingDevice] = []
        for index in range(count):
            start = 1 + index * 12
            record = values[start : start + 12]
            if len(record) < 12:
                break
            address = bytes(record[1:7])
            profiles = bytes(record[8:12])
            if not any(profiles):
                continue
            device = PairingDevice(
                address=address,
                connected=record[7] == 128,
                connect_type=record[0],
                profiles=profiles,
            )
            if resolve_names:
                try:
                    device = dataclasses.replace(device, name=self.get_remote_name(address))
                except Bt11Error:
                    # A paired device can temporarily reject a name query;
                    # keeping its address is more useful than dropping it.
                    pass
            devices.append(device)
        return devices

    def get_remote_name(self, address: bytes) -> str:
        if len(address) != 6:
            raise ValueError("蓝牙地址必须是 6 字节")
        return _decode_text(self.request(APP_FEATURE, CMD_GET_REMOTE_NAME, bytes([0]) + address + bytes([0])))

    def connect_device(self, address: bytes) -> None:
        self.request(APP_FEATURE, CMD_CONNECT_DEVICE, bytes([0]) + address + bytes([0]))

    def disconnect_device(self, address: bytes) -> None:
        self.request(APP_FEATURE, CMD_DISCONNECT_DEVICE, bytes([0]) + address + bytes([0]))

    def pair_device(self, address: bytes) -> None:
        self.request(APP_FEATURE, CMD_PAIR_DEVICE, bytes([0]) + address + bytes([0]))

    def delete_paired_device(self, address: bytes) -> None:
        self.request(APP_FEATURE, CMD_DELETE_PAIRED_DEVICE, bytes([0]) + address + bytes([0]))

    def delete_all_paired(self) -> None:
        self.request(APP_FEATURE, CMD_DELETE_ALL_PAIRED)

    def reset_settings(self) -> None:
        self.request(APP_FEATURE, CMD_RESET_SETTINGS)

    def get_brightness(self) -> int:
        values = self.request(APP_FEATURE, CMD_GET_BRIGHTNESS)
        if not values:
            raise Bt11Error("BT11 返回了空的亮度值")
        return values[0]

    def set_brightness(self, brightness: int) -> int:
        if not 0 <= brightness <= 7:
            raise ValueError("BT11 指示灯亮度范围是 0-7")
        self.request(APP_FEATURE, CMD_SET_BRIGHTNESS, bytes([brightness]))
        return self.get_brightness()

    def scan(self, seconds: float = 3.0) -> list[ScanResult]:
        if seconds <= 0:
            raise ValueError("扫描时间必须为正数")
        found: dict[bytes, ScanResult] = {}
        original_pairing_mode = self.get_pairing_mode()
        # The official page enables manual pairing around a scan.  Without
        # this transition, newer firmware acknowledges SCAN_START but emits
        # no discovery reports while pairing is closed.
        if original_pairing_mode == 0:
            self.set_pairing_mode(0)
            self.set_pairing_mode(2)
        self.send_special(SCAN_START)
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                try:
                    _, data = self.read_event(deadline)
                except Bt11Error:
                    break
                if len(data) < 8:
                    continue
                event = data[7]
                payload = _payload(data)
                if event == 0x81:
                    # This follows the official web driver's report layout.
                    if len(payload) >= 20:
                        address = bytes(payload[9:15])
                        rssi = int.from_bytes(payload[16:18], "little", signed=False)
                        name_length = int.from_bytes(payload[18:20], "little")
                        name = _decode_text(payload[20 : 20 + name_length])
                        if len(address) == 6:
                            found[address] = ScanResult(address, name, rssi)
                elif event == 0x83:
                    # The paired-list notification can be ignored here; the
                    # normal query is used after scanning to get authoritative
                    # names and connection flags.
                    continue
        finally:
            self.send_special(SCAN_STOP)
            # Firmware 1.1.x emits a scan-stop acknowledgement before it is
            # ready to accept the next normal command.  The official page
            # relies on its serialized command queue; hidraw needs the small
            # equivalent delay explicitly.
            time.sleep(0.25)
            if original_pairing_mode != 2:
                last_error = None
                for _ in range(2):
                    try:
                        self.set_pairing_mode(original_pairing_mode)
                        last_error = None
                        break
                    except Bt11Error as exc:
                        last_error = exc
                        time.sleep(0.25)
                if last_error is not None:
                    raise last_error
        return sorted(found.values(), key=lambda item: (item.name, item.address))

    def status(self) -> DeviceStatus:
        firmware = self.get_firmware()
        name = self.get_name()
        codecs, unknown = self.get_codecs()
        aptx_mode = self.get_aptx_mode()
        ldac_mode = self.get_ldac_mode()
        pairing_mode = self.get_pairing_mode()
        connected, headset_count, le_count = self.get_connected_status()
        brightness = self.get_brightness()
        paired = tuple(self.get_paired_devices())
        return DeviceStatus(
            firmware=firmware,
            name=name,
            codecs=codecs,
            unknown_codec_ids=unknown,
            aptx_mode=aptx_mode,
            ldac_mode=ldac_mode,
            pairing_mode=pairing_mode,
            connected=connected,
            connected_headsets=headset_count,
            connected_le=le_count,
            brightness=brightness,
            paired_devices=paired,
        )

    def self_test(self) -> list[str]:
        """Round-trip every non-destructive setting without changing values."""

        before = self.status()
        results = ["读取全部状态: PASS"]

        if before.name:
            self.set_name(before.name)
            results.append("设备名写入/读回: PASS")
        self.set_brightness(before.brightness)
        results.append("指示灯亮度写入/读回: PASS")

        if before.aptx_mode in APTX_MODES:
            self.set_aptx_mode(before.aptx_mode)
            results.append("aptX Adaptive 模式写入/读回: PASS")
        else:
            results.append("aptX Adaptive 模式写入/读回: SKIP（设备未返回已知模式）")

        if before.ldac_mode in LDAC_MODES:
            self.set_ldac_mode(before.ldac_mode)
            results.append("LDAC 模式写入/读回: PASS")
        else:
            results.append("LDAC 模式写入/读回: SKIP（设备未返回已知模式）")

        self.set_pairing_mode(before.pairing_mode)
        results.append("配对模式写入/读回: PASS")

        # SBC and trailing zero are reported by the device as baseline/
        # reserved entries; the official web UI does not send them as user
        # checkboxes.  Preserve the visible selectable codec set.
        selectable = [name for name in before.codecs if name in USER_CODEC_NAMES]
        self.set_codecs(selectable)
        results.append("codec 选择写入/读回: PASS")

        after = self.status()
        if after.name != before.name:
            raise Bt11Error("自检失败：设备名写入后不一致")
        if after.brightness != before.brightness:
            raise Bt11Error("自检失败：亮度写入后不一致")
        if after.aptx_mode != before.aptx_mode:
            raise Bt11Error("自检失败：aptX Adaptive 模式写入后不一致")
        if after.ldac_mode != before.ldac_mode:
            raise Bt11Error("自检失败：LDAC 模式写入后不一致")
        if after.pairing_mode != before.pairing_mode:
            raise Bt11Error("自检失败：配对模式写入后不一致")
        if not set(selectable).issubset(set(after.codecs)):
            raise Bt11Error("自检失败：codec 选择写入后不一致")
        results.append("最终状态复核: PASS")
        return results


def _hid_ioctl(direction: int, number: int, size: int) -> int:
    """Build Linux's _IOC number for HIDIOCSFEATURE/HIDIOCGFEATURE."""

    return (direction << 30) | (ord("H") << 8) | number | (size << 16)


class Bt11FirmwareUpdater:
    """The BT11's DFU transport, kept separate from normal settings HID."""

    def __init__(self, transport: Bt11Hid, timeout: float = 180.0):
        self.transport = transport
        self.timeout = timeout
        self.fd = transport._require_fd()

    @staticmethod
    def _command_id(command: int) -> int:
        value = abs(command - 33)
        return value // 2 if value > 128 else value

    def _send_feature(self, command: int, payload: bytes = b"") -> None:
        if len(payload) > DFU_FEATURE_LENGTH - 2:
            raise Bt11Error("DFU feature payload 过大")
        data = bytearray(DFU_FEATURE_LENGTH)
        data[0] = 1 + len(payload)
        data[1] = command
        data[2 : 2 + len(payload)] = payload
        report = bytearray([DFU_FEATURE_REPORT_ID]) + data
        ioctl_number = _hid_ioctl(3, 0x06, len(report))  # _IOC_READ|_IOC_WRITE
        try:
            fcntl.ioctl(self.fd, ioctl_number, report, True)
        except OSError as exc:
            raise Bt11Error(f"写入 BT11 DFU feature report 失败: {exc}") from exc

    def _send_output(self, command: int, payload: bytes = b"") -> None:
        if len(payload) > DFU_OUTPUT_LENGTH - 4:
            raise Bt11Error("DFU output payload 过大")
        data = bytearray(DFU_OUTPUT_LENGTH)
        data[0] = 3 + len(payload)
        data[1] = self._command_id(command)
        data[2] = (len(payload) >> 8) & 0xFF
        data[3] = len(payload) & 0xFF
        data[4 : 4 + len(payload)] = payload
        report = bytes([DFU_OUTPUT_REPORT_ID]) + data
        try:
            written = os.write(self.fd, report)
        except OSError as exc:
            raise Bt11Error(f"写入 BT11 DFU output report 失败: {exc}") from exc
        if written != len(report):
            raise Bt11Error(f"BT11 DFU 短写：{written}/{len(report)} 字节")

    def _next_event(self, deadline: float) -> tuple[int, bytes]:
        while True:
            report_id, data = self.transport.read_event(deadline)
            if report_id != DFU_INPUT_REPORT_ID or len(data) < 2:
                continue
            payload_length = data[0]
            command = data[1]
            # The official WebHID implementation uses data.slice(4) rather
            # than trusting the first length byte; hidraw reports also carry
            # zero-filled trailing bytes, so preserve the same layout.
            _ = payload_length
            return command, data[4:]

    def probe(self, timeout: float = 4.0) -> list[tuple[int, bytes]]:
        """Perform the non-destructive DFU handshake and return input events."""

        self._send_feature(2)
        self._send_output(DFU_HOST_VERSION_REQ)
        deadline = time.monotonic() + timeout
        events: list[tuple[int, bytes]] = []
        while time.monotonic() < deadline:
            try:
                events.append(self._next_event(deadline))
            except Bt11Error:
                break
        return events

    def _send_sync(self) -> None:
        self._send_output(DFU_HOST_SYNC_REQ, DFU_UPDATE_ID_KEYS)

    def _reopen_after_reboot(self, timeout: float = 30.0) -> None:
        """Rebind hidraw after BT11 disappears and re-enumerates in DFU."""

        self.transport.close()
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                self.transport.open()
                self.fd = self.transport._require_fd()
                return
            except Bt11Error as exc:
                last_error = exc
                time.sleep(0.25)
        raise Bt11Error(f"BT11 重启后 HID 未重新出现: {last_error}")

    def update(self, firmware: bytes, progress=None) -> None:
        """Run the official BT11 host-upgrade state machine.

        This method is intentionally not called by status/self-test. Firmware
        flashing is irreversible while in progress and must be explicitly
        requested by the CLI or confirmed in the GUI.
        """

        if not firmware:
            raise Bt11Error("固件文件为空")
        self._send_feature(2)
        self._send_feature(DFU_MSG_DEVICE_INFO_REQ, os.urandom(4))
        self._send_output(DFU_HOST_VERSION_REQ)
        time.sleep(0.2)

        index = 0
        state = "init"
        self._send_sync()
        deadline = time.monotonic() + self.timeout

        def report_progress() -> None:
            if progress:
                progress(index, len(firmware), state)

        while time.monotonic() < deadline:
            try:
                command, payload = self._next_event(deadline)
            except Bt11Error as exc:
                # A successful transfer deliberately disconnects USB while
                # the new image boots.  The browser implementation listens
                # for navigator.hid.onconnect; hidraw must reopen the node.
                if state == "rebooting" and any(
                    marker in str(exc) for marker in ("I/O", "Input/output error")
                ):
                    self._reopen_after_reboot()
                    time.sleep(3.0)
                    state = "rebooted"
                    self._send_feature(2)
                    self._send_sync()
                    continue
                raise

            if command == DFU_HOST_ERRORWARN_IND:
                code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else -1
                # The official FiiO updater treats 129 (and the older 35)
                # as a stale/rescue session: abort it, resynchronize, and
                # continue.  They are not failures before data transfer.
                if code in {129, 35} and state in {"init", "starting", "restarting"}:
                    state = "starting"
                    time.sleep(0.3)
                    self._send_output(DFU_HOST_ABORT_REQ)
                    time.sleep(3.0)
                    self._send_sync()
                    continue
                raise Bt11Error(f"BT11 DFU 返回错误码 {code}")

            if command == DFU_HOST_SYNC_CFM:
                if len(payload) < 5 or payload[1:5] != DFU_UPDATE_ID_KEYS:
                    raise Bt11Error("BT11 DFU update ID 不匹配")
                result = payload[0]
                if state == "init":
                    state = "starting"
                    self._send_output(DFU_HOST_ABORT_REQ)
                elif state == "starting":
                    self._send_output(DFU_HOST_START_REQ)
                elif state == "rebooted":
                    if result != 3:
                        raise Bt11Error(f"BT11 DFU 重启后同步失败，状态码 {result}")
                    state = "confirming"
                    self._send_output(DFU_HOST_START_REQ)
                elif state == "upgrade-check":
                    if result == 0:
                        state = "success"
                        report_progress()
                        return
                    if result == 3:
                        state = "confirming"
                        self._send_output(DFU_HOST_START_REQ)
                        continue
                    raise Bt11Error(f"BT11 DFU 校验失败，状态码 {result}")
                else:
                    raise Bt11Error(f"BT11 DFU 在状态 {state} 收到意外同步响应")
                report_progress()
                continue

            if command == DFU_HOST_ABORT_CFM:
                if state in {"starting", "restarting"}:
                    self._send_sync()
                continue

            if command == DFU_HOST_START_CFM:
                if state == "starting":
                    state = "started"
                    self._send_output(DFU_HOST_START_DATA_REQ)
                elif state == "confirming":
                    self._send_output(DFU_HOST_PROCEED_TO_COMMIT, bytes([0]))
                continue

            if command == DFU_HOST_DATA_BYTES_CFM:
                if len(payload) < 4:
                    raise Bt11Error("BT11 DFU 数据长度响应不完整")
                requested = int.from_bytes(payload[:4], "big")
                if requested <= 0:
                    raise Bt11Error("BT11 DFU 返回了无效的数据长度")
                state = "transferring"
                remaining = min(requested, len(firmware) - index)
                while remaining:
                    amount = min(remaining, DFU_CHUNK_SIZE)
                    end = index + amount
                    packet = bytes([1 if end == len(firmware) else 0]) + firmware[index:end]
                    self._send_output(DFU_HOST_DATA, packet)
                    index = end
                    remaining -= amount
                    report_progress()
                if index == len(firmware):
                    state = "transfer-complete"
                    time.sleep(1.0)
                    self._send_output(DFU_HOST_IS_CSR_VALID_DONE_REQ)
                continue

            if command == DFU_HOST_IS_CSR_VALID_DONE_CFM:
                state = "validating"
                time.sleep(2.0)
                self._send_output(DFU_HOST_IS_CSR_VALID_DONE_REQ)
                continue

            if command == DFU_HOST_TRANSFER_COMPLETE_IND:
                state = "rebooting"
                self._send_output(DFU_HOST_TRANSFER_COMPLETE_RES, bytes([0]))
                report_progress()
                continue

            if command == DFU_MSG_DEVICE_INFO_CFM and state == "rebooting":
                state = "rebooted"
                time.sleep(3.0)
                self._send_feature(2)
                self._send_sync()
                continue

            if command == DFU_HOST_COMMIT_CFM:
                state = "upgrade-check"
                self._send_output(DFU_HOST_COMMIT_REQ, bytes([0]))
                continue

            if command == DFU_HOST_COMPLETE:
                state = "success"
                report_progress()
                return

        raise Bt11Error(f"BT11 DFU 超时，最后状态为 {state}，已发送 {index}/{len(firmware)} 字节")


def _format_codecs(names: Iterable[str], unknown: Iterable[int] = ()) -> str:
    values = [CODEC_LABELS.get(name, name) for name in names]
    values.extend(f"unknown(0x{value:02x})" for value in unknown)
    return ", ".join(values) if values else "（设备未返回可选 codec）"


def _print_status(status: DeviceStatus) -> None:
    print(f"设备名: {status.name or '（未设置）'}")
    print(f"固件: {status.firmware or '（设备未返回）'}")
    print(f"codec: {_format_codecs(status.codecs, status.unknown_codec_ids)}")
    aptx_text = "未知" if status.aptx_mode is None else f"{status.aptx_mode} ({APTX_MODES.get(status.aptx_mode, '未知')})"
    ldac_text = "未知" if status.ldac_mode is None else f"{status.ldac_mode} ({LDAC_MODES.get(status.ldac_mode, '未知')})"
    print(f"aptX Adaptive 模式: {aptx_text}")
    print(f"LDAC 模式: {ldac_text}")
    print(f"配对模式: {status.pairing_mode} ({PAIRING_MODES.get(status.pairing_mode, '未知')})")
    print(
        f"连接状态: {'已连接' if status.connected else '未连接'}; "
        f"经典蓝牙设备 {status.connected_headsets}; LE 设备 {status.connected_le}"
    )
    print(f"指示灯亮度: {status.brightness}/7")
    print("已配对设备:")
    if not status.paired_devices:
        print("  （无）")
    for device in status.paired_devices:
        state = "connected" if device.connected else "disconnected"
        print(f"  {device.address_text}  {device.name or '（名称不可用）'}  [{state}]")


def _run_cli(args: argparse.Namespace) -> int:
    if args.command == "gui":
        run_gui(args.device)
        return 0

    try:
        with Bt11Hid(args.device) as device:
            if args.command == "status":
                _print_status(device.status())
            elif args.command == "firmware":
                print(device.get_firmware())
            elif args.command == "firmware-probe":
                events = Bt11FirmwareUpdater(device).probe(args.seconds)
                for command, payload in events:
                    print(f"report-6 command=0x{command:02x} payload={payload.hex()}")
            elif args.command == "firmware-update":
                if not args.yes:
                    raise Bt11Error("固件刷写会重启 BT11，请确认固件来源后添加 --yes")
                with open(args.file, "rb") as firmware_file:
                    firmware = firmware_file.read()

                def progress(done, total, state):
                    print(f"{state}: {done}/{total}", flush=True)

                Bt11FirmwareUpdater(device, timeout=args.timeout).update(firmware, progress)
                print("firmware update complete")
            elif args.command == "name":
                print(device.set_name(args.value) if args.value is not None else device.get_name())
            elif args.command == "brightness":
                print(device.set_brightness(args.value) if args.value is not None else device.get_brightness())
            elif args.command == "aptx-mode":
                print(device.set_aptx_mode(args.value) if args.value is not None else device.get_aptx_mode())
            elif args.command == "ldac-mode":
                print(device.set_ldac_mode(args.value) if args.value is not None else device.get_ldac_mode())
            elif args.command == "pairing":
                print(device.set_pairing_mode(args.value) if args.value is not None else device.get_pairing_mode())
            elif args.command == "codecs":
                if args.value is None:
                    names, unknown = device.get_codecs()
                    print(_format_codecs(names, unknown))
                else:
                    print(_format_codecs(device.set_codecs(args.value)))
            elif args.command == "devices":
                for item in device.get_paired_devices():
                    print(f"{item.address_text}\t{item.name}\t{'connected' if item.connected else 'disconnected'}")
            elif args.command in {"connect", "disconnect", "pair", "forget"}:
                address = _parse_address(args.address)
                operation = {
                    "connect": device.connect_device,
                    "disconnect": device.disconnect_device,
                    "pair": device.pair_device,
                    "forget": device.delete_paired_device,
                }[args.command]
                operation(address)
                print("ok")
            elif args.command == "scan":
                for item in device.scan(args.seconds):
                    print(f"{item.address_text}\t{item.rssi}\t{item.name}")
            elif args.command == "self-test":
                for result in device.self_test():
                    print(result)
            elif args.command == "forget-all":
                if not args.yes:
                    raise Bt11Error("forget-all 会清除所有 BT11 配对记录，请添加 --yes")
                device.delete_all_paired()
                print("ok")
            elif args.command == "reset":
                if not args.yes:
                    raise Bt11Error("reset 会恢复 BT11 设置，请添加 --yes")
                device.reset_settings()
                print("ok")
    except (Bt11Error, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0


class Bt11Gui:
    def __init__(self, root: tk.Tk, device_path: Optional[str]):
        self.root = root
        self.root.title("FiiO BT11 Linux Control")
        self.root.geometry("900x720")
        self.device_path = device_path
        self.worker_lock = threading.Lock()
        self.name_var = tk.StringVar()
        self.firmware_file_var = tk.StringVar()
        self.brightness_var = tk.IntVar(value=0)
        self.pairing_var = tk.StringVar(value="closed")
        self.aptx_mode_var = tk.StringVar()
        self.ldac_mode_var = tk.StringVar()
        self.status_var = tk.StringVar(value="未连接")
        self.firmware_var = tk.StringVar(value="-")
        self.connection_var = tk.StringVar(value="-")
        self.codec_vars = {name: tk.BooleanVar(value=False) for name in CODEC_LABELS if name != "sbc"}
        self.codec_raw_var = tk.StringVar(value="")
        self.paired: list[PairingDevice] = []
        self.scanned: list[ScanResult] = []
        self._build()
        self.root.after(100, self.refresh)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="BT11 HID:").pack(side="left")
        self.path_entry = ttk.Entry(header, width=65)
        self.path_entry.insert(0, self.device_path or "自动发现")
        self.path_entry.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(header, text="刷新", command=self.refresh).pack(side="left")
        ttk.Label(outer, textvariable=self.status_var, foreground="#b03030").pack(anchor="w", pady=(8, 4))

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        info = ttk.LabelFrame(content, text="设备")
        info.pack(fill="x", pady=5)
        ttk.Label(info, text="名称").grid(row=0, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(info, textvariable=self.name_var, width=32).grid(row=0, column=1, sticky="w", padx=6, pady=5)
        ttk.Button(info, text="写入名称", command=lambda: self.run(self._set_name)).grid(row=0, column=2, padx=6, pady=5)
        ttk.Label(info, text="固件").grid(row=1, column=0, sticky="w", padx=6, pady=5)
        ttk.Label(info, textvariable=self.firmware_var).grid(row=1, column=1, sticky="w", padx=6, pady=5)
        ttk.Label(info, text="连接").grid(row=2, column=0, sticky="w", padx=6, pady=5)
        ttk.Label(info, textvariable=self.connection_var).grid(row=2, column=1, sticky="w", padx=6, pady=5)
        ttk.Label(info, text="亮度（0-7）").grid(row=3, column=0, sticky="w", padx=6, pady=5)
        ttk.Scale(info, from_=0, to=7, orient="horizontal", command=lambda value: self.brightness_var.set(round(float(value)))).grid(row=3, column=1, sticky="ew", padx=6, pady=5)
        ttk.Label(info, textvariable=self.brightness_var, width=3).grid(row=3, column=2, sticky="w", padx=6)
        ttk.Button(info, text="写入亮度", command=lambda: self.run(self._set_brightness)).grid(row=3, column=3, padx=6, pady=5)
        ttk.Label(info, text="固件文件").grid(row=4, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(info, textvariable=self.firmware_file_var, width=42).grid(row=4, column=1, sticky="ew", padx=6, pady=5)
        ttk.Button(info, text="选择", command=self._choose_firmware).grid(row=4, column=2, padx=6, pady=5)
        ttk.Button(info, text="刷写固件", command=self._confirm_firmware_update).grid(row=4, column=3, padx=6, pady=5)
        info.columnconfigure(1, weight=1)

        codecs = ttk.LabelFrame(content, text="Bluetooth 编码器")
        codecs.pack(fill="x", pady=5)
        codec_names = USER_CODEC_NAMES
        for index, name in enumerate(codec_names):
            ttk.Checkbutton(codecs, text=CODEC_LABELS[name], variable=self.codec_vars[name]).grid(
                row=index // 3, column=index % 3, sticky="w", padx=8, pady=4
            )
        ttk.Label(codecs, text="设备原始返回").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(codecs, textvariable=self.codec_raw_var).grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Label(codecs, text="aptX Adaptive 模式").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        self.aptx_combo = ttk.Combobox(codecs, textvariable=self.aptx_mode_var, state="readonly", width=24)
        self.aptx_combo["values"] = [f"{value}: {label}" for value, label in APTX_MODES.items()]
        self.aptx_combo.grid(row=3, column=1, sticky="w", padx=8, pady=4)
        ttk.Button(codecs, text="写入 aptX 模式", command=lambda: self.run(self._set_aptx)).grid(row=3, column=2, padx=8, pady=4)
        ttk.Label(codecs, text="LDAC 模式").grid(row=4, column=0, sticky="w", padx=8, pady=4)
        self.ldac_combo = ttk.Combobox(codecs, textvariable=self.ldac_mode_var, state="readonly", width=24)
        self.ldac_combo["values"] = [f"{value}: {label}" for value, label in LDAC_MODES.items()]
        self.ldac_combo.grid(row=4, column=1, sticky="w", padx=8, pady=4)
        ttk.Button(codecs, text="写入 LDAC 模式", command=lambda: self.run(self._set_ldac)).grid(row=4, column=2, padx=8, pady=4)
        ttk.Button(codecs, text="写入编码器选择", command=lambda: self.run(self._set_codecs)).grid(row=5, column=0, padx=8, pady=5)

        pairing = ttk.LabelFrame(content, text="配对与设备")
        pairing.pack(fill="both", expand=True, pady=5)
        ttk.Label(pairing, text="配对模式").grid(row=0, column=0, sticky="w", padx=6, pady=5)
        self.pairing_combo = ttk.Combobox(pairing, textvariable=self.pairing_var, state="readonly", values=list(PAIRING_MODE_IDS), width=18)
        self.pairing_combo.grid(row=0, column=1, sticky="w", padx=6, pady=5)
        ttk.Button(pairing, text="写入配对模式", command=lambda: self.run(self._set_pairing)).grid(row=0, column=2, padx=6, pady=5)
        ttk.Button(pairing, text="刷新已配对列表", command=lambda: self.run(self._load_devices)).grid(row=0, column=3, padx=6, pady=5)
        self.device_tree = ttk.Treeview(pairing, columns=("address", "name", "state"), show="headings", height=6)
        for column, heading, width in (("address", "地址", 150), ("name", "名称", 280), ("state", "状态", 100)):
            self.device_tree.heading(column, text=heading)
            self.device_tree.column(column, width=width)
        self.device_tree.grid(row=1, column=0, columnspan=4, sticky="nsew", padx=6, pady=5)
        buttons = ttk.Frame(pairing)
        buttons.grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=5)
        ttk.Button(buttons, text="连接选中", command=lambda: self.run(self._connect_selected)).pack(side="left", padx=3)
        ttk.Button(buttons, text="断开选中", command=lambda: self.run(self._disconnect_selected)).pack(side="left", padx=3)
        ttk.Button(buttons, text="删除选中", command=lambda: self.run(self._forget_selected)).pack(side="left", padx=3)
        ttk.Button(buttons, text="扫描附近设备", command=lambda: self.run(self._scan)).pack(side="left", padx=3)
        ttk.Button(buttons, text="清除所有配对", command=self._forget_all).pack(side="left", padx=3)
        ttk.Button(buttons, text="恢复设置", command=self._reset).pack(side="left", padx=3)
        self.scan_list = tk.Listbox(pairing, height=6)
        self.scan_list.grid(row=3, column=0, columnspan=4, sticky="nsew", padx=6, pady=5)
        ttk.Button(pairing, text="配对并连接扫描结果", command=lambda: self.run(self._pair_scanned)).grid(row=4, column=0, padx=6, pady=5, sticky="w")
        pairing.columnconfigure(1, weight=1)
        pairing.rowconfigure(1, weight=1)
        pairing.rowconfigure(3, weight=1)

    def _path(self) -> Optional[str]:
        value = self.path_entry.get().strip()
        return None if not value or value == "自动发现" else value

    def run(self, callback) -> None:
        if not self.worker_lock.acquire(blocking=False):
            return
        self.status_var.set("正在通信…")

        def worker() -> None:
            try:
                result = callback()
                self.root.after(0, lambda: self._done(result))
            except Exception as exc:  # GUI should display protocol errors, not crash.
                self.root.after(0, lambda: self._error(exc))
            finally:
                self.worker_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, result=None) -> None:
        self.status_var.set("就绪")
        if isinstance(result, DeviceStatus):
            self._show_status(result)
        elif isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
            kind, value = result
            if kind == "name":
                self.name_var.set(value)
            elif kind == "brightness":
                self.brightness_var.set(value)
            elif kind == "aptx":
                self.aptx_mode_var.set(self._mode_text(value, APTX_MODES))
            elif kind == "ldac":
                self.ldac_mode_var.set(self._mode_text(value, LDAC_MODES))
            elif kind == "pairing":
                self.pairing_var.set(PAIRING_MODES.get(value, str(value)))
            elif kind == "codecs":
                for name, variable in self.codec_vars.items():
                    variable.set(name in value)
                self.codec_raw_var.set(_format_codecs(value))
            elif kind == "firmware":
                self.status_var.set("固件刷写完成；请重新读取设备状态")
        elif isinstance(result, list) and (not result or isinstance(result[0], PairingDevice)):
            self._show_devices(result)
        elif isinstance(result, list) and (not result or isinstance(result[0], ScanResult)):
            self._show_scan(result)

    def _error(self, exc: Exception) -> None:
        self.status_var.set(f"错误：{exc}")
        messagebox.showerror("BT11", str(exc), parent=self.root)

    def refresh(self) -> None:
        self.run(self._read_status)

    def _read_status(self) -> DeviceStatus:
        with Bt11Hid(self._path()) as device:
            return device.status()

    def _show_status(self, status: DeviceStatus) -> None:
        self.name_var.set(status.name)
        self.firmware_var.set(status.firmware or "未知")
        self.connection_var.set(
            f"{'已连接' if status.connected else '未连接'}；经典 {status.connected_headsets}，LE {status.connected_le}"
        )
        self.brightness_var.set(status.brightness)
        self.paired = list(status.paired_devices)
        self._show_devices(self.paired)
        for name, variable in self.codec_vars.items():
            variable.set(name in status.codecs)
        self.codec_raw_var.set(_format_codecs(status.codecs, status.unknown_codec_ids))
        self.aptx_mode_var.set(self._mode_text(status.aptx_mode, APTX_MODES))
        self.ldac_mode_var.set(self._mode_text(status.ldac_mode, LDAC_MODES))
        self.pairing_var.set(PAIRING_MODES.get(status.pairing_mode, str(status.pairing_mode)))

    @staticmethod
    def _mode_text(value: Optional[int], mapping: dict[int, str]) -> str:
        return "" if value is None else f"{value}: {mapping.get(value, '未知')}"

    def _with_device(self, operation):
        with Bt11Hid(self._path()) as device:
            return operation(device)

    def _set_name(self):
        return ("name", self._with_device(lambda device: device.set_name(self.name_var.get())))

    def _set_brightness(self):
        return ("brightness", self._with_device(lambda device: device.set_brightness(self.brightness_var.get())))

    def _set_codecs(self):
        names = [name for name, variable in self.codec_vars.items() if variable.get()]
        return ("codecs", self._with_device(lambda device: device.set_codecs(names)))

    def _set_aptx(self):
        value = int(self.aptx_mode_var.get().split(":", 1)[0])
        return ("aptx", self._with_device(lambda device: device.set_aptx_mode(value)))

    def _set_ldac(self):
        value = int(self.ldac_mode_var.get().split(":", 1)[0])
        return ("ldac", self._with_device(lambda device: device.set_ldac_mode(value)))

    def _set_pairing(self):
        value = PAIRING_MODE_IDS[self.pairing_var.get()]
        return ("pairing", self._with_device(lambda device: device.set_pairing_mode(value)))

    def _choose_firmware(self):
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择官方 BT11.bin",
            filetypes=(("BT11 firmware", "*.bin"), ("All files", "*")),
        )
        if path:
            self.firmware_file_var.set(path)

    def _confirm_firmware_update(self):
        path = self.firmware_file_var.get().strip()
        if not path:
            messagebox.showerror("BT11", "请先选择官方 BT11.bin 文件", parent=self.root)
            return
        if messagebox.askyesno(
            "确认刷写固件",
            "刷写期间不要拔出 BT11。请确认文件来自 FiiO 官方，并继续吗？",
            parent=self.root,
        ):
            self.run(lambda: self._update_firmware(path))

    def _update_firmware(self, path: str):
        with open(path, "rb") as firmware_file:
            firmware = firmware_file.read()
        with Bt11Hid(self._path()) as device:
            Bt11FirmwareUpdater(device).update(firmware)
        return ("firmware", True)

    def _load_devices(self):
        return self._with_device(lambda device: device.get_paired_devices())

    def _show_devices(self, devices: Sequence[PairingDevice]) -> None:
        self.paired = list(devices)
        for item in self.device_tree.get_children():
            self.device_tree.delete(item)
        for index, device in enumerate(self.paired):
            self.device_tree.insert("", "end", iid=str(index), values=(device.address_text, device.name, "已连接" if device.connected else "未连接"))

    def _selected_device(self) -> PairingDevice:
        selected = self.device_tree.selection()
        if not selected:
            raise Bt11Error("请先在已配对列表中选择设备")
        return self.paired[int(selected[0])]

    def _connect_selected(self):
        device_info = self._selected_device()
        return self._with_device(lambda device: device.connect_device(device_info.address))

    def _disconnect_selected(self):
        device_info = self._selected_device()
        return self._with_device(lambda device: device.disconnect_device(device_info.address))

    def _forget_selected(self):
        device_info = self._selected_device()
        if not messagebox.askyesno("确认", f"删除配对记录 {device_info.name or device_info.address_text}？", parent=self.root):
            return None
        return self._with_device(lambda device: device.delete_paired_device(device_info.address))

    def _scan(self):
        return self._with_device(lambda device: device.scan(3.0))

    def _show_scan(self, results: Sequence[ScanResult]) -> None:
        self.scanned = list(results)
        self.scan_list.delete(0, tk.END)
        for item in self.scanned:
            self.scan_list.insert(tk.END, f"{item.address_text}  RSSI {item.rssi}  {item.name or '（无名称）'}")

    def _pair_scanned(self):
        selected = self.scan_list.curselection()
        if not selected:
            raise Bt11Error("请先在扫描结果中选择设备")
        info = self.scanned[selected[0]]

        def operation(device: Bt11Hid):
            device.pair_device(info.address)
            device.connect_device(info.address)

        return self._with_device(operation)

    def _forget_all(self):
        if messagebox.askyesno("确认", "这会清除 BT11 的全部配对记录，继续吗？", parent=self.root):
            self.run(lambda: self._with_device(lambda device: device.delete_all_paired()))

    def _reset(self):
        if messagebox.askyesno("确认", "这会恢复 BT11 设置，继续吗？", parent=self.root):
            self.run(lambda: self._with_device(lambda device: device.reset_settings()))


def run_gui(device_path: Optional[str] = None) -> None:
    global tk, filedialog, messagebox, ttk
    if tk is None or filedialog is None or messagebox is None or ttk is None:
        try:
            import tkinter as tk_module
            from tkinter import filedialog as filedialog_module
            from tkinter import messagebox as messagebox_module
            from tkinter import ttk as ttk_module
        except ImportError as exc:
            raise Bt11Error(
                "当前 Python 没有 Tk 图形扩展。NixOS 可使用 "
                "`nix shell nixpkgs#python3Packages.tkinter` 后再启动 GUI。"
            ) from exc
        tk = tk_module
        filedialog = filedialog_module
        messagebox = messagebox_module
        ttk = ttk_module
    root = tk.Tk()
    Bt11Gui(root, device_path)
    root.mainloop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FiiO BT11 Linux control panel")
    parser.add_argument("--device", help="BT11 vendor HID device path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gui", help="启动 Tk 图形界面")
    sub.add_parser("status", help="读取全部 BT11 状态")
    sub.add_parser("firmware", help="读取固件版本")
    probe = sub.add_parser("firmware-probe", help="执行无写入的 DFU 通道探测")
    probe.add_argument("--seconds", type=float, default=4.0)
    update = sub.add_parser("firmware-update", help="使用本地固件文件刷写 BT11（高风险）")
    update.add_argument("file", help="官方 BT11.bin 文件路径")
    update.add_argument("--yes", action="store_true", help="确认执行不可逆刷写")
    update.add_argument("--timeout", type=float, default=180.0)
    for name, help_text, choices in (
        ("name", "读取或设置蓝牙广播名称", None),
        ("brightness", "读取或设置指示灯亮度（0-7）", range(8)),
        ("aptx-mode", "读取或设置 aptX Adaptive 模式", APTX_MODES),
        ("ldac-mode", "读取或设置 LDAC 模式", LDAC_MODES),
        ("pairing", "读取或设置配对模式", range(3)),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("value", nargs="?", type=None if choices is None else int, choices=choices)
    codec = sub.add_parser("codecs", help="读取或设置可用 codec")
    codec.add_argument("value", nargs="*", choices=sorted(set(USER_CODEC_NAMES) | {"sbc"}), help="不提供则读取；提供一个或多个则写入")
    sub.add_parser("devices", help="列出已配对设备")
    for command_name in ("connect", "disconnect", "pair", "forget"):
        command = sub.add_parser(command_name)
        command.add_argument("address")
    scan = sub.add_parser("scan", help="扫描附近的 Bluetooth 设备")
    scan.add_argument("--seconds", type=float, default=3.0)
    sub.add_parser("self-test", help="串行回读全部非破坏性设置")
    for command_name, help_text in (("forget-all", "删除全部配对记录"), ("reset", "恢复 BT11 设置") ):
        command = sub.add_parser(command_name, help=help_text)
        command.add_argument("--yes", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    # argparse gives codecs a list; other optional values are scalar.
    if args.command == "codecs" and args.value == []:
        args.value = None
    return _run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
