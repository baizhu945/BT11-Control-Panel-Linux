import unittest

from bt11_control import (
    APP_FEATURE,
    CMD_GET_CODECS,
    CMD_GET_PAIRED_LIST,
    CMD_DELETE_ALL_PAIRED,
    CMD_RESET_SETTINGS,
    CMD_SET_CODECS,
    CODEC_IDS,
    Bt11FirmwareUpdater,
    Bt11Hid,
    PairingDevice,
    _payload,
    build_frame,
)


class FakeBt11(Bt11Hid):
    def __init__(self, responses):
        super().__init__(path="/dev/null")
        self.responses = list(responses)
        self.calls = []

    def request(self, feature, command, payload=b"", timeout=None):
        self.calls.append((feature, command, bytes(payload)))
        return self.responses.pop(0)


class ProtocolTests(unittest.TestCase):
    def test_build_frame_matches_fiio_web_driver_layout(self):
        self.assertEqual(
            build_frame(APP_FEATURE, CMD_GET_CODECS),
            bytes.fromhex("ff 03 00 00 00 1d 30 06"),
        )
        self.assertEqual(
            build_frame(APP_FEATURE, CMD_SET_CODECS, bytes([8, 7, 1, 0])),
            bytes.fromhex("ff 03 00 04 00 1d 30 07 08 07 01 00"),
        )

    def test_payload_uses_length_byte(self):
        data = build_frame(APP_FEATURE, CMD_GET_CODECS, bytes([8, 7, 6])) + bytes(20)
        self.assertEqual(_payload(data), bytes([8, 7, 6]))

    def test_codecs_parse_and_write(self):
        device = FakeBt11([bytes([8, 7, 6]), b"", bytes([8, 7])])
        self.assertEqual(device.get_codecs(), (("ldac", "aptx-adaptive", "aptx-hd"), ()))
        self.assertEqual(device.set_codecs(["aptx-adaptive", "ldac"]), ("ldac", "aptx-adaptive"))
        self.assertEqual(
            device.calls,
            [
                (APP_FEATURE, CMD_GET_CODECS, b""),
                (APP_FEATURE, CMD_SET_CODECS, bytes([8, 7, 1, 0])),
                (APP_FEATURE, CMD_GET_CODECS, b""),
            ],
        )

    def test_paired_list_record_layout(self):
        address = bytes.fromhex("80 99 e7 6a 8f cc")
        # count + connect type + six address bytes + connected flag + 4 profiles
        response = bytes([1, 0]) + address + bytes([128, 1, 0, 0, 0])
        device = FakeBt11([response, b"WF-1000XM5"])
        devices = device.get_paired_devices()
        self.assertEqual(
            devices,
            [PairingDevice(address=address, name="WF-1000XM5", connected=True, connect_type=0, profiles=bytes([1, 0, 0, 0]))],
        )
        self.assertEqual(device.calls[0], (APP_FEATURE, CMD_GET_PAIRED_LIST, b""))

    def test_pairing_address_commands_use_official_payload_layout(self):
        address = bytes.fromhex("80 99 e7 6a 8f cc")
        device = FakeBt11([b"", b"", b"", b""])
        device.connect_device(address)
        device.disconnect_device(address)
        device.pair_device(address)
        device.delete_paired_device(address)
        expected_payload = bytes([0]) + address + bytes([0])
        self.assertEqual([call[2] for call in device.calls], [expected_payload] * 4)

    def test_dfu_command_id_mapping_matches_web_driver(self):
        self.assertEqual(Bt11FirmwareUpdater._command_id(58), 25)
        self.assertEqual(Bt11FirmwareUpdater._command_id(52), 19)
        self.assertEqual(Bt11FirmwareUpdater._command_id(37), 4)

    def test_destructive_commands_are_explicit_protocol_calls(self):
        device = FakeBt11([b"", b""])
        device.delete_all_paired()
        device.reset_settings()
        self.assertEqual(
            device.calls,
            [
                (APP_FEATURE, CMD_DELETE_ALL_PAIRED, b""),
                (APP_FEATURE, CMD_RESET_SETTINGS, b""),
            ],
        )


if __name__ == "__main__":
    unittest.main()
