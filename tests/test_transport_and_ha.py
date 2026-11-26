"""Integration tests for UDP LAN hub, BLE fallback, coordinator, and HA entities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib.util
import json
from pathlib import Path
import socket
import sys
import types
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parent.parent
COMP_DIR = ROOT / "custom_components" / "govee_h60a6_local"


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockGoveeDeviceProtocol(asyncio.DatagramProtocol):
    """Emulates a real Govee H60A6 responding to UDP LAN commands on localhost."""

    def __init__(self, reply_port: int) -> None:
        self.reply_port = reply_port
        self.transport: asyncio.DatagramTransport | None = None
        self.on_off = 0
        self.brightness = 80
        self.color = {"r": 255, "g": 255, "b": 255}
        self.kelvin = 4000
        self.received_commands: list[dict[str, Any]] = []
        self.drop_packets = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.drop_packets or self.transport is None:
            return
        msg = json.loads(data.decode("utf-8"))["msg"]
        self.received_commands.append(msg)
        cmd = msg["cmd"]
        payload = msg.get("data", {})

        if cmd == "turn":
            self.on_off = int(payload["value"])
        elif cmd == "brightness":
            self.brightness = int(payload["value"])
        elif cmd == "colorwc":
            self.color = dict(payload["color"])
            self.kelvin = int(payload.get("colorTemInKelvin", 0))
        elif cmd == "devStatus":
            reply = json.dumps(
                {
                    "msg": {
                        "cmd": "devStatus",
                        "data": {
                            "onOff": self.on_off,
                            "brightness": self.brightness,
                            "color": self.color,
                            "colorTemInKelvin": self.kelvin,
                        },
                    }
                }
            ).encode("utf-8")
            self.transport.sendto(reply, (addr[0], self.reply_port))
        elif cmd == "scan":
            reply = json.dumps(
                {
                    "msg": {
                        "cmd": "scan",
                        "data": {
                            "ip": "127.0.0.1",
                            "device": "AA:BB:CC:DD:EE:FF:00:11",
                            "sku": "H60A6",
                            "bleVersionHard": "3.01.01",
                            "bleVersionSoft": "1.03.01",
                            "wifiVersionHard": "1.00.10",
                            "wifiVersionSoft": "1.00.45",
                        },
                    }
                }
            ).encode("utf-8")
            self.transport.sendto(reply, (addr[0], self.reply_port))


class FakeBleClient:
    """In-memory fake BleakClient recording 20-byte writes."""

    def __init__(self) -> None:
        self.is_connected = False
        self.written: list[tuple[str, bytes]] = []

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(self, _uuid: str, _cb: Any) -> None:
        return None

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        self.written.append((uuid, bytes(data)))


def _install_ha_stubs() -> None:
    """Install lightweight Home Assistant stubs so HA entity modules can be tested directly."""
    if "homeassistant" in sys.modules:
        return

    def _make_mod(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    ha = _make_mod("homeassistant")
    _make_mod("homeassistant.config_entries")
    _make_mod("homeassistant.const")
    _make_mod("homeassistant.core")
    _make_mod("homeassistant.exceptions")
    _make_mod("homeassistant.helpers")
    _make_mod("homeassistant.helpers.config_validation")
    _make_mod("homeassistant.helpers.device_registry")
    _make_mod("homeassistant.helpers.entity_platform")
    _make_mod("homeassistant.helpers.update_coordinator")
    _make_mod("homeassistant.components")
    _make_mod("homeassistant.components.light")
    _make_mod("homeassistant.components.sensor")
    _make_mod("homeassistant.components.button")
    _make_mod("homeassistant.data_entry_flow")

    # Minimal voluptuous stub if not installed
    if "voluptuous" not in sys.modules:
        vol = _make_mod("voluptuous")
        vol.Schema = lambda x: x
        vol.Optional = lambda k, default=None: k
        vol.Required = lambda k, default=None: k
        vol.All = lambda *a: a
        vol.Coerce = lambda t: t
        vol.Range = lambda **kw: kw
        vol.In = lambda x: x
        vol.ExactSequence = lambda x: x

    # homeassistant.const
    const_mod = sys.modules["homeassistant.const"]
    const_mod.CONF_NAME = "name"

    class Platform:
        LIGHT = "light"
        SENSOR = "sensor"
        BUTTON = "button"

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"

    const_mod.Platform = Platform
    const_mod.EntityCategory = EntityCategory

    # homeassistant.config_entries
    ce_mod = sys.modules["homeassistant.config_entries"]

    @dataclass
    class ConfigEntry:
        entry_id: str
        data: dict[str, Any]
        options: dict[str, Any] = field(default_factory=dict)
        unique_id: str | None = None

    ce_mod.ConfigEntry = ConfigEntry
    ce_mod.ConfigFlow = type("ConfigFlow", (), {})
    ce_mod.OptionsFlow = type("OptionsFlow", (), {})

    # homeassistant.core
    core_mod = sys.modules["homeassistant.core"]

    class HomeAssistant:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {}

        def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
            return asyncio.create_task(coro)

    core_mod.HomeAssistant = HomeAssistant
    core_mod.ServiceCall = type("ServiceCall", (), {})
    core_mod.callback = lambda fn: fn

    # homeassistant.exceptions
    exc_mod = sys.modules["homeassistant.exceptions"]
    exc_mod.HomeAssistantError = type("HomeAssistantError", (Exception,), {})

    # homeassistant.helpers
    cv_mod = sys.modules["homeassistant.helpers.config_validation"]
    cv_mod.string = str
    cv_mod.boolean = bool
    cv_mod.ensure_list = lambda x: x if isinstance(x, list) else [x]

    dr_mod = sys.modules["homeassistant.helpers.device_registry"]
    dr_mod.DeviceInfo = dict

    ep_mod = sys.modules["homeassistant.helpers.entity_platform"]
    ep_mod.AddEntitiesCallback = Any

    uc_mod = sys.modules["homeassistant.helpers.update_coordinator"]
    uc_mod.UpdateFailed = type("UpdateFailed", (Exception,), {})

    class DataUpdateCoordinator:
        def __class_getitem__(cls, _item: Any) -> Any:
            return cls

        def __init__(self, hass: Any, logger: Any, *, name: str, update_interval: Any) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data: Any = None

        async def async_request_refresh(self) -> None:
            self.data = await self._async_update_data()

        def async_set_updated_data(self, data: Any) -> None:
            self.data = data

    class CoordinatorEntity:
        def __class_getitem__(cls, _item: Any) -> Any:
            return cls

        def __init__(self, coordinator: Any) -> None:
            self.coordinator = coordinator

    uc_mod.DataUpdateCoordinator = DataUpdateCoordinator
    uc_mod.CoordinatorEntity = CoordinatorEntity

    # light, sensor, button
    light_mod = sys.modules["homeassistant.components.light"]
    light_mod.ATTR_BRIGHTNESS = "brightness"
    light_mod.ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"
    light_mod.ATTR_EFFECT = "effect"
    light_mod.ATTR_RGB_COLOR = "rgb_color"
    light_mod.ATTR_TRANSITION = "transition"

    class ColorMode:
        RGB = "rgb"
        COLOR_TEMP = "color_temp"

    class LightEntityFeature:
        EFFECT = 4
        TRANSITION = 32

    light_mod.ColorMode = ColorMode
    light_mod.LightEntityFeature = LightEntityFeature
    light_mod.LightEntity = type("LightEntity", (), {})

    sys.modules["homeassistant.components.sensor"].SensorEntity = type("SensorEntity", (), {})
    sys.modules["homeassistant.components.button"].ButtonEntity = type("ButtonEntity", (), {})
    sys.modules["homeassistant.data_entry_flow"].FlowResult = dict
    ha.config_entries = ce_mod


class TestTransportAndEntities(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _install_ha_stubs()
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        self.listen_port = _free_udp_port()
        self.cmd_port = _free_udp_port()
        self.disc_port = _free_udp_port()

        loop = asyncio.get_running_loop()
        self.mock_device = MockGoveeDeviceProtocol(reply_port=self.listen_port)
        self.dev_transport, _ = await loop.create_datagram_endpoint(
            lambda: self.mock_device,
            local_addr=("127.0.0.1", self.cmd_port),
        )
        self.disc_transport, _ = await loop.create_datagram_endpoint(
            lambda: self.mock_device,
            local_addr=("127.0.0.1", self.disc_port),
        )

        from custom_components.govee_h60a6_local.transport import GoveeLanHub

        self.hub = GoveeLanHub(
            listen_port=self.listen_port,
            command_port=self.cmd_port,
            discovery_port=self.disc_port,
            bind_host="127.0.0.1",
        )
        await self.hub.async_start()

    async def asyncTearDown(self) -> None:
        await self.hub.async_stop(force=True)
        self.dev_transport.close()
        self.disc_transport.close()

    async def test_wifi_preferred_full_lifecycle_and_ble_fallback(self) -> None:
        from custom_components.govee_h60a6_local.const import TRANSPORT_WIFI_PREFERRED
        from custom_components.govee_h60a6_local.transport import (
            GoveeBleTransport,
            GoveeH60A6Client,
        )

        fake_ble = FakeBleClient()
        ble_transport = GoveeBleTransport(
            "AA:BB:CC:DD:EE:FF",
            idle_timeout=0,
            client_factory=lambda: fake_ble,
        )
        client = GoveeH60A6Client(
            host="127.0.0.1",
            hub=self.hub,
            ble_transport=ble_transport,
            transport_mode=TRANSPORT_WIFI_PREFERRED,
            command_delay_ms=0,
            lan_timeout=0.3,
        )

        # 1. Discovery over localhost unicast
        found = await self.hub.async_discover(timeout=0.2, unicast_host="127.0.0.1")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].sku, "H60A6")

        # 2. Commands go over Wi-Fi LAN while healthy
        self.assertEqual(await client.async_turn(True), "wifi")
        self.assertEqual(await client.async_set_rgb(10, 200, 255), "wifi")
        self.assertEqual(await client.async_set_brightness(65), "wifi")
        await asyncio.sleep(0.05)

        status = await client.async_get_status()
        assert status is not None
        self.assertTrue(status.is_on)
        self.assertEqual(status.brightness, 65)
        self.assertEqual(status.rgb, (10, 200, 255))
        self.assertEqual(status.color_temp_kelvin, 0)

        # 3. Color temperature update via colorwc (Wi-Fi)
        self.assertEqual(await client.async_set_color_temp(2700), "wifi")
        await asyncio.sleep(0.05)
        status = await client.async_get_status()
        assert status is not None
        self.assertEqual(status.color_temp_kelvin, 2700)

        # 3b. Segment/ring & scene packets prefer BLE automatically when BLE is configured
        # because H60A6 ignores ptReal over Wi-Fi UDP 4003.
        self.assertEqual(
            await client.async_set_segment_color(255, 0, 128, 0x0FFF), "ble"
        )
        self.assertGreaterEqual(len(fake_ble.written), 2)
        ble_writes_before_outage = len(fake_ble.written)

        # 4. Simulate Wi-Fi outage -> 2 failed devStatus polls trigger BLE fallback
        self.mock_device.drop_packets = True
        self.assertIsNone(await client.async_get_status())
        self.assertIsNone(await client.async_get_status())
        self.assertEqual(client.active_transport, "ble")

        used = await client.async_set_rgb(255, 50, 0)
        self.assertEqual(used, "ble")
        self.assertEqual(len(fake_ble.written), ble_writes_before_outage + 1)
        self.assertEqual(
            fake_ble.written[-1][1][:6], bytes([0x33, 0x05, 0x02, 255, 50, 0])
        )

        # 5. Wi-Fi recovers -> devStatus succeeds and switches back to Wi-Fi!
        self.mock_device.drop_packets = False
        status = await client.async_get_status()
        assert status is not None
        self.assertEqual(client.active_transport, "wifi")
        self.assertEqual(await client.async_turn(False), "wifi")

        await client.async_close()

    async def test_coordinator_and_entities_end_to_end(self) -> None:
        from homeassistant.components.light import (
            ATTR_BRIGHTNESS,
            ATTR_COLOR_TEMP_KELVIN,
            ATTR_EFFECT,
            ATTR_RGB_COLOR,
            ColorMode,
        )
        from homeassistant.config_entries import ConfigEntry
        from homeassistant.core import HomeAssistant

        from custom_components.govee_h60a6_local.button import (
            GoveeH60A6RefreshButton,
            GoveeH60A6StopEffectButton,
        )
        from custom_components.govee_h60a6_local.const import (
            CONF_HOST,
            CONF_TRANSPORT_MODE,
            TRANSPORT_WIFI_ONLY,
        )
        from custom_components.govee_h60a6_local.coordinator import GoveeH60A6Coordinator
        from custom_components.govee_h60a6_local.light import (
            GoveeH60A6MainLight,
            GoveeH60A6RingLight,
            GoveeH60A6SegmentLight,
        )
        from custom_components.govee_h60a6_local.sensor import GoveeH60A6TransportSensor
        from custom_components.govee_h60a6_local.transport import GoveeH60A6Client

        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="test_h60a6",
            data={CONF_HOST: "127.0.0.1", CONF_TRANSPORT_MODE: TRANSPORT_WIFI_ONLY},
            unique_id="h60a6_unique",
        )
        client = GoveeH60A6Client(
            host="127.0.0.1",
            hub=self.hub,
            transport_mode=TRANSPORT_WIFI_ONLY,
            command_delay_ms=0,
            lan_timeout=0.5,
        )
        coord = GoveeH60A6Coordinator(hass, entry, client, poll_interval=60)
        await coord.async_request_refresh()

        main_light = GoveeH60A6MainLight(coord)
        ring_light = GoveeH60A6RingLight(coord)
        seg3_light = GoveeH60A6SegmentLight(coord, 3)
        transport_sensor = GoveeH60A6TransportSensor(coord)
        refresh_btn = GoveeH60A6RefreshButton(coord)
        stop_btn = GoveeH60A6StopEffectButton(coord)

        self.assertEqual(transport_sensor.native_value, "wifi")
        self.assertFalse(main_light.is_on)

        # Turn on main light with RGB + brightness
        await main_light.async_turn_on(
            **{ATTR_RGB_COLOR: (255, 0, 128), ATTR_BRIGHTNESS: 204}
        )
        self.assertTrue(main_light.is_on)
        self.assertEqual(main_light.color_mode, ColorMode.RGB)
        self.assertEqual(main_light.rgb_color, (255, 0, 128))
        self.assertEqual(coord.state.brightness, 80)

        # Switch to Kelvin color temp
        await main_light.async_turn_on(**{ATTR_COLOR_TEMP_KELVIN: 3200})
        self.assertEqual(main_light.color_mode, ColorMode.COLOR_TEMP)
        self.assertEqual(main_light.color_temp_kelvin, 3200)
        self.assertIsNone(main_light.rgb_color)

        # Control ambient ring & individual segment 3 via ptReal
        await ring_light.async_turn_on(**{ATTR_RGB_COLOR: (0, 255, 200), ATTR_BRIGHTNESS: 255})
        self.assertTrue(ring_light.is_on)
        self.assertEqual(ring_light.rgb_color, (0, 255, 200))

        await seg3_light.async_turn_on(**{ATTR_RGB_COLOR: (255, 180, 0), ATTR_BRIGHTNESS: 128})
        self.assertEqual(seg3_light.rgb_color, (255, 180, 0))
        await asyncio.sleep(0.05)
        ptreal_cmds = [m for m in self.mock_device.received_commands if m["cmd"] == "ptReal"]
        self.assertGreaterEqual(len(ptreal_cmds), 2)

        # Start native hardware scene effect, then stop via button
        await main_light.async_turn_on(**{ATTR_EFFECT: "Scene: Aurora"})
        self.assertEqual(main_light.effect, "Scene: Aurora")
        await stop_btn.async_press()
        self.assertIsNone(main_light.effect)

        # Trigger Razer streaming service helper
        await coord.async_set_razer_segments([(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        await asyncio.sleep(0.05)
        razer_cmds = [m for m in self.mock_device.received_commands if m["cmd"] == "razer"]
        self.assertGreaterEqual(len(razer_cmds), 2)

        # Refresh button syncs cleanly from emulated device
        await refresh_btn.async_press()
        await coord.async_shutdown_tasks()

    async def test_port_in_use_falls_back_to_wifi_send_only(self) -> None:
        """Reproduce EADDRINUSE on UDP 4002 and verify send-only Wi-Fi still works."""
        from custom_components.govee_h60a6_local.const import TRANSPORT_WIFI_PREFERRED
        from custom_components.govee_h60a6_local.transport import (
            GoveeH60A6Client,
            GoveeLanHub,
        )

        # Occupy a port exclusively (without SO_REUSEPORT), matching another HA integration
        busy_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        busy_sock.bind(("127.0.0.1", 0))
        busy_port = busy_sock.getsockname()[1]

        hub = GoveeLanHub(
            listen_port=busy_port,
            command_port=self.cmd_port,
            discovery_port=self.disc_port,
            bind_host="127.0.0.1",
        )
        try:
            await hub.async_start()
            self.assertTrue(hub.is_running)
            self.assertFalse(hub.receive_enabled)
            self.assertNotEqual(hub.bound_port, busy_port)

            client = GoveeH60A6Client(
                host="127.0.0.1",
                hub=hub,
                transport_mode=TRANSPORT_WIFI_PREFERRED,
                command_delay_ms=0,
            )
            # Status poll stays healthy in send-only mode without failing
            self.assertIsNone(await client.async_get_status())
            self.assertEqual(client.active_transport, "wifi_send_only")

            # Outbound Wi-Fi control commands to port 4003 still reach the light!
            await client.async_turn(True)
            await client.async_set_rgb(0, 128, 255)
            await asyncio.sleep(0.05)

            self.assertEqual(self.mock_device.on_off, 1)
            self.assertEqual(self.mock_device.color, {"r": 0, "g": 128, "b": 255})
            self.assertEqual(client.active_transport, "wifi_send_only")
        finally:
            await hub.async_stop(force=True)
            busy_sock.close()


if __name__ == "__main__":
    unittest.main()
