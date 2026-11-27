# Govee H60A6 Local — Home Assistant Integration

100% local Home Assistant custom integration for the **Govee 15-Inch RGBICWW Smart Ceiling Light Pro (`H60A6`)**, with **Wi-Fi LAN preferred** (UDP ports `4001`–`4003`) and **Bluetooth LE** control/fallback via Home Assistant's native Bluetooth stack.

No Govee Cloud account, API key, or internet access is required at runtime.

---

## Features

- **Wi-Fi LAN Preferred + Encrypted BLE Hybrid Routing**
  - Uses Govee's local LAN UDP API (`devStatus`, `turn`, `brightness`, `colorwc`, `razer`) for instant main-light control and live state feedback over Wi-Fi.
  - If another Home Assistant integration or add-on (such as `govee_light_local` or `govee2mqtt`) already holds UDP port `4002`, the hub automatically falls back to **`wifi_send_only`** mode on an ephemeral UDP port so all outbound Wi-Fi control commands continue working without failing setup.
  - Because H60A6 firmware ignores `ptReal` segment/ring/scene commands over Wi-Fi UDP `4003`, pairing the light's **Bluetooth MAC address** (via Home Assistant's local Bluetooth adapter or an ESPHome Bluetooth proxy) routes **Main Center**, **Ambient Ring**, **12 ring segments**, and **native scenes/music modes** locally over an encrypted BLE GATT session (`E701`/`E702` handshake with AES-128-ECB + RC4), while keeping main-light controls on Wi-Fi (with automatic BLE fallback if Wi-Fi drops).
  - Built-in command pacing (`80ms` default) and gentle polling (`15s` default) to prevent firmware freezes on H60A6 hardware.
- **Full Main Fixture Control (`light.<name>`)**
  - On / Off
  - Brightness (`1%–100%`)
  - Full RGB color wheel (`colorwc` over Wi-Fi / `33 05 15 01` mask `0x1FFF` over BLE)
  - Tunable Warm-to-Cool White (`2200K–6500K`) using dedicated WW/CW LEDs (`colorwc` over Wi-Fi / `33 05 15 01` over BLE)
  - Smooth software transitions (`transition` parameter)
  - **75 native H60A6 hardware scenes** (bundled offline) + **8 local software dynamic effects** (*Rainbow Flow*, *Aurora Pulse*, *Sunset Drift*, *Ocean Wave*, *Candle Flicker*, *Forest Breeze*, *Cyber Neon*, *Warm Breathing*)
- **Dual-Zone & Per-Segment Entities (with State Persistence)**
  - **Main Center** (`light.<name>_main_center`): Dedicated center panel (segment `13`, mask `0x1000`) with full RGB and `2200K–6500K` dedicated WW/CW Kelvin control.
  - **Ambient Ring** (`light.<name>_ambient_ring`): Backlight halo ring (segments `1–12`, mask `0x0FFF`) with full RGB, `2200K–6500K` Kelvin control, and per-segment color memory across off/on.
  - **Ring Segment 1–12** (`light.<name>_ring_segment_1`..`12`): Optional individual RGB entities (enabled via **Configure -> Enable Individual Ring Segments**) using `33 05 15 01` with 16-bit little-endian segment bitmasks.
  - Center, Ring, and Segment entities implement `RestoreEntity` and stay synchronized with whole-light and `devStatus` updates.
- **Home Assistant Integrations Icon (`brand/`)**
  - Includes bundled `brand/icon.png`, `brand/icon@2x.png`, `brand/dark_icon.png`, and `brand/dark_icon@2x.png` for Home Assistant 2026.3+ local integration icons.
- **Advanced Services**
  - `govee_h60a6_local.set_segment_color` — Color any combination of segments `1–13` (`1–12` = ambient ring, `13` = main center)
  - `govee_h60a6_local.send_scene` — Trigger any numeric Govee scene ID (`33 05 04 <lo> <hi>`)
  - `govee_h60a6_local.send_music_mode` — Trigger onboard music reactive modes (`33 05 13`)
  - `govee_h60a6_local.set_razer_segments` — Stream multi-color segment arrays over local UDP Razer/DreamView protocol (`0xB1`/`0xB0`)
  - `govee_h60a6_local.send_raw_ptreal` — Send raw hex frames (auto-padded to 20 bytes with XOR checksum for short frames)
- **Diagnostic Entities**
  - `sensor.<name>_active_transport` (`wifi` / `wifi_send_only` / `ble` / `offline`)
  - `button.<name>_refresh_status` and `button.<name>_stop_effect`

---

## Prerequisites

1. **Enable LAN Control in the Govee Home App** (one-time setup):
   - Open the **Govee Home** app -> select your **H60A6** -> **Settings (gear icon)** -> toggle **LAN Control** **ON**.
   - If the toggle doesn't appear immediately, power-cycle the ceiling light at the wall switch and wait 2–3 minutes.
2. **Bluetooth LE (Recommended for Full Dual-Zone & Scene Support)**:
   - Make sure Home Assistant can reach your H60A6 over Bluetooth (local adapter or ESPHome Bluetooth Proxy) and enter the H60A6's Bluetooth MAC address during setup, because H60A6 ignores `ptReal` frames sent over Wi-Fi UDP `4003`.
