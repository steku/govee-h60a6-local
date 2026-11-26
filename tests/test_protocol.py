"""Unit tests for Govee H60A6 protocol encoders, decoders, and metadata."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parent.parent
COMP_DIR = ROOT / "custom_components" / "govee_h60a6_local"


def _load_core():
    pkg_name = "_govee_test_core"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMP_DIR)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg
    mods = {}
    for name in ("const", "protocol", "transport"):
        full = f"{pkg_name}.{name}"
        if full in sys.modules:
            mods[name] = sys.modules[full]
            continue
        spec = importlib.util.spec_from_file_location(full, COMP_DIR / f"{name}.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods["const"], mods["protocol"], mods["transport"]


const, protocol, transport = _load_core()


class TestGoveeProtocol(unittest.TestCase):
    def test_lan_json_commands(self) -> None:
        self.assertEqual(
            json.loads(protocol.build_scan_payload()),
            {"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}},
        )
        self.assertEqual(
            json.loads(protocol.build_status_payload()),
            {"msg": {"cmd": "devStatus", "data": {}}},
        )
        self.assertEqual(
            json.loads(protocol.build_turn_payload(True)),
            {"msg": {"cmd": "turn", "data": {"value": 1}}},
        )
        self.assertEqual(
            json.loads(protocol.build_turn_payload(False)),
            {"msg": {"cmd": "turn", "data": {"value": 0}}},
        )
        self.assertEqual(
            json.loads(protocol.build_brightness_payload(150)),
            {"msg": {"cmd": "brightness", "data": {"value": 100}}},
        )
        self.assertEqual(
            json.loads(protocol.build_color_payload(255, 128, 0)),
            {
                "msg": {
                    "cmd": "colorwc",
                    "data": {"color": {"r": 255, "g": 128, "b": 0}, "colorTemInKelvin": 0},
                }
            },
        )
        self.assertEqual(
            json.loads(protocol.build_color_temp_payload(4000)),
            {
                "msg": {
                    "cmd": "colorwc",
                    "data": {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 4000},
                }
            },
        )
        # Clamps out-of-range Kelvin to H60A6 bounds (2200..6500)
        self.assertEqual(
            json.loads(protocol.build_color_temp_payload(1000))["msg"]["data"][
                "colorTemInKelvin"
            ],
            const.MIN_KELVIN,
        )
        self.assertEqual(
            json.loads(protocol.build_color_temp_payload(9000))["msg"]["data"][
                "colorTemInKelvin"
            ],
            const.MAX_KELVIN,
        )

    def test_ble_packets_length_and_xor_checksum(self) -> None:
        packets = [
            protocol.build_ble_power_packet(True),
            protocol.build_ble_power_packet(False),
            protocol.build_ble_brightness_packet(100),
            protocol.build_ble_rgb_packet(255, 0, 128),
            protocol.build_ble_color_temp_packet(2700),
            protocol.build_ble_segment_color_packet(10, 20, 30, 0x0FFF),
            protocol.build_ble_segment_brightness_packet(75, 0x000F),
            protocol.build_ble_scene_packet(4),
            protocol.build_ble_scene_packet(0x012B),
            protocol.build_ble_music_mode_packet(2, 80),
            protocol.build_ble_keepalive_packet(),
        ]
        for pkt in packets:
            self.assertEqual(len(pkt), 20)
            self.assertEqual(pkt[19], protocol.compute_xor_checksum(pkt[:19]))

        # Verify known brightness 100% checksum: 0x33 ^ 0x04 ^ 0x64 = 0x53
        b100 = protocol.build_ble_brightness_packet(100)
        self.assertEqual(b100[:3], bytes([0x33, 0x04, 0x64]))
        self.assertEqual(b100[19], 0x33 ^ 0x04 ^ 0x64)

    def test_segment_bitmask_encoding(self) -> None:
        self.assertEqual(protocol.segments_to_bitmask([1]), 0x0001)
        self.assertEqual(protocol.segments_to_bitmask([2]), 0x0002)
        self.assertEqual(protocol.segments_to_bitmask([1, 2, 4]), 0x000B)
        self.assertEqual(
            protocol.segments_to_bitmask(list(range(1, 13))),
            const.ALL_RING_SEGMENTS_MASK,
        )
        # 33 05 15 01: little-endian bitmask at bytes 12..13
        pkt = protocol.build_ble_segment_color_packet(12, 34, 56, 0x0ABC)
        self.assertEqual(pkt[0:4], bytes([0x33, 0x05, 0x15, 0x01]))
        self.assertEqual(pkt[4:7], bytes([12, 34, 56]))
        self.assertEqual(pkt[7:12], bytes([0x00, 0x00, 0x00, 0x00, 0x00]))
        self.assertEqual(pkt[12:14], bytes([0xBC, 0x0A]))
        self.assertEqual(pkt[19], protocol.compute_xor_checksum(pkt[:19]))

        # 33 05 0b: little-endian bitmask at bytes 8..9
        pkt_v2 = protocol.build_ble_segment_color_v2_packet(12, 34, 56, 0x0ABC)
        self.assertEqual(pkt_v2[0:3], bytes([0x33, 0x05, 0x0B]))
        self.assertEqual(pkt_v2[3:6], bytes([12, 34, 56]))
        self.assertEqual(pkt_v2[8:10], bytes([0xBC, 0x0A]))
        self.assertEqual(pkt_v2[19], protocol.compute_xor_checksum(pkt_v2[:19]))

        # 33 05 15 02: little-endian bitmask at bytes 5..6
        pkt_br = protocol.build_ble_segment_brightness_packet(75, 0x0ABC)
        self.assertEqual(pkt_br[0:5], bytes([0x33, 0x05, 0x15, 0x02, 75]))
        self.assertEqual(pkt_br[5:7], bytes([0xBC, 0x0A]))
        self.assertEqual(pkt_br[19], protocol.compute_xor_checksum(pkt_br[:19]))

    def test_ptreal_and_raw_hex_parsing(self) -> None:
        pkt = protocol.parse_hex_to_ble_packet("33 05 04 04")
        self.assertEqual(len(pkt), 20)
        self.assertEqual(pkt[0:4], bytes([0x33, 0x05, 0x04, 0x04]))
        self.assertEqual(pkt[19], protocol.compute_xor_checksum(pkt[:19]))

        wrapped = json.loads(protocol.build_ptreal_payload([pkt]))
        self.assertEqual(wrapped["msg"]["cmd"], "ptReal")
        decoded = base64.b64decode(wrapped["msg"]["data"]["command"][0])
        self.assertEqual(decoded, pkt)

        with self.assertRaises(ValueError):
            protocol.parse_hex_to_ble_packet("invalid")

    def test_razer_frames(self) -> None:
        on_frame = protocol.build_razer_toggle_frame(True)
        # BB 00 01 B1 01 ^ = 0x0A -> base64 uwABsQEK
        self.assertEqual(on_frame, bytes([0xBB, 0x00, 0x01, 0xB1, 0x01, 0x0A]))
        self.assertEqual(base64.b64encode(on_frame).decode(), "uwABsQEK")

        off_frame = protocol.build_razer_toggle_frame(False)
        self.assertEqual(off_frame, bytes([0xBB, 0x00, 0x01, 0xB1, 0x00, 0x0B]))
        self.assertEqual(base64.b64encode(off_frame).decode(), "uwABsQAL")

        color_frame = protocol.build_razer_color_frame([(255, 0, 0), (0, 255, 0)], gradient=True)
        self.assertEqual(color_frame[0], 0xBB)
        self.assertEqual(color_frame[3], 0xB0)
        self.assertEqual(color_frame[4], 0x01)  # gradient flag
        self.assertEqual(color_frame[5], 2)  # 2 segments
        self.assertEqual(color_frame[-1], protocol.compute_xor_checksum(color_frame[:-1]))

    def test_brightness_and_kelvin_conversions(self) -> None:
        self.assertEqual(protocol.ha_brightness_to_govee(255), 100)
        self.assertEqual(protocol.ha_brightness_to_govee(128), 50)
        self.assertEqual(protocol.ha_brightness_to_govee(1), 1)
        self.assertEqual(protocol.govee_brightness_to_ha(100), 255)
        self.assertEqual(protocol.govee_brightness_to_ha(50), 128)
        warm = protocol.kelvin_to_rgb(2200)
        cool = protocol.kelvin_to_rgb(6500)
        self.assertGreater(warm[0], warm[2])  # Warm has more red than blue
        self.assertGreater(cool[2], warm[2])  # Cool has much more blue than warm

    def test_response_parsers(self) -> None:
        raw = json.dumps(
            {
                "msg": {
                    "cmd": "devStatus",
                    "data": {
                        "onOff": 1,
                        "brightness": 72,
                        "color": {"r": 12, "g": 34, "b": 56},
                        "colorTemInKelvin": 0,
                    },
                }
            }
        ).encode()
        parsed = protocol.parse_lan_response(raw)
        assert parsed is not None
        cmd, data = parsed
        self.assertEqual(cmd, "devStatus")
        status = protocol.parse_status_response(data)
        assert status is not None
        self.assertTrue(status.is_on)
        self.assertEqual(status.brightness, 72)
        self.assertEqual(status.rgb, (12, 34, 56))
        self.assertEqual(status.color_temp_kelvin, 0)

    def test_integration_json_files_valid(self) -> None:
        for path in (
            COMP_DIR / "manifest.json",
            COMP_DIR / "strings.json",
            COMP_DIR / "translations" / "en.json",
            ROOT / "hacs.json",
        ):
            with self.subTest(path=str(path)):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(data, dict)

        manifest = json.loads((COMP_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["domain"], const.DOMAIN)
        self.assertTrue(manifest["config_flow"])


if __name__ == "__main__":
    unittest.main()
