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
from typing import Any

from .const import ALL_RING_SEGMENTS_MASK, MAX_KELVIN, MIN_KELVIN, RING_SEGMENT_COUNT


@dataclass(slots=True)
class DiscoveredDevice:
    """Device information parsed from a Govee LAN `scan` response."""

    ip: str
    device_id: str
    sku: str
    ble_version_hard: str = ""
    ble_version_soft: str = ""
    wifi_version_hard: str = ""
    wifi_version_soft: str = ""


@dataclass(slots=True)
class GoveeDeviceStatus:
    """State snapshot parsed from a Govee LAN `devStatus` response."""

    is_on: bool
    brightness: int  # 1..100
    rgb: tuple[int, int, int]
    color_temp_kelvin: int  # 0 when in RGB mode, otherwise 2200..6500


@dataclass(slots=True)
class SegmentState:
    """Tracked state for an individual ring segment or sub-zone."""

    is_on: bool = True
    brightness: int = 100  # 0..100
    rgb: tuple[int, int, int] = (255, 255, 255)


@dataclass(slots=True)
class GoveeState:
    """Aggregated state tracked for a Govee H60A6 fixture."""

    is_on: bool = False
    brightness: int = 100  # 1..100
    rgb: tuple[int, int, int] = (255, 255, 255)
    color_temp_kelvin: int = 4000  # 0 means RGB active
    effect: str | None = None
    ring: SegmentState = field(default_factory=SegmentState)
    segments: list[SegmentState] = field(
        default_factory=lambda: [SegmentState() for _ in range(RING_SEGMENT_COUNT)]
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
    """Build 20-byte static RGB packet (`33 05 02 R G B`)."""
    return build_ble_packet(
        [0x33, 0x05, 0x02, clamp(r, 0, 255), clamp(g, 0, 255), clamp(b, 0, 255)]
    )


def build_ble_color_temp_packet(kelvin: int) -> bytes:
    """Build 20-byte white color temperature packet (`33 05 02 FF FF FF 01 R G B`).

    Sets the RGB lock bytes to `FF FF FF` with white-mode flag `0x01` followed by
    the warm/cool RGB white balance target for the requested Kelvin value.
    """
    clamped_k = clamp(kelvin, MIN_KELVIN, MAX_KELVIN)
    r, g, b = kelvin_to_rgb(clamped_k)
    return build_ble_packet([0x33, 0x05, 0x02, 0xFF, 0xFF, 0xFF, 0x01, r, g, b])


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
    """Build 20-byte hardware scene activation packet (`33 05 04 <id>`)."""
    if scene_id < 0 or scene_id > 0xFFFF:
        raise ValueError(f"scene_id must be 0..65535 (got {scene_id})")
    if scene_id <= 0xFF:
        return build_ble_packet([0x33, 0x05, 0x04, scene_id])
    return build_ble_packet([0x33, 0x05, 0x04, (scene_id >> 8) & 0xFF, scene_id & 0xFF])


def build_ble_music_mode_packet(mode_id: int, sensitivity: int = 50) -> bytes:
    """Build 20-byte onboard music mode packet (`33 05 13 <mode> <sensitivity>`)."""
    return build_ble_packet(
        [0x33, 0x05, 0x13, clamp(mode_id, 0, 255), clamp(sensitivity, 1, 100)]
    )


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
