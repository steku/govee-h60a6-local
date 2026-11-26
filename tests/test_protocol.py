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
    for name in ("const", "protocol", "scenes", "transport"):
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
    return mods["const"], mods["protocol"], mods["scenes"], mods["transport"]


const, protocol, scenes_mod, transport = _load_core()


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

        # Verify full-fixture RGB packet uses 33 05 15 01 with 0x1FFF mask at bytes 12..14
        rgb_pkt = protocol.build_ble_rgb_packet(255, 0, 128)
        self.assertEqual(rgb_pkt[0:7], bytes([0x33, 0x05, 0x15, 0x01, 255, 0, 128]))
        self.assertEqual(rgb_pkt[12:14], bytes([0xFF, 0x1F]))

        # Verify dedicated WW/CW Kelvin packet uses 33 05 15 01 00 00 00 <k_hi> <k_lo> <wr wg wb> <mask_lo mask_hi>
        k2700 = protocol.build_ble_color_temp_packet(2700)
        wr, wg, wb = protocol.kelvin_to_rgb(2700)
        self.assertEqual(k2700[0:7], bytes([0x33, 0x05, 0x15, 0x01, 0, 0, 0]))
        self.assertEqual(k2700[7:9], bytes([0x0A, 0x8C]))  # 2700 = 0x0A8C
        self.assertEqual(k2700[9:12], bytes([wr, wg, wb]))
        self.assertEqual(k2700[12:14], bytes([0xFF, 0x1F]))
        self.assertEqual(k2700[19], protocol.compute_xor_checksum(k2700[:19]))

        k6500_center = protocol.build_ble_segment_color_temp_packet(6500, 0x1000)
        self.assertEqual(k6500_center[7:9], bytes([0x19, 0x64]))  # 6500 = 0x1964
        self.assertEqual(k6500_center[12:14], bytes([0x00, 0x10]))
        self.assertEqual(k6500_center[19], protocol.compute_xor_checksum(k6500_center[:19]))

    def test_segment_bitmask_encoding(self) -> None:
        self.assertEqual(const.MAIN_CENTER_SEGMENT_MASK, 0x1000)
        self.assertEqual(protocol.segments_to_bitmask([1]), 0x0001)
        self.assertEqual(protocol.segments_to_bitmask([2]), 0x0002)
        self.assertEqual(protocol.segments_to_bitmask([1, 2, 4]), 0x000B)
        self.assertEqual(protocol.segments_to_bitmask([13]), 0x1000)
        self.assertEqual(
            protocol.segments_to_bitmask(list(range(1, 13))),
            const.ALL_RING_SEGMENTS_MASK,
        )
        self.assertEqual(
            protocol.segments_to_bitmask(list(range(1, 14))),
            const.ALL_SEGMENTS_MASK,
        )
        self.assertEqual(len(protocol.GoveeState().segments), 13)
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

        # Verify local Home Assistant brand icons (256x256 and 512x512 RGBA PNGs)
        import struct

        for filename, expected_size in (
            ("icon.png", 256),
            ("icon@2x.png", 512),
            ("dark_icon.png", 256),
            ("dark_icon@2x.png", 512),
        ):
            icon_path = COMP_DIR / "brand" / filename
            self.assertTrue(icon_path.is_file(), f"Missing {icon_path}")
            header = icon_path.read_bytes()[:24]
            self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
            w, h = struct.unpack(">II", header[16:24])
            self.assertEqual((w, h), (expected_size, expected_size))

    def test_ble_session_encryption_matches_h60a6_vectors(self) -> None:
        psk = const.BLE_HANDSHAKE_PSK
        session_key = bytes.fromhex("f623507eabd80633608ebbe81543709d")

        vectors = [
            (
                psk,
                bytes.fromhex("e701f623507eabd80633608ebbe81543709d0558"),
                bytes.fromhex("c16f604acd11ed251913521df5e607e018734507"),
            ),
            (
                psk,
                bytes.fromhex("e702255a69d97b399444d2c62fc7563ce2d8b5a1"),
                bytes.fromhex("19a14536c53134b4f71388e0bfd732dd8a36f5fe"),
            ),
            (
                session_key,
                bytes.fromhex("aa010000000000000000000000000000000000ab"),
                bytes.fromhex("def39ec62b3c34c1587d62cb49039ab4b8a9297f"),
            ),
            (
                session_key,
                bytes.fromhex("3301000000000000000000000000000000000032"),
                bytes.fromhex("7f758c8a244228c82e76af98fd9afc39b8a929e6"),
            ),
            (
                session_key,
                bytes.fromhex("ee300100000000000000000000000000000000df"),
                bytes.fromhex("352e28270454d6df4c8ac3be2950df2eb8a9290b"),
            ),
        ]
        for key, plain, cipher in vectors:
            self.assertEqual(protocol.govee_ble_encrypt(plain, key), cipher)
            self.assertEqual(protocol.govee_ble_decrypt(cipher, key), plain)

        hs1 = protocol.build_ble_handshake_packet(0x01, bytes(range(17)))
        self.assertEqual(len(hs1), 20)
        self.assertEqual(hs1[:2], bytes([0xE7, 0x01]))
        self.assertEqual(hs1[19], protocol.compute_xor_checksum(hs1[:19]))

        # Real 195-byte 0xA4 Sunrise vector from H60A6 HCI snoop log ([0676])
        snoop_key = bytes.fromhex("1c4976a4d1fe2c5986b3e10e3b6996c3")
        sunrise_b64 = (
            "UCABAQFDAAGyAAOlAAYADwD/AAAEBQYHHR4fICEiIyQlJicdAP8iAQgaGxwoKSor"
            "RkdISUpLTE1OT1BRUmdoaWprbG1uMAD/WAECAxQVFhcYGSwtLi8wMUFCQ0RFU1RV"
            "VldYYmNkZWZvcHFyc3h5ent8fX5/gIGCg4QBAP+EAQkeAP+XAQEKDxAREhMyMzQ1"
            "Njw9Pj9AWVpbXF1eX2BhdHV2dwoA/6cBAAsMDQ43ODk6OwIAZAEQJwAAAAA="
        )
        sunrise_a4 = protocol.build_ble_multipacket_scene_packets(19075, sunrise_b64)[0]
        self.assertEqual(len(sunrise_a4), 195)
        enc_a4 = protocol.govee_ble_encrypt(sunrise_a4, snoop_key)
        self.assertEqual(len(enc_a4), 195)
        self.assertEqual(protocol.govee_ble_decrypt(enc_a4, snoop_key), sunrise_a4)
        # Verify full 16-byte blocks (e.g. block 1 bytes 16..32) use AES-128-ECB while 3-byte tail uses RC4
        self.assertEqual(
            protocol._aes128_decrypt_block(enc_a4[16:32], snoop_key),
            sunrise_a4[16:32],
        )
        self.assertEqual(
            protocol._rc4_crypt(enc_a4[192:], snoop_key),
            sunrise_a4[192:],
        )
        # Real 4-byte 0xA4 terminator vector ([0677]) encrypted purely via RC4
        term_plain = bytes.fromhex("a4ffffa4")
        term_cipher = bytes.fromhex("29aab437")
        self.assertEqual(protocol.govee_ble_encrypt(term_plain, snoop_key), term_cipher)
        self.assertEqual(protocol.govee_ble_decrypt(term_cipher, snoop_key), term_plain)

    def test_little_endian_and_multipacket_scene_encoding(self) -> None:
        # 10191 == 0x27CF -> little-endian CF 27
        pkt = protocol.build_ble_scene_packet(10191)
        self.assertEqual(len(pkt), 20)
        self.assertEqual(pkt[0:5], bytes([0x33, 0x05, 0x04, 0xCF, 0x27]))
        self.assertEqual(pkt[19], protocol.compute_xor_checksum(pkt[:19]))

        param_b64 = "ASkAAAAHAgH/gQD5FBQD+RQG/wAA/38A//8AAP8AAP//AAD/EADyAACAAA=="
        frames = protocol.build_ble_multipacket_scene_packets(10191, param_b64)
        self.assertGreater(len(frames), 2)
        for frame in frames:
            self.assertEqual(len(frame), 20)
            self.assertEqual(frame[19], protocol.compute_xor_checksum(frame[:19]))
        # First frame: 0xA3 0x00 0x01 <total_lines> ...
        self.assertEqual(frames[0][0:4], bytes([0xA3, 0x00, 0x01, len(frames) - 1]))
        # Last param frame uses line index 0xFF
        self.assertEqual(frames[-2][0:2], bytes([0xA3, 0xFF]))
        # Final frame is the 33 05 04 CF 27 activation packet
        self.assertEqual(frames[-1], pkt)

        # Verify a real bundled H60A6 scene payload (Forest: code 19073 = 0x4A81 -> LE 81 4A)
        forest_b64 = "UCwAAgEA/wAUEQAFZAYBBP//AAD/AJn/AP/DABEABU0KAQT//wD/Ov//hAAA+f8="
        forest_frames = protocol.build_ble_multipacket_scene_packets(19073, forest_b64)
        self.assertEqual(len(forest_frames), 4)
        self.assertEqual(forest_frames[0][0:6], bytes([0xA3, 0x00, 0x01, 0x03, 0x58, 0x2C]))
        self.assertEqual(forest_frames[1][0:2], bytes([0xA3, 0x01]))
        self.assertEqual(forest_frames[2][0:2], bytes([0xA3, 0xFF]))
        self.assertEqual(forest_frames[3][0:5], bytes([0x33, 0x05, 0x04, 0x81, 0x4A]))

        # Exact byte-for-byte match against real H60A6 BLE HCI snoop capture (Rainbow: 19077 = 0x4A85)
        rainbow_b64 = "UDwAAgEAAP88GQABVcQAPDwEAAAAAGRkAQED/wAA//8AAP8AGQABVYgAPDwEAAAAAGRkAQEDAAD/AP//iwD/"
        rainbow_frames = protocol.build_ble_multipacket_scene_packets(19077, rainbow_b64)
        self.assertEqual(
            [frame.hex() for frame in rainbow_frames],
            [
                "a3000104583c0002010000ff3c19000155c4008b",
                "a3013c3c04000000006464010103ff0000ffff5a",
                "a3020000ff001900015588003c3c04000000009f",
                "a3ff64640101030000ff00ffff8b00ff000000d4",
                "330504854a0000000000000000000000000000fd",
            ],
        )

        # Sunrise (19075 = 0x4A83 -> LE 83 4A): 0x20 multi-layer scene uses 195-byte 0xA4 + `a4ffffa4` + `330504`
        sunrise_b64 = (
            "UCABAQFDAAGyAAOlAAYADwD/AAAEBQYHHR4fICEiIyQlJicdAP8iAQgaGxwoKSor"
            "RkdISUpLTE1OT1BRUmdoaWprbG1uMAD/WAECAxQVFhcYGSwtLi8wMUFCQ0RFU1RV"
            "VldYYmNkZWZvcHFyc3h5ent8fX5/gIGCg4QBAP+EAQkeAP+XAQEKDxAREhMyMzQ1"
            "Njw9Pj9AWVpbXF1eX2BhdHV2dwoA/6cBAAsMDQ43ODk6OwIAZAEQJwAAAAA="
        )
        sunrise_frames = protocol.build_ble_multipacket_scene_packets(19075, sunrise_b64)
        self.assertEqual(len(sunrise_frames), 3)
        self.assertEqual(len(sunrise_frames[0]), 195)
        self.assertEqual(
            sunrise_frames[0][:16].hex(),
            "a400000102005820010101430001b200",
        )
        self.assertEqual(
            sunrise_frames[0][-1],
            protocol.compute_xor_checksum(sunrise_frames[0][:-1]),
        )
        self.assertEqual(sunrise_frames[1].hex(), "a4ffffa4")
        self.assertEqual(
            sunrise_frames[2].hex(),
            "330504834a0000000000000000000000000000fb",
        )

        # Halloween B (19096 = 0x4A98 -> LE 98 4A): 35-byte 0xA3 scene whose LE length happens to be 0x0020
        halloween_b_b64 = "UCAAAQGLAP8UGAACQQoBBBkyS2RkZAEAAQP/AACLAP//fwA="
        halloween_b_frames = protocol.build_ble_multipacket_scene_packets(
            19096, halloween_b_b64
        )
        self.assertEqual(len(halloween_b_frames), 4)
        self.assertEqual(
            halloween_b_frames[0][0:10],
            bytes.fromhex("a300010358200001018b"),
        )
        self.assertEqual(halloween_b_frames[1][0:2], bytes([0xA3, 0x01]))
        self.assertEqual(halloween_b_frames[2][0:2], bytes([0xA3, 0xFF]))
        self.assertEqual(
            halloween_b_frames[3].hex(),
            "330504984a0000000000000000000000000000e0",
        )

        # Ocean (19080 = 0x4A88 -> LE 88 4A): 336-byte multi-chunk 0xA4 matching [0695]/[0696]/[0699]
        ocean_b64 = (
            "UCABAQEBAAQWAAMJAAEAAgAL/j8/QARkZAEQJwAAAAAcAAMPAAIAAgD7/wsNDgEA"
            "/xKuXANhZAIQJwAAAABXAANKAAEAQwAAAP8AAQIJCgsMDQ4PEBESExQVFi8wMTIz"
            "NDU2Nzg5Ojs8PT4/QEFCQ1ZXWFlaW1xdXl9gYWJjZHFyc3R1dnd4eXqBgoOEAgBk"
            "AxAnAAAAALcAA6oABwAKAAAYUgUGHyAhIiMkJSYeAABQjAQHGhscHR4nKCkqK0dI"
            "SUpLTE1OT1BRUmhpamtsbQcAAGyxLEZTVGdubyMAAIHPAgMICRQVFhkvMDFBQkNF"
            "VldYYmNkZnFyc3h5fH1+f4GCg4QeAACT6QEKDxAREhMyMzQ1Njw9Pj9AWVpbXF1e"
            "X2BhdHV2dwsAAKL/AAsMDQ43ODk6O3oKAP///xcYLS5EVWVwe4ACAGQEECcAAAAA"
        )
        ocean_frames = protocol.build_ble_multipacket_scene_packets(19080, ocean_b64)
        self.assertEqual(len(ocean_frames), 3)
        self.assertEqual(len(ocean_frames[0]), 244)
        self.assertEqual(
            ocean_frames[0][:20].hex(),
            "a400000102005820010101010004160003090001",
        )
        self.assertEqual(ocean_frames[0][-8:].hex(), "1516192f30314188")
        self.assertEqual(len(ocean_frames[1]), 103)
        self.assertEqual(
            ocean_frames[1][:20].hex(),
            "a4ffff4243455657586263646671727378797c7d",
        )
        self.assertEqual(ocean_frames[1][-8:].hex(), "04102700000000cc")
        self.assertEqual(
            ocean_frames[2].hex(),
            "330504884a0000000000000000000000000000f0",
        )

    def test_parse_govee_library_and_load_scenes(self) -> None:
        import tempfile

        mock_library = {
            "message": "success",
            "status": 200,
            "data": {
                "categories": [
                    {
                        "sceneId": 7691,
                        "sceneName": "Rainbow B",
                        "sceneCode": 0,
                        "lightEffects": [
                            {
                                "scenceParamId": 11837,
                                "scenceName": "",
                                "scenceParam": "ASkAAAAHAgH/gQD5FBQD+RQG/wAA/38A//8AAP8AAP//AAD/EADyAACAAA==",
                                "sceneCode": 10191,
                            }
                        ],
                    },
                    {
                        "categoryName": "Natural",
                        "scenes": [
                            {
                                "sceneName": "Aurora",
                                "lightEffects": [
                                    {"sceneCode": 456, "scenceParam": ""}
                                ],
                            }
                        ],
                    },
                ]
            },
        }
        parsed = scenes_mod.parse_govee_library_json(mock_library)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["name"], "Rainbow B")
        self.assertEqual(parsed[0]["scene_code"], 10191)
        self.assertTrue(parsed[0]["scene_param"])
        self.assertEqual(parsed[1]["name"], "Aurora")
        self.assertEqual(parsed[1]["category"], "Natural")
        self.assertEqual(parsed[1]["scene_code"], 456)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "scenes.json"
            tmp_path.write_text(json.dumps({"scenes": parsed}), encoding="utf-8")
            loaded = scenes_mod.load_h60a6_scenes(tmp_path)
            self.assertIn("Scene: Rainbow B", loaded)
            self.assertIn("Scene: Aurora", loaded)
            self.assertEqual(loaded["Scene: Rainbow B"].scene_code, 10191)


if __name__ == "__main__":
    unittest.main()
