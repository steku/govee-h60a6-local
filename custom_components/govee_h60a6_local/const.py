"""Constants for the Govee H60A6 Local integration."""

from __future__ import annotations

DOMAIN = "govee_h60a6_local"
MANUFACTURER = "Govee"
MODEL_SKU = "H60A6"
MODEL_NAME = "15-Inch RGBICWW Smart Ceiling Light Pro (H60A6)"

# Default network settings (Govee LAN API)
DEFAULT_NAME = "Govee H60A6 Ceiling Light"
GOVEE_DISCOVERY_MULTICAST = "239.255.255.250"
GOVEE_DISCOVERY_PORT = 4001
GOVEE_LISTEN_PORT = 4002
GOVEE_COMMAND_PORT = 4003

# Govee BLE GATT UUIDs
BLE_SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
BLE_WRITE_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
BLE_NOTIFY_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
BLE_HANDSHAKE_PSK = b"MakingLifeSmarte"

# H60A6 optical capabilities
MIN_KELVIN = 2200
MAX_KELVIN = 6500
RING_SEGMENT_COUNT = 12
ALL_RING_SEGMENTS_MASK = (1 << RING_SEGMENT_COUNT) - 1  # 0x0FFF
MAIN_CENTER_SEGMENT = 13
MAIN_CENTER_SEGMENT_MASK = 1 << (MAIN_CENTER_SEGMENT - 1)  # 0x1000 (bit 12 = main center)
MAIN_LIGHT_SEGMENT_MASK = MAIN_CENTER_SEGMENT_MASK  # 0x1000 (alias)
TOTAL_SEGMENT_COUNT = 13
ALL_SEGMENTS_MASK = (1 << TOTAL_SEGMENT_COUNT) - 1  # 0x1FFF
ALL_FIXTURE_SEGMENTS_MASK = ALL_SEGMENTS_MASK  # 0x1FFF (alias)

# Config entry / options keys
CONF_HOST = "host"
CONF_BLE_ADDRESS = "ble_address"
CONF_TRANSPORT_MODE = "transport_mode"
CONF_POLL_INTERVAL = "poll_interval"
CONF_COMMAND_DELAY_MS = "command_delay_ms"
CONF_ENABLE_SEGMENTS = "enable_segments"
CONF_LISTEN_PORT = "listen_port"
CONF_DEVICE_ID = "device_id"
CONF_SKU = "sku"
CONF_WIFI_VERSION = "wifi_version"
CONF_BLE_VERSION = "ble_version"

# Transport modes
TRANSPORT_WIFI_PREFERRED = "wifi_preferred"
TRANSPORT_WIFI_ONLY = "wifi_only"
TRANSPORT_BLE_ONLY = "ble_only"

TRANSPORT_MODES = [
    TRANSPORT_WIFI_PREFERRED,
    TRANSPORT_WIFI_ONLY,
    TRANSPORT_BLE_ONLY,
]

# Polling & pacing defaults (conservative for H60A6 firmware stability)
DEFAULT_POLL_INTERVAL = 15  # seconds
MIN_POLL_INTERVAL = 5
MAX_POLL_INTERVAL = 300
DEFAULT_COMMAND_DELAY_MS = 80
DEFAULT_LAN_TIMEOUT = 3.0
DEFAULT_BLE_IDLE_TIMEOUT = 30.0

# Service names
SERVICE_SET_SEGMENT_COLOR = "set_segment_color"
SERVICE_SEND_SCENE = "send_scene"
SERVICE_SEND_MUSIC_MODE = "send_music_mode"
SERVICE_SEND_RAW_PTREAL = "send_raw_ptreal"
SERVICE_SET_RAZER_SEGMENTS = "set_razer_segments"

# Local software dynamic effects (100% local, no cloud required)
EFFECT_SOLID = "Solid"
LOCAL_EFFECTS: tuple[str, ...] = (
    "Rainbow Flow",
    "Aurora Pulse",
    "Sunset Drift",
    "Ocean Wave",
    "Candle Flicker",
    "Forest Breeze",
    "Cyber Neon",
    "Warm Breathing",
)

# Native ptReal hardware scene presets (33 05 04 <id>)
# Note: Exact scene IDs can vary slightly by H60A6 firmware revision;
# users can also trigger any numeric scene ID via govee_h60a6_local.send_scene.
NATIVE_SCENES: dict[str, int] = {
    "Scene: Sunrise": 0x00,
    "Scene: Sunset": 0x01,
    "Scene: Forest": 0x02,
    "Scene: Ocean": 0x03,
    "Scene: Aurora": 0x04,
    "Scene: Rainbow": 0x05,
    "Scene: Starry Sky": 0x06,
    "Scene: Candlelight": 0x07,
    "Scene: Night Light": 0x08,
    "Scene: Reading": 0x09,
    "Scene: Movie": 0x0A,
    "Scene: Romantic": 0x0B,
    "Scene: Crossing": 0x0C,
    "Scene: Energetic": 0x0D,
    "Scene: Party": 0x0E,
}

# Native ptReal music modes (33 05 13 <mode> <sensitivity>)
NATIVE_MUSIC_MODES: dict[str, int] = {
    "Music: Energic": 0x00,
    "Music: Rhythm": 0x01,
    "Music: Spectrum": 0x02,
    "Music: Rolling": 0x03,
    "Music: Separation": 0x04,
    "Music: Hopping": 0x05,
}

ALL_EFFECTS: list[str] = [
    *LOCAL_EFFECTS,
    *NATIVE_SCENES.keys(),
    *NATIVE_MUSIC_MODES.keys(),
]
