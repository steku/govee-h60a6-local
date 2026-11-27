"""Pure-Python Govee H60A6 local protocol encoders and decoders.

Supports:
- Official Govee LAN UDP JSON API (scan, devStatus, turn, brightness, colorwc)
- Govee ptReal 20-byte XOR-checksummed binary frames (over LAN UDP or BLE GATT)
- Govee Razer/DreamView high-speed local UDP segment streaming (0xB1 / 0xB0)
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import math
import os
from typing import Any

from .const import (
    ALL_FIXTURE_SEGMENTS_MASK,
    ALL_RING_SEGMENTS_MASK,
    MAX_KELVIN,
    MIN_KELVIN,
    RING_SEGMENT_COUNT,
    TOTAL_SEGMENT_COUNT,
)


@dataclass
class DiscoveredDevice:
    """Device information parsed from a Govee LAN `scan` response."""

    ip: str
    device_id: str
    sku: str
    ble_version_hard: str = ""
    ble_version_soft: str = ""
    wifi_version_hard: str = ""
    wifi_version_soft: str = ""


@dataclass
class GoveeDeviceStatus:
    """State snapshot parsed from a Govee LAN `devStatus` response."""

    is_on: bool
    brightness: int  # 1..100
    rgb: tuple[int, int, int]
    color_temp_kelvin: int  # 0 when in RGB mode, otherwise 2200..6500


@dataclass
class GoveeBleStatus:
    """Full state snapshot parsed from a BLE `ac 03 03 41 30 a5` status query."""

    is_on: bool
    brightness: int  # 1..100
    color_temp_kelvin: int  # 0 when in RGB/scene mode, otherwise 2200..6500
    rgb: tuple[int, int, int]
    scene_code: int | None = None
    ring_segments: list[tuple[int, tuple[int, int, int]]] = field(default_factory=list)
    center_on: bool | None = None
    center_rgb: tuple[int, int, int] | None = None


@dataclass
class SegmentState:
    """Tracked state for an individual ring segment or sub-zone."""

    is_on: bool = True
    brightness: int = 100  # 0..100
    rgb: tuple[int, int, int] = (255, 255, 255)
    color_temp_kelvin: int = 0  # 0 = RGB mode, 2200..6500 = dedicated white channel


@dataclass
class GoveeState:
    """Aggregated state tracked for a Govee H60A6 fixture."""

    is_on: bool = False
    brightness: int = 100  # 1..100
    rgb: tuple[int, int, int] = (255, 255, 255)
    color_temp_kelvin: int = 4000  # 0 means RGB active
    effect: str | None = None
    ring: SegmentState = field(default_factory=SegmentState)
    segments: list[SegmentState] = field(
        default_factory=lambda: [SegmentState() for _ in range(TOTAL_SEGMENT_COUNT)]
    )
    active_transport: str = "offline"
    available: bool = False
    last_error: str | None = None
    razer_active: bool = False
    device_id: str | None = None
    sku: str = "H60A6"
    wifi_version: str | None = None
    ble_version: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clamp(value: int, low: int, high: int) -> int:
    """Clamp an integer to [low, high]."""
    return max(low, min(high, int(value)))


def ha_brightness_to_govee(ha_brightness: int) -> int:
    """Convert Home Assistant 0..255 brightness to Govee 1..100 percentage."""
    if ha_brightness <= 0:
        return 1
    return clamp(round((ha_brightness / 255.0) * 100.0), 1, 100)


def govee_brightness_to_ha(govee_brightness: int) -> int:
    """Convert Govee 0..100 percentage to Home Assistant 0..255 brightness."""
    return clamp(round((govee_brightness / 100.0) * 255.0), 0, 255)


def kelvin_to_rgb(kelvin: int) -> tuple[int, int, int]:
    """Approximate RGB values for a color temperature in Kelvin (Tanner Helland algorithm).

    Used by Govee BLE white-balance packets and local color previews.
    """
    temp = clamp(kelvin, 1000, 40000) / 100.0

    if temp <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(max(temp, 1.0)) - 161.1195681661
        if temp <= 19:
            blue = 0.0
        else:
            blue = 138.5177312231 * math.log(temp - 10.0) - 305.0447927307
    else:
        red = 329.698727446 * ((temp - 60.0) ** -0.1332047592)
        green = 288.1221695283 * ((temp - 60.0) ** -0.0755148492)
        blue = 255.0

    return (
        clamp(round(red), 0, 255),
        clamp(round(green), 0, 255),
        clamp(round(blue), 0, 255),
    )


def segments_to_bitmask(segments: list[int] | set[int]) -> int:
    """Convert 1-based segment indices (1..12) to a 16-bit Govee bitmask."""
    mask = 0
    for seg in segments:
        if 1 <= seg <= 16:
            mask |= 1 << (seg - 1)
    return mask


# ---------------------------------------------------------------------------
# Standard Govee LAN UDP JSON Payloads
# ---------------------------------------------------------------------------


def encode_lan_message(cmd: str, data: dict[str, Any]) -> bytes:
    """Encode a Govee LAN API command to UTF-8 JSON bytes."""
    return json.dumps({"msg": {"cmd": cmd, "data": data}}, separators=(",", ":")).encode(
        "utf-8"
    )


def build_scan_payload() -> bytes:
    """Build a Govee LAN discovery scan message (UDP port 4001)."""
    return encode_lan_message("scan", {"account_topic": "reserve"})


def build_status_payload() -> bytes:
    """Build a Govee LAN device status request (UDP port 4003)."""
    return encode_lan_message("devStatus", {})


def build_turn_payload(is_on: bool) -> bytes:
    """Build a Govee LAN power on/off command."""
    return encode_lan_message("turn", {"value": 1 if is_on else 0})


def build_brightness_payload(brightness_pct: int) -> bytes:
    """Build a Govee LAN brightness command (1..100)."""
    return encode_lan_message("brightness", {"value": clamp(brightness_pct, 1, 100)})


def build_color_payload(r: int, g: int, b: int) -> bytes:
    """Build a Govee LAN `colorwc` RGB command."""
    return encode_lan_message(
        "colorwc",
        {
            "color": {
                "r": clamp(r, 0, 255),
                "g": clamp(g, 0, 255),
                "b": clamp(b, 0, 255),
            },
            "colorTemInKelvin": 0,
        },
    )


def build_color_temp_payload(kelvin: int) -> bytes:
    """Build a Govee LAN `colorwc` Kelvin color temperature command."""
    clamped_k = clamp(kelvin, MIN_KELVIN, MAX_KELVIN)
    return encode_lan_message(
        "colorwc",
        {
            "color": {"r": 0, "g": 0, "b": 0},
            "colorTemInKelvin": clamped_k,
        },
    )


def build_ptreal_payload(packets: list[bytes]) -> bytes:
    """Wrap one or more 20-byte Govee BLE packets in a LAN `ptReal` JSON frame."""
    encoded = [base64.b64encode(pkt).decode("ascii") for pkt in packets]
    return encode_lan_message("ptReal", {"command": encoded})


def build_razer_payload(raw_frame: bytes) -> bytes:
    """Wrap a Govee Razer/DreamView binary frame in a LAN `razer` JSON payload."""
    return encode_lan_message(
        "razer", {"pt": base64.b64encode(raw_frame).decode("ascii")}
    )


# ---------------------------------------------------------------------------
# 20-Byte BLE / ptReal Binary Frame Builders
# ---------------------------------------------------------------------------


def compute_xor_checksum(data: bytes | bytearray) -> int:
    """Calculate cumulative XOR checksum over all bytes in `data`."""
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum & 0xFF


def build_ble_packet(payload: bytes | list[int]) -> bytes:
    """Build a compliant 20-byte Govee BLE / ptReal packet with XOR checksum.

    - Truncates or zero-pads payload to 19 bytes.
    - Appends the XOR checksum as byte 19 (20th byte).
    """
    raw = bytearray(payload[:19])
    if len(raw) < 19:
        raw.extend(bytes(19 - len(raw)))
    raw.append(compute_xor_checksum(raw))
    return bytes(raw)


def parse_hex_to_ble_packet(hex_str: str) -> bytes:
    """Parse a hex string (with optional spaces/colons/0x) into a valid 20-byte packet.

    If 1..19 bytes are provided, automatically zero-pads and appends XOR checksum.
    If 20 bytes are provided, validates/returns exact 20 bytes (or recomputes checksum
    only when less than 20 bytes were supplied).
    """
    cleaned = hex_str.replace("0x", "").replace("0X", "").replace(":", "").replace(" ", "").strip()
    if not cleaned or len(cleaned) % 2 != 0:
        raise ValueError(f"Invalid hex string: {hex_str!r}")
    raw = bytes.fromhex(cleaned)
    if len(raw) == 20:
        return raw
    if len(raw) > 20:
        raise ValueError(f"Govee BLE packet cannot exceed 20 bytes (got {len(raw)})")
    return build_ble_packet(raw)


def build_ble_keepalive_packet() -> bytes:
    """Build BLE heartbeat packet (`aa 01 ...`)."""
    return build_ble_packet([0xAA, 0x01])


def build_ble_power_packet(is_on: bool) -> bytes:
    """Build 20-byte power packet (`33 01 01/00`)."""
    return build_ble_packet([0x33, 0x01, 0x01 if is_on else 0x00])


def build_ble_brightness_packet(brightness_pct: int) -> bytes:
    """Build 20-byte brightness packet (`33 04 <0..100>`)."""
    return build_ble_packet([0x33, 0x04, clamp(brightness_pct, 1, 100)])


def build_ble_rgb_packet(r: int, g: int, b: int) -> bytes:
    """Build 20-byte full-fixture RGB packet (`33 05 15 01` with mask `0x1FFF`)."""
    return build_ble_segment_color_packet(r, g, b, ALL_FIXTURE_SEGMENTS_MASK)


def build_ble_segment_color_temp_packet(
    kelvin: int,
    segment_mask: int = ALL_FIXTURE_SEGMENTS_MASK,
) -> bytes:
    """Build 20-byte dedicated WW/CW Kelvin segment packet (`33 05 15 01`).

    Layout (verified against Govee app H60A6 BLE captures at 3000K/4000K/6500K):
      [0..3]   33 05 15 01
      [4..6]   FF FF FF (white-mode RGB sentinel so full WW/CW output stays bright)
      [7..8]   Kelvin big-endian (k_hi, k_lo)
      [9..11]  Warm/cool white RGB approximation (wr, wg, wb)
      [12..13] 16-bit little-endian segment bitmask (mask_lo, mask_hi)
      [14..18] 00 00 00 00 00
      [19]     XOR checksum
    """
    clamped_k = clamp(kelvin, MIN_KELVIN, MAX_KELVIN)
    wr, wg, wb = kelvin_to_rgb(clamped_k)
    k_hi = (clamped_k >> 8) & 0xFF
    k_lo = clamped_k & 0xFF
    mask = segment_mask & 0xFFFF
    mask_lo = mask & 0xFF
    mask_hi = (mask >> 8) & 0xFF
    return build_ble_packet(
        [
            0x33,
            0x05,
            0x15,
            0x01,
            0xFF,
            0xFF,
            0xFF,
            k_hi,
            k_lo,
            wr,
            wg,
            wb,
            mask_lo,
            mask_hi,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
        ]
    )


def build_ble_color_temp_packet(kelvin: int) -> bytes:
    """Build 20-byte full-fixture dedicated WW/CW white color temperature packet."""
    return build_ble_segment_color_temp_packet(kelvin, ALL_FIXTURE_SEGMENTS_MASK)


def build_ble_segment_color_packet(
    r: int,
    g: int,
    b: int,
    segment_mask: int = ALL_RING_SEGMENTS_MASK,
) -> bytes:
    """Build 20-byte RGBIC segment color packet (`33 05 15 01`).

    Layout:
      [0..3]  33 05 15 01
      [4..6]  R G B
      [7..11] 00 00 00 00 00
      [12..13] 16-bit little-endian segment bitmask (mask_lo, mask_hi)
      [14..18] 00 00 00 00 00
      [19]    XOR checksum
    """
    mask = segment_mask & 0xFFFF
    mask_lo = mask & 0xFF
    mask_hi = (mask >> 8) & 0xFF
    return build_ble_packet(
        [
            0x33,
            0x05,
            0x15,
            0x01,
            clamp(r, 0, 255),
            clamp(g, 0, 255),
            clamp(b, 0, 255),
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            mask_lo,
            mask_hi,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
        ]
    )


def build_ble_segment_color_v2_packet(
    r: int,
    g: int,
    b: int,
    segment_mask: int = ALL_RING_SEGMENTS_MASK,
) -> bytes:
    """Build 20-byte RGBIC v2 segment color packet (`33 05 0b`).

    Layout:
      [0..2]  33 05 0b
      [3..5]  R G B
      [6..7]  00 00 (color temp = 0 for RGB)
      [8..9]  16-bit little-endian segment bitmask (mask_lo, mask_hi)
      [10..18] 00 ...
      [19]    XOR checksum
    """
    mask = segment_mask & 0xFFFF
    mask_lo = mask & 0xFF
    mask_hi = (mask >> 8) & 0xFF
    return build_ble_packet(
        [
            0x33,
            0x05,
            0x0B,
            clamp(r, 0, 255),
            clamp(g, 0, 255),
            clamp(b, 0, 255),
            0x00,
            0x00,
            mask_lo,
            mask_hi,
        ]
    )


def build_ble_segment_brightness_packet(
    brightness_pct: int,
    segment_mask: int = ALL_RING_SEGMENTS_MASK,
) -> bytes:
    """Build 20-byte RGBIC segment brightness packet (`33 05 15 02`).

    Layout:
      [0..3]  33 05 15 02
      [4]     brightness percentage (0..100)
      [5..6]  16-bit little-endian segment bitmask (mask_lo, mask_hi)
      [7..18] 00 ...
      [19]    XOR checksum
    """
    mask = segment_mask & 0xFFFF
    mask_lo = mask & 0xFF
    mask_hi = (mask >> 8) & 0xFF
    return build_ble_packet(
        [
            0x33,
            0x05,
            0x15,
            0x02,
            clamp(brightness_pct, 0, 100),
            mask_lo,
            mask_hi,
        ]
    )


def build_ble_scene_packet(scene_id: int) -> bytes:
    """Build 20-byte hardware scene activation packet (`33 05 04 <lo> <hi>`).

    Govee encodes 16-bit scene codes in little-endian order.
    """
    if scene_id < 0 or scene_id > 0xFFFF:
        raise ValueError(f"scene_id must be 0..65535 (got {scene_id})")
    return build_ble_packet([0x33, 0x05, 0x04, scene_id & 0xFF, (scene_id >> 8) & 0xFF])


def build_ble_multipacket_scene_packets(
    scene_code: int, scene_param_b64: str | None = None
) -> list[bytes]:
    """Build `0xA3` multi-packet parameter upload frames + `33 05 04` activation frame.

    When `scene_param_b64` is empty or None, returns `[build_ble_scene_packet(scene_code)]`.
    Otherwise decodes the base64 `scenceParam` payload, translates Govee cloud's
    `0x50` stream header byte to `0x58` (verified against the Govee app's H60A6
    BLE capture), splits it across 20-byte `0xA3` frames (15 bytes in line 0 after
    `[0xA3, 0x00, 0x01, total_lines]`, 17 bytes per subsequent frame with final
    frame index `0xFF`), and appends the little-endian `33 05 04` activation packet.
    """
    if not scene_param_b64:
        return [build_ble_scene_packet(scene_code)]

    raw = base64.b64decode(scene_param_b64)
    if not raw:
        return [build_ble_scene_packet(scene_code)]

    # H60A6 BLE firmware expects header byte 0x58 for uploaded scene_param streams
    # (Govee's light-effect-libraries API stores 0x50; sending 0x50 returns error a3 50 0a).
    if raw[0] == 0x50:
        raw = bytes([0x58]) + raw[1:]

    # Multi-layer 0x20 scenes use variable-length 0xA4 frames followed by a 4-byte
    # `a4 ff ff a4` terminator (verified against H60A6 BLE capture [0676]/[0677] for Sunrise 19075).
    # In legacy 0xA3 scenes like 35-byte Halloween B (19096), bytes 1..2 are the little-endian
    # payload length (`len(raw) - 3`), which happens to equal `0x0020` (= 32).
    is_a4_scene = (
        len(raw) >= 3
        and raw[1] == 0x20
        and int.from_bytes(raw[1:3], "little") != len(raw) - 3
    )
    if is_a4_scene:
        first_a4 = raw[:237]
        rem_a4 = raw[237:]
        tail_a4 = (
            [rem_a4[i : i + 240] for i in range(0, len(rem_a4), 240)]
            if rem_a4
            else [b""]
        )
        total_packets = 1 + len(tail_a4)
        head_body = (
            bytes(
                [
                    0xA4,
                    0x00,
                    0x00,
                    0x01,
                    total_packets & 0xFF,
                    (total_packets >> 8) & 0xFF,
                ]
            )
            + first_a4
        )
        frames = [head_body + bytes([compute_xor_checksum(head_body)])]
        for idx, chunk in enumerate(tail_a4, start=1):
            seq_lo, seq_hi = (
                (0xFF, 0xFF)
                if idx == len(tail_a4)
                else (idx & 0xFF, (idx >> 8) & 0xFF)
            )
            chunk_body = bytes([0xA4, seq_lo, seq_hi]) + chunk
            frames.append(chunk_body + bytes([compute_xor_checksum(chunk_body)]))
        frames.append(build_ble_scene_packet(scene_code))
        return frames

    first_chunk = raw[:15]
    remainder = raw[15:]
    tail_chunks = [remainder[i : i + 17] for i in range(0, len(remainder), 17)]
    total_lines = 1 + len(tail_chunks)
    if total_lines > 0xFF:
        raise ValueError(f"Scene parameter payload too large ({total_lines} lines)")

    frames: list[bytes] = [
        build_ble_packet([0xA3, 0x00, 0x01, total_lines, *first_chunk])
    ]
    for idx, chunk in enumerate(tail_chunks, start=1):
        line_idx = 0xFF if idx == total_lines - 1 else idx
        frames.append(build_ble_packet([0xA3, line_idx, *chunk]))

    frames.append(build_ble_scene_packet(scene_code))
    return frames


def build_ble_music_mode_packet(mode_id: int, sensitivity: int = 50) -> bytes:
    """Build 20-byte onboard music mode packet (`33 05 13 <mode> <sensitivity>`)."""
    return build_ble_packet(
        [0x33, 0x05, 0x13, clamp(mode_id, 0, 255), clamp(sensitivity, 1, 100)]
    )


# ---------------------------------------------------------------------------
# Govee BLE Session Encryption (AES-128-ECB + RC4) & Handshake Packets
# ---------------------------------------------------------------------------

_AES_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

_AES_INV_SBOX = (
    0x52, 0x09, 0x6A, 0xD5, 0x30, 0x36, 0xA5, 0x38, 0xBF, 0x40, 0xA3, 0x9E, 0x81, 0xF3, 0xD7, 0xFB,
    0x7C, 0xE3, 0x39, 0x82, 0x9B, 0x2F, 0xFF, 0x87, 0x34, 0x8E, 0x43, 0x44, 0xC4, 0xDE, 0xE9, 0xCB,
    0x54, 0x7B, 0x94, 0x32, 0xA6, 0xC2, 0x23, 0x3D, 0xEE, 0x4C, 0x95, 0x0B, 0x42, 0xFA, 0xC3, 0x4E,
    0x08, 0x2E, 0xA1, 0x66, 0x28, 0xD9, 0x24, 0xB2, 0x76, 0x5B, 0xA2, 0x49, 0x6D, 0x8B, 0xD1, 0x25,
    0x72, 0xF8, 0xF6, 0x64, 0x86, 0x68, 0x98, 0x16, 0xD4, 0xA4, 0x5C, 0xCC, 0x5D, 0x65, 0xB6, 0x92,
    0x6C, 0x70, 0x48, 0x50, 0xFD, 0xED, 0xB9, 0xDA, 0x5E, 0x15, 0x46, 0x57, 0xA7, 0x8D, 0x9D, 0x84,
    0x90, 0xD8, 0xAB, 0x00, 0x8C, 0xBC, 0xD3, 0x0A, 0xF7, 0xE4, 0x58, 0x05, 0xB8, 0xB3, 0x45, 0x06,
    0xD0, 0x2C, 0x1E, 0x8F, 0xCA, 0x3F, 0x0F, 0x02, 0xC1, 0xAF, 0xBD, 0x03, 0x01, 0x13, 0x8A, 0x6B,
    0x3A, 0x91, 0x11, 0x41, 0x4F, 0x67, 0xDC, 0xEA, 0x97, 0xF2, 0xCF, 0xCE, 0xF0, 0xB4, 0xE6, 0x73,
    0x96, 0xAC, 0x74, 0x22, 0xE7, 0xAD, 0x35, 0x85, 0xE2, 0xF9, 0x37, 0xE8, 0x1C, 0x75, 0xDF, 0x6E,
    0x47, 0xF1, 0x1A, 0x71, 0x1D, 0x29, 0xC5, 0x89, 0x6F, 0xB7, 0x62, 0x0E, 0xAA, 0x18, 0xBE, 0x1B,
    0xFC, 0x56, 0x3E, 0x4B, 0xC6, 0xD2, 0x79, 0x20, 0x9A, 0xDB, 0xC0, 0xFE, 0x78, 0xCD, 0x5A, 0xF4,
    0x1F, 0xDD, 0xA8, 0x33, 0x88, 0x07, 0xC7, 0x31, 0xB1, 0x12, 0x10, 0x59, 0x27, 0x80, 0xEC, 0x5F,
    0x60, 0x51, 0x7F, 0xA9, 0x19, 0xB5, 0x4A, 0x0D, 0x2D, 0xE5, 0x7A, 0x9F, 0x93, 0xC9, 0x9C, 0xEF,
    0xA0, 0xE0, 0x3B, 0x4D, 0xAE, 0x2A, 0xF5, 0xB0, 0xC8, 0xEB, 0xBB, 0x3C, 0x83, 0x53, 0x99, 0x61,
    0x17, 0x2B, 0x04, 0x7E, 0xBA, 0x77, 0xD6, 0x26, 0xE1, 0x69, 0x14, 0x63, 0x55, 0x21, 0x0C, 0x7D,
)

_AES_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)


def _gf_mul(a: int, b: int) -> int:
    res = 0
    for _ in range(8):
        if b & 1:
            res ^= a
        a = _xtime(a)
        b >>= 1
    return res


def _aes128_expand_key(key: bytes) -> list[list[int]]:
    rk = list(key)
    for i in range(10):
        t0, t1, t2, t3 = rk[-4], rk[-3], rk[-2], rk[-1]
        t0, t1, t2, t3 = (
            _AES_SBOX[t1] ^ _AES_RCON[i],
            _AES_SBOX[t2],
            _AES_SBOX[t3],
            _AES_SBOX[t0],
        )
        base = len(rk) - 16
        rk.extend([rk[base] ^ t0, rk[base + 1] ^ t1, rk[base + 2] ^ t2, rk[base + 3] ^ t3])
        for _ in range(3):
            base = len(rk) - 16
            rk.extend(
                [
                    rk[base] ^ rk[-4],
                    rk[base + 1] ^ rk[-3],
                    rk[base + 2] ^ rk[-2],
                    rk[base + 3] ^ rk[-1],
                ]
            )
    return [rk[i : i + 16] for i in range(0, 176, 16)]


def _aes128_encrypt_block(block: bytes, key: bytes) -> bytes:
    rounds = _aes128_expand_key(key)
    s = [b ^ k for b, k in zip(block, rounds[0])]
    for r in range(1, 11):
        s = [_AES_SBOX[b] for b in s]
        s = [
            s[0], s[5], s[10], s[15],
            s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7],
            s[12], s[1], s[6], s[11],
        ]
        if r < 10:
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                t = a0 ^ a1 ^ a2 ^ a3
                s[c] = a0 ^ t ^ _xtime(a0 ^ a1)
                s[c + 1] = a1 ^ t ^ _xtime(a1 ^ a2)
                s[c + 2] = a2 ^ t ^ _xtime(a2 ^ a3)
                s[c + 3] = a3 ^ t ^ _xtime(a3 ^ a0)
        rk = rounds[r]
        s = [b ^ k for b, k in zip(s, rk)]
    return bytes(s)


def _aes128_decrypt_block(block: bytes, key: bytes) -> bytes:
    rounds = _aes128_expand_key(key)
    s = [b ^ k for b, k in zip(block, rounds[10])]
    for r in range(9, -1, -1):
        s = [
            s[0], s[13], s[10], s[7],
            s[4], s[1], s[14], s[11],
            s[8], s[5], s[2], s[15],
            s[12], s[9], s[6], s[3],
        ]
        s = [_AES_INV_SBOX[b] for b in s]
        rk = rounds[r]
        s = [b ^ k for b, k in zip(s, rk)]
        if r > 0:
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                s[c] = _gf_mul(a0, 14) ^ _gf_mul(a1, 11) ^ _gf_mul(a2, 13) ^ _gf_mul(a3, 9)
                s[c + 1] = _gf_mul(a0, 9) ^ _gf_mul(a1, 14) ^ _gf_mul(a2, 11) ^ _gf_mul(a3, 13)
                s[c + 2] = _gf_mul(a0, 13) ^ _gf_mul(a1, 9) ^ _gf_mul(a2, 14) ^ _gf_mul(a3, 11)
                s[c + 3] = _gf_mul(a0, 11) ^ _gf_mul(a1, 13) ^ _gf_mul(a2, 9) ^ _gf_mul(a3, 14)
    return bytes(s)


def _rc4_crypt(data: bytes, key: bytes) -> bytes:
    s = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % klen]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = 0
    j = 0
    out = bytearray(len(data))
    for idx, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[idx] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def _crypt_ble_blocks(packet: bytes, key: bytes, encrypt: bool) -> bytes:
    """Encrypt/decrypt each full 16-byte block with AES-128-ECB and any remainder with RC4."""
    if len(key) != 16:
        raise ValueError(f"Govee BLE key must be 16 bytes (got {len(key)})")
    if not packet:
        raise ValueError("Govee BLE packet cannot be empty")
    block_fn = _aes128_encrypt_block if encrypt else _aes128_decrypt_block
    n_blocks = len(packet) // 16
    out = bytearray()
    for blk_idx in range(n_blocks):
        out.extend(block_fn(packet[blk_idx * 16 : (blk_idx + 1) * 16], key))
    tail_offset = n_blocks * 16
    if tail_offset < len(packet):
        out.extend(_rc4_crypt(packet[tail_offset:], key))
    return bytes(out)


def govee_ble_encrypt(packet: bytes, key: bytes) -> bytes:
    """Encrypt a Govee BLE packet with AES-128-ECB per 16-byte block + RC4 tail."""
    return _crypt_ble_blocks(packet, key, encrypt=True)


def govee_ble_decrypt(packet: bytes, key: bytes) -> bytes:
    """Decrypt a Govee BLE packet with AES-128-ECB per 16-byte block + RC4 tail."""
    return _crypt_ble_blocks(packet, key, encrypt=False)


def build_ble_handshake_packet(step: int, nonce: bytes | None = None) -> bytes:
    """Build a 20-byte Govee BLE session handshake packet (`E7 <step>` + 17-byte nonce + XOR)."""
    pad = nonce[:17] if nonce is not None else os.urandom(17)
    if len(pad) < 17:
        pad = pad + bytes(17 - len(pad))
    return build_ble_packet([0xE7, step & 0xFF, *pad])


# ---------------------------------------------------------------------------
# Razer / DreamView High-Speed Local Streaming Frames
# ---------------------------------------------------------------------------


def build_razer_toggle_frame(enable: bool) -> bytes:
    """Build Razer/DreamView stream enable (`0xB1 0x01`) or disable (`0xB1 0x00`) frame.

    Format: `0xBB | len_hi | len_lo | 0xB1 | state | xor_checksum`
    """
    body = bytes([0xBB, 0x00, 0x01, 0xB1, 0x01 if enable else 0x00])
    return body + bytes([compute_xor_checksum(body)])


def build_razer_color_frame(
    colors: list[tuple[int, int, int]],
    gradient: bool = True,
) -> bytes:
    """Build Razer/DreamView multi-segment RGB streaming frame (`0xB0`).

    Format: `0xBB | len_hi | len_lo | 0xB0 | flags | count | R1 G1 B1 ... | xor_checksum`
    where `len` = 2 + 3 * len(colors) (bytes after 0xB0: flags + count + RGB data).
    """
    if not colors:
        raise ValueError("colors must contain at least one RGB tuple")
    count = min(len(colors), 255)
    payload = bytearray([0x01 if gradient else 0x00, count])
    for r, g, b in colors[:count]:
        payload.extend([clamp(r, 0, 255), clamp(g, 0, 255), clamp(b, 0, 255)])
    data_len = len(payload)
    header = bytes([0xBB, (data_len >> 8) & 0xFF, data_len & 0xFF, 0xB0])
    frame_without_crc = header + bytes(payload)
    return frame_without_crc + bytes([compute_xor_checksum(frame_without_crc)])


# ---------------------------------------------------------------------------
# LAN Response Decoders
# ---------------------------------------------------------------------------


def parse_lan_response(raw_bytes: bytes) -> tuple[str, dict[str, Any]] | None:
    """Parse an incoming UDP JSON payload from a Govee device."""
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    msg = parsed.get("msg")
    if not isinstance(msg, dict):
        return None
    cmd = msg.get("cmd")
    data = msg.get("data")
    if not isinstance(cmd, str) or not isinstance(data, dict):
        return None
    return cmd, data


def parse_scan_response(data: dict[str, Any], sender_ip: str) -> DiscoveredDevice | None:
    """Convert `scan` response data dict into a `DiscoveredDevice`."""
    ip = str(data.get("ip") or sender_ip)
    device_id = str(data.get("device") or "")
    sku = str(data.get("sku") or "")
    if not ip:
        return None
    return DiscoveredDevice(
        ip=ip,
        device_id=device_id,
        sku=sku,
        ble_version_hard=str(data.get("bleVersionHard") or ""),
        ble_version_soft=str(data.get("bleVersionSoft") or ""),
        wifi_version_hard=str(data.get("wifiVersionHard") or ""),
        wifi_version_soft=str(data.get("wifiVersionSoft") or ""),
    )


def parse_status_response(data: dict[str, Any]) -> GoveeDeviceStatus | None:
    """Convert `devStatus` response data dict into a `GoveeDeviceStatus`."""
    if "onOff" not in data:
        return None
    color_raw = data.get("color") or {}
    r = clamp(int(color_raw.get("r", 255)), 0, 255)
    g = clamp(int(color_raw.get("g", 255)), 0, 255)
    b = clamp(int(color_raw.get("b", 255)), 0, 255)
    brightness = clamp(int(data.get("brightness", 100)), 0, 100)
    kelvin = int(data.get("colorTemInKelvin", 0) or 0)
    return GoveeDeviceStatus(
        is_on=bool(int(data.get("onOff", 0))),
        brightness=brightness if brightness > 0 else 1,
        rgb=(r, g, b),
        color_temp_kelvin=kelvin,
    )


def build_ble_status_query_packet() -> bytes:
    """Build 20-byte BLE full-state query packet (`ac 03 03 41 30 a5`)."""
    return build_ble_packet([0xAC, 0x03, 0x03, 0x41, 0x30, 0xA5])


def parse_ble_status_frames(frames: list[bytes]) -> GoveeBleStatus | None:
    """Reassemble and decode multi-packet `0xAC` status notifications from the H60A6."""
    ac_frames = [f for f in frames if len(f) >= 19 and f[0] == 0xAC]
    if not ac_frames:
        return None

    head = next((f for f in ac_frames if f[1] == 0x00), None)
    if head is None:
        return None

    tail = [f for f in ac_frames if f is not head]
    tail.sort(key=lambda f: (1, 0) if f[1] == 0xFF else (0, f[1]))

    raw = bytearray(head[4:19])
    for frame in tail:
        raw.extend(frame[2:19])

    if len(raw) < 4:
        return None

    is_on = True
    brightness = 100
    color_temp_kelvin = 0
    scene_code: int | None = None

    # raw[0] = byte length of sub-TLV body following `raw[1:3]` (`03 00`)
    hdr_body_len = raw[0]
    hdr_end = min(len(raw), 3 + hdr_body_len)
    pos = 3
    while pos + 2 <= hdr_end:
        tag = raw[pos]
        length = raw[pos + 1]
        val = raw[pos + 2 : min(hdr_end, pos + 2 + length)]
        pos += 2 + length
        if tag == 0x01 and len(val) >= 1:
            is_on = bool(val[0])
        elif tag == 0x04 and len(val) >= 1:
            brightness = clamp(val[0], 1, 100) if val[0] > 0 else 1
        elif tag == 0x05 and val:
            if len(val) >= 4 and val[0] == 0x15 and val[1] == 0x01:
                k_raw = (val[2] << 8) | val[3]
                color_temp_kelvin = (
                    clamp(k_raw, MIN_KELVIN, MAX_KELVIN) if k_raw > 0 else 0
                )
                scene_code = None
            elif len(val) >= 3 and val[0] == 0x04:
                scene_code = val[1] | (val[2] << 8)
                color_temp_kelvin = 0

    ring_segments: list[tuple[int, tuple[int, int, int]]] = []
    center_on: bool | None = None
    center_rgb: tuple[int, int, int] | None = None

    a5_pos = raw.find(b"\xa5\x11\x01")
    if a5_pos != -1:
        # Three 19-byte blocks (`a5 11 01..03` + 4 segments * 4 bytes [br, r, g, b])
        for grp in range(3):
            grp_off = a5_pos + grp * 19
            if grp_off + 19 > len(raw):
                break
            if raw[grp_off : grp_off + 3] != bytes([0xA5, 0x11, grp + 1]):
                break
            for seg_i in range(4):
                off = grp_off + 3 + seg_i * 4
                ring_segments.append(
                    (
                        clamp(raw[off], 0, 100),
                        (raw[off + 1], raw[off + 2], raw[off + 3]),
                    )
                )
        ring_segments = ring_segments[:RING_SEGMENT_COUNT]

        center_off = a5_pos + 3 * 19
        if center_off + 7 <= len(raw) and raw[center_off : center_off + 3] == b"\xa5\x05\x04":
            center_on = bool(raw[center_off + 3])
            center_rgb = (
                raw[center_off + 4],
                raw[center_off + 5],
                raw[center_off + 6],
            )

    if color_temp_kelvin > 0:
        rgb = kelvin_to_rgb(color_temp_kelvin)
    elif center_rgb is not None and any(center_rgb):
        rgb = center_rgb
    else:
        first_ring_rgb = next((c for _, c in ring_segments if any(c)), None)
        rgb = first_ring_rgb if first_ring_rgb is not None else (255, 255, 255)

    return GoveeBleStatus(
        is_on=is_on,
        brightness=brightness,
        color_temp_kelvin=color_temp_kelvin,
        rgb=rgb,
        scene_code=scene_code,
        ring_segments=ring_segments,
        center_on=center_on,
        center_rgb=center_rgb,
    )