3. **Same Subnet / UDP Ports**:
   - Home Assistant and your H60A6 should be able to exchange UDP packets on ports **4001** (discovery), **4002** (status replies to HA), and **4003** (commands to the light).
   - If another integration already binds UDP port `4002`, this integration still sends all Wi-Fi commands in `wifi_send_only` mode; free UDP port `4002` if you also want live `devStatus` feedback.

---

## Installation in Home Assistant

### Option A: Manual Copy (Fastest)
1. Copy the contents of `custom_components/govee_h60a6_local` into your Home Assistant `config/custom_components/govee_h60a6_local/` directory (use trailing `/.` when updating an existing folder so you don't create a nested subdirectory):
   ```bash
   mkdir -p /path/to/homeassistant/config/custom_components/govee_h60a6_local
   cp -r custom_components/govee_h60a6_local/. /path/to/homeassistant/config/custom_components/govee_h60a6_local/
   ```
2. Restart Home Assistant.
3. Go to **Settings -> Devices & Services -> Add Integration** and search for **Govee H60A6 Local**.
4. Choose **Auto-discover Govee lights on local network** or **Manual IP / Bluetooth setup** and enter your light's local IP address.
5. *(Recommended)* Enter the H60A6's Bluetooth MAC address for Ambient Ring, Main Center, ring segments, native scenes, and automatic BLE fallback.

### Option B: HACS (Custom Repository)
Push this repository to GitHub, add it as a Custom Repository in HACS (category: **Integration**), install **Govee H60A6 Local**, and restart Home Assistant.

---

## Standalone CLI / Quick Test Tool

You can test your H60A6 directly from a terminal before adding it to Home Assistant:

```bash
# Discover Govee lights on your LAN
python3 tools/govee_cli.py discover

# Query live status
python3 tools/govee_cli.py --host <DEVICE_IP> status

# Turn ON and set vibrant cyan RGB color
python3 tools/govee_cli.py --host <DEVICE_IP> on
python3 tools/govee_cli.py --host <DEVICE_IP> color "#00E5FF"
python3 tools/govee_cli.py --host <DEVICE_IP> brightness 80

# Switch to warm white (2700K)
python3 tools/govee_cli.py --host <DEVICE_IP> kelvin 2700

# Scan for Bluetooth MAC address
python3 tools/govee_cli.py ble-scan

# Color ambient ring segments 1-6 purple and 7-12 gold via BLE
python3 tools/govee_cli.py --ble <BLE_MAC> --mode ble_only segment 1,2,3,4,5,6 "#9900FF" --brightness 100
python3 tools/govee_cli.py --ble <BLE_MAC> --mode ble_only segment 7,8,9,10,11,12 "#FFAA00" --brightness 100

# Run with Wi-Fi preferred + BLE fallback
python3 tools/govee_cli.py --host <DEVICE_IP> --ble <BLE_MAC> color "#FF3366"
```

---

## Official Govee H60A6 Scenes (Offline Bundle)

[scenes_h60a6.json](custom_components/govee_h60a6_local/scenes_h60a6.json) is already bundled with Govee's H60A6 scene catalog (no cloud access or API key needed at runtime). **75 animated hardware scenes** (`Scene: Sunrise`, `Scene: Ocean`, `Scene: Rainbow`, `Scene: Forest`, `Scene: Aurora`, etc.) are exposed in Home Assistant's **Effect** dropdown alongside the 8 local software effects (the 11 parameter-less static white-Kelvin presets from the mobile app, such as `Illumination`, are omitted since Kelvin and brightness are controlled directly).

Hardware scenes are uploaded locally over encrypted BLE using:
- **`0xA3`** 20-byte chunk frames (`a3 00 01 <lines> 58 ...`, `a3 ff ...`) for compact scene payloads (15 scenes), or
- **`0xA4`** variable-length single/multi-chunk frames (`a4 00 00 01 <total> 00 58 20 ...` ending with `a4 ff ff ... <xor>`) for multi-layer `0x20` scenes (60 scenes),
followed by `33 05 04 <scene_lo> <scene_hi>`.

To refresh the bundled `scenes_h60a6.json` catalog from Govee's public SKU library (`app2.govee.com`), run:

```bash
python3 tools/fetch_h60a6_scenes.py
```

List or trigger bundled scenes from the CLI:

```bash
python3 tools/govee_cli.py scenes
python3 tools/govee_cli.py --ble <BLE_MAC> --mode ble_only scene "Ocean"
```

---

## Example Home Assistant Service Calls

```yaml
# Set half the ambient ring to deep blue (segments 1..12 = ring, 13 = main center)
action: govee_h60a6_local.set_segment_color
data:
  segments: [1, 2, 3, 4, 5, 6]
  rgb_color: [0, 80, 255]
  brightness_pct: 90

# Stream a 3-color gradient over local Razer/DreamView UDP
action: govee_h60a6_local.set_razer_segments
data:
  colors:
    - [255, 0, 128]
    - [128, 0, 255]
    - [0, 200, 255]
  gradient: true

# Send a custom 20-byte ptReal packet (XOR checksum calculated automatically)
action: govee_h60a6_local.send_raw_ptreal
data:
  commands:
    - "33 05 15 01 ff 00 80 00 00 00 00 00 ff 0f"
```

---

## Running Tests

```bash
python3 -m py_compile custom_components/govee_h60a6_local/*.py tools/*.py tests/*.py
python3 -m unittest discover -s tests -v
```
