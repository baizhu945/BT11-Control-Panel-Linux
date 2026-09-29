import unittest

import os
import tempfile

import bt11_control
import bt11_auto_mode
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
    auto_mode_disabled,
    build_frame,
    set_auto_mode_disabled,
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


class SampleRateModeTests(unittest.TestCase):
    def test_lossless_rates(self):
        self.assertEqual(bt11_auto_mode.mode_for_rate(44100), 19)
        self.assertEqual(bt11_auto_mode.mode_for_rate(88200), 19)

    def test_other_known_rates_use_high_quality(self):
        for rate in (32000, 48000, 96000, 176400):
            with self.subTest(rate=rate):
                self.assertEqual(bt11_auto_mode.mode_for_rate(rate), 3)

    def test_invalid_or_unknown_rate_does_not_decide(self):
        self.assertIsNone(bt11_auto_mode.mode_for_rate(None))
        self.assertIsNone(bt11_auto_mode.mode_for_rate(0))
        self.assertIsNone(bt11_auto_mode.mode_for_rate(-44100))

    def test_only_good_modes_are_changed(self):
        self.assertEqual(bt11_auto_mode.decide(44100, 3)[0], 19)
        self.assertEqual(bt11_auto_mode.decide(48000, 19)[0], 3)
        self.assertIsNone(bt11_auto_mode.decide(44100, 19)[0])
        self.assertIsNone(bt11_auto_mode.decide(48000, 3)[0])

    def test_automatic_writer_rejects_low_latency_target(self):
        with self.assertRaises(ValueError):
            bt11_auto_mode.set_mode(2)

    def test_low_latency_is_always_left_alone(self):
        for rate in (44100, 88200, 48000, 96000):
            with self.subTest(rate=rate):
                mode, _reason, label = bt11_auto_mode.decide(rate, 2)
                self.assertIsNone(mode)
                self.assertEqual(label, "paused-ll")

    def test_unknown_current_mode_fails_closed(self):
        self.assertIsNone(bt11_auto_mode.decide(44100, None)[0])
        self.assertIsNone(bt11_auto_mode.decide(44100, 1)[0])

    def test_rate_parser(self):
        self.assertEqual(bt11_auto_mode._parse_rate("1/44100"), 44100)
        self.assertEqual(bt11_auto_mode._parse_rate("1/88200"), 88200)
        self.assertEqual(bt11_auto_mode._parse_rate(48000), 48000)
        self.assertEqual(bt11_auto_mode._parse_rate("48000"), 48000)
        self.assertIsNone(bt11_auto_mode._parse_rate("invalid"))
        self.assertIsNone(bt11_auto_mode._parse_rate("1/0"))
        self.assertIsNone(bt11_auto_mode._parse_rate(0))

    def test_mixed_stream_rate_is_conservative(self):
        stream = bt11_auto_mode.Stream
        self.assertEqual(
            bt11_auto_mode.content_rate([
                stream(("a", ""), "a", 44100),
                stream(("b", ""), "b", 88200),
            ]),
            88200,
        )
        self.assertEqual(
            bt11_auto_mode.mode_for_rate(
                bt11_auto_mode.content_rate([
                    stream(("a", ""), "a", 44100),
                    stream(("b", ""), "b", 48000),
                ])
            ),
            3,
        )
        self.assertEqual(
            bt11_auto_mode.mode_for_rate(
                bt11_auto_mode.content_rate([
                    stream(("a", ""), "a", 32000),
                    stream(("b", ""), "b", 44100),
                ])
            ),
            3,
        )
        self.assertIsNone(
            bt11_auto_mode.content_rate([stream(("a", ""), "a", None)]))

    def test_active_playback_filters_and_deduplicates_links(self):
        document = [
            {
                "id": 10,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"media.class": "Audio/Sink", "node.name": "BT11"}},
            },
            {
                "id": 20,
                "type": "PipeWire:Interface:Node",
                "info": {"state": "running", "props": {"node.name": "player", "node.rate": "1/44100"}},
            },
            {
                "id": 30,
                "type": "PipeWire:Interface:Node",
                "info": {"state": "idle", "props": {"node.name": "idle", "node.rate": "1/48000"}},
            },
            {
                "id": 40,
                "type": "PipeWire:Interface:Node",
                "info": {"state": "running", "props": {"node.name": "other", "audio.rate": 48000}},
            },
            {"type": "PipeWire:Interface:Link", "info": {"input-node-id": 10, "output-node-id": 20}},
            {"type": "PipeWire:Interface:Link", "info": {"input-node-id": 10, "output-node-id": 20}},
            {"type": "PipeWire:Interface:Link", "info": {"input-node-id": 10, "output-node-id": 30}},
            {"type": "PipeWire:Interface:Link", "info": {"input-node-id": 10, "output-node-id": 10}},
            {"type": "PipeWire:Interface:Link", "info": {"input-node-id": 99, "output-node-id": 40}},
        ]
        players = bt11_auto_mode.active_playback(document, 10)
        self.assertEqual([player.pair() for player in players], [("player", 44100)])
        self.assertEqual(players[0].key, ("player", ""))
        self.assertEqual(bt11_auto_mode.players_text(players),
                         "[('player', 44100)]")

    def test_stream_key_survives_node_id_changes(self):
        document = [
            {
                "id": 10,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"media.class": "Audio/Sink", "node.name": "BT11"}},
            },
            {
                "id": 77,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {"node.name": "Spotify", "application.name": "Spotify",
                              "node.rate": "1/44100"},
                },
            },
            {"type": "PipeWire:Interface:Link", "info": {"input-node-id": 10, "output-node-id": 77}},
        ]
        self.assertEqual(
            bt11_auto_mode.active_playback(document, 10)[0].key,
            ("Spotify", "Spotify"),
        )


