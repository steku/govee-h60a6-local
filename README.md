# Govee H60A6 Local — Home Assistant Integration

100% local Home Assistant custom integration for the **Govee 15-Inch RGBICWW Smart Ceiling Light Pro (`H60A6`)**, with **Wi-Fi LAN preferred** (UDP ports `4001`–`4003`) and **optional Bluetooth LE fallback** via Home Assistant's native Bluetooth stack.

No Govee Cloud account, API key, or internet access is required.

---

## Features

- **Wi-Fi LAN Preferred + Automatic BLE Hybrid Routing**
  - Uses Govee's local LAN UDP API (`devStatus`, `turn`, `brightness`, `colorwc`, `razer`) for instant main-light control and real state feedback over Wi-Fi.
  - Because H60A6 firmware ignores `ptReal` segment/ring commands over Wi-Fi UDP `4003`, pairing the light's **Bluetooth MAC address** (via Home Assistant's local Bluetooth adapter or an ESPHome Bluetooth proxy) automatically routes **Ambient Ring**, **12 ring segments**, and **native scenes/music modes** locally over BLE while keeping main-light controls on Wi-Fi (and falling back to BLE if Wi-Fi drops).
  - Built-in command pacing (`80ms` default) and gentle polling (`15s` default) to prevent firmware freezes reported on newer H60A6 firmware revisions.
- **Full Light Control (`light.<name>`)**
  - On / Off
  - Brightness (`1%–100%`)
  - Full RGB color wheel (`colorwc` over Wi-Fi / `33 05 02` over BLE)
  - Tunable Warm-to-Cool White (`2200K–6500K`) using modern Home Assistant Kelvin APIs
  - Smooth software transitions (`transition` parameter)
  - Built-in local dynamic effects (*Rainbow Flow*, *Aurora Pulse*, *Sunset Drift*, *Ocean Wave*, *Candle Flicker*, *Forest Breeze*, *Cyber Neon*, *Warm Breathing*) + native `ptReal` hardware scene & music presets
- **Dual-Zone / Ambient Ring & Per-Segment Control**
  - Dedicated **Ambient Ring** light entity (`light.<name>_ambient_ring`) controlling the 12-segment halo backlight via `ptReal` (`33 05 15 01` / `33 05 15 02`).
  - Optional toggle in integration options to expose all **12 individual ring segments** (`Ring Segment 1`–`12`) as separate Home Assistant light entities.
- **Advanced Services**
  - `govee_h60a6_local.set_segment_color` — Color any combination of segments `1–12`
  - `govee_h60a6_local.send_scene` — Trigger any numeric Govee scene ID (`33 05 04 <id>`)
  - `govee_h60a6_local.send_music_mode` — Trigger onboard music reactive modes (`33 05 13`)
  - `govee_h60a6_local.set_razer_segments` — Stream multi-color segment arrays over local UDP Razer/DreamView protocol (`0xB1`/`0xB0`)
  - `govee_h60a6_local.send_raw_ptreal` — Send any raw hex frame (auto-padded to 20 bytes with XOR checksum)
- **Diagnostic Entities**
  - `sensor.<name>_active_transport` (`wifi` / `ble` / `offline`)
  - `button.<name>_refresh_status` and `button.<name>_stop_effect`

---

## Prerequisites

1. **Enable LAN Control in the Govee Home App** (one-time setup):
   - Open the **Govee Home** app -> select your **H60A6** -> **Settings (gear icon)** -> toggle **LAN Control** **ON**.
   - If the toggle doesn't appear immediately, power-cycle the ceiling light at the wall switch and wait 2–3 minutes.
2. **Same Subnet / UDP Ports**:
   - Home Assistant and your H60A6 should be able to exchange UDP packets on ports **4001** (discovery), **4002** (status replies to HA), and **4003** (commands to the light).
   - Ensure no other add-on on the same host (such as `govee2mqtt`) is already holding UDP port `4002`.

---

## Installation in Home Assistant

### Option A: Manual Copy (Fastest)
1. Copy the folder `custom_components/govee_h60a6_local` into your Home Assistant `config/custom_components/` directory:
   ```bash
   cp -r custom_components/govee_h60a6_local /path/to/homeassistant/config/custom_components/
   ```
2. Restart Home Assistant.
3. Go to **Settings -> Devices & Services -> Add Integration** and search for **Govee H60A6 Local**.
4. Choose **Auto-discover Govee lights on local network** or **Manual IP / Bluetooth setup** and enter your light's local IP address.
5. *(Optional)* Enter the H60A6's Bluetooth MAC address for automatic BLE fallback if Wi-Fi ever drops.

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

# Color ambient ring segments 1-6 purple and 7-12 gold
python3 tools/govee_cli.py --host <DEVICE_IP> segment 1,2,3,4,5,6 "#9900FF" --brightness 100
python3 tools/govee_cli.py --host <DEVICE_IP> segment 7,8,9,10,11,12 "#FFAA00" --brightness 100

# Scan for Bluetooth MAC address (optional fallback)
python3 tools/govee_cli.py ble-scan

# Run with Wi-Fi preferred + BLE fallback
python3 tools/govee_cli.py --host <DEVICE_IP> --ble AA:BB:CC:DD:EE:FF color "#FF3366"
```

---

## Example Home Assistant Service Calls

```yaml
# Set half the ambient ring to deep blue
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
    - "33 05 04 04"
```

---

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```