class SessionTrackerTests(unittest.TestCase):
    """A notification at another rate must not disturb a running session."""

    MUSIC = frozenset({("mpv", "mpv")})
    OTHER = frozenset({("pw-play", "pw-play")})

    def setUp(self):
        self.tracker = bt11_auto_mode.SessionTracker(idle_grace=2.0)

    def test_extra_stream_does_not_change_the_session_rate(self):
        self.assertEqual(self.tracker.update(self.MUSIC, 44100, 0.0), "started")
        self.assertEqual(
            self.tracker.update(self.MUSIC | self.OTHER, 48000, 0.5), "continues")
        self.assertEqual(self.tracker.target_rate, 44100)

    def test_reverse_case_keeps_high_quality_session(self):
        self.assertEqual(self.tracker.update(self.OTHER, 48000, 0.0), "started")
        self.assertEqual(
            self.tracker.update(self.OTHER | self.MUSIC, 44100, 0.5), "continues")
        self.assertEqual(self.tracker.target_rate, 48000)

    def test_new_playback_after_silence_decides_again(self):
        self.tracker.update(self.MUSIC, 44100, 0.0)
        self.assertEqual(self.tracker.update(frozenset(), None, 1.0), "fading")
        self.assertEqual(self.tracker.update(frozenset(), None, 4.0), "ended")
        self.assertEqual(self.tracker.update(self.OTHER, 48000, 5.0), "started")
        self.assertEqual(self.tracker.target_rate, 48000)

    def test_short_gap_keeps_the_session(self):
        self.tracker.update(self.MUSIC, 44100, 0.0)
        self.assertEqual(self.tracker.update(frozenset(), None, 1.0), "fading")
        self.assertEqual(self.tracker.update(self.MUSIC, 44100, 1.5), "continues")
        self.assertEqual(self.tracker.target_rate, 44100)

    def test_handover_needs_the_grace_before_switching(self):
        self.tracker.update(self.MUSIC, 44100, 0.0)
        self.assertEqual(self.tracker.update(self.OTHER, 48000, 0.5), "fading")
        self.assertEqual(self.tracker.target_rate, 44100)
        self.assertEqual(self.tracker.update(self.OTHER, 48000, 3.0), "replaced")
        self.assertEqual(self.tracker.target_rate, 48000)

    def test_unknown_rate_is_adopted_when_it_becomes_known(self):
        self.assertEqual(self.tracker.update(self.MUSIC, None, 0.0), "started")
        self.assertIsNone(self.tracker.target_rate)
        self.assertEqual(
            self.tracker.update(self.MUSIC, 44100, 0.4), "rate-known")
        self.assertEqual(self.tracker.target_rate, 44100)

    def test_idle_without_session(self):
        self.assertEqual(self.tracker.update(frozenset(), None, 0.0), "idle")
        self.assertFalse(self.tracker.active)


class AutoModeFlagTests(unittest.TestCase):
    """The auto-mode switch is a flag file, so it must work without the device."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original = bt11_control.AUTO_MODE_FLAG
        bt11_control.AUTO_MODE_FLAG = os.path.join(self.directory.name,
                                                   "sub", "auto-mode-disabled")

    def tearDown(self):
        bt11_control.AUTO_MODE_FLAG = self.original
        self.directory.cleanup()

    def test_toggle_round_trip(self):
        self.assertFalse(auto_mode_disabled())
        self.assertTrue(set_auto_mode_disabled(True))
        self.assertTrue(os.path.exists(bt11_control.AUTO_MODE_FLAG))
        self.assertTrue(auto_mode_disabled())
        self.assertFalse(set_auto_mode_disabled(False))
        self.assertFalse(auto_mode_disabled())

    def test_disabling_twice_is_idempotent(self):
        set_auto_mode_disabled(True)
        self.assertTrue(set_auto_mode_disabled(True))
        self.assertFalse(set_auto_mode_disabled(False))
        self.assertFalse(set_auto_mode_disabled(False))


if __name__ == "__main__":
    unittest.main()
