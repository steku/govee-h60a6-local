"""Config flow and options flow for Govee H60A6 Local."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_BLE_ADDRESS,
    CONF_BLE_VERSION,
    CONF_COMMAND_DELAY_MS,
    CONF_DEVICE_ID,
    CONF_ENABLE_SEGMENTS,
    CONF_HOST,
    CONF_LISTEN_PORT,
    CONF_POLL_INTERVAL,
    CONF_SKU,
    CONF_TRANSPORT_MODE,
    CONF_WIFI_VERSION,
    DEFAULT_COMMAND_DELAY_MS,
    DEFAULT_NAME,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    GOVEE_LISTEN_PORT,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MODEL_SKU,
    TRANSPORT_BLE_ONLY,
    TRANSPORT_MODES,
    TRANSPORT_WIFI_ONLY,
    TRANSPORT_WIFI_PREFERRED,
)
from .protocol import DiscoveredDevice
from .transport import GoveeLanHub

_LOGGER = logging.getLogger(__name__)

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}$")


def _normalize_mac(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.strip().replace("-", ":").upper()
    return cleaned


def _get_discovered_ble_govee(hass: Any) -> dict[str, str]:
    """Return nearby Govee BLE devices known to Home Assistant Bluetooth, if loaded."""
    devices: dict[str, str] = {}
    try:
        from homeassistant.components import bluetooth

        for info in bluetooth.async_discovered_service_info(hass, connectable=True):
            name = info.name or ""
            if "govee" in name.lower() or "ihoment" in name.lower() or "h60a6" in name.lower():
                devices[info.address.upper()] = f"{name} ({info.address.upper()})"
    except Exception:
        pass
    return devices


def _get_existing_hub(hass: Any, listen_port: int) -> GoveeLanHub | None:
    """Return an already-running GoveeLanHub for `listen_port` if one exists."""
    try:
        hubs = hass.data.get(DOMAIN, {}).get("hubs", {})
        hub = hubs.get(listen_port)
        if isinstance(hub, GoveeLanHub) and hub.is_running:
            return hub
    except Exception:
        pass
    return None


async def _probe_lan_device(
    hass: Any, host: str, listen_port: int = GOVEE_LISTEN_PORT
) -> tuple[bool, DiscoveredDevice | None]:
    """Validate that `host` responds to Govee LAN `devStatus` and optional `scan`.

    Reuses an existing running hub if present (so adding multiple H60A6 lights
    never collides on UDP port 4002). If port 4002 is occupied by another
    integration, allows setup to proceed in Wi-Fi send-only mode.
    """
    existing = _get_existing_hub(hass, listen_port)
    hub = existing or GoveeLanHub(listen_port=listen_port)
    owns_hub = existing is None
    try:
        if owns_hub:
            await hub.async_start(allow_ephemeral_fallback=True)
        if not hub.receive_enabled:
            # Port 4002 is held by another integration; Wi-Fi send-only still works
            return True, None
        try:
            await hub.async_query_status(host, timeout=2.5)
        except Exception as err:
            _LOGGER.debug("devStatus probe to %s failed: %s", host, err)
            return False, None

        discovered_list = await hub.async_discover(timeout=1.2, unicast_host=host)
        matched = next((d for d in discovered_list if d.ip == host), None)
        return True, matched
    except OSError as err:
        _LOGGER.warning("Unable to bind Govee UDP port %s: %s", listen_port, err)
        return False, None
    finally:
        if owns_hub:
            await hub.async_stop(force=True)


class GoveeH60A6ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the UI setup flow for Govee H60A6 Local."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_devices: dict[str, DiscoveredDevice] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user choose between manual setup (pre-filled IP) or LAN scan."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["manual", "discover"],
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Auto-discover Govee LAN devices on the local subnet."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_ip = user_input[CONF_HOST]
            dev = self._discovered_devices.get(selected_ip)
            return await self._create_or_validate_entry(
                name=user_input.get(CONF_NAME) or f"Govee {dev.sku if dev and dev.sku else MODEL_SKU}",
                host=selected_ip,
                ble_address=_normalize_mac(user_input.get(CONF_BLE_ADDRESS)),
                transport_mode=user_input.get(CONF_TRANSPORT_MODE, TRANSPORT_WIFI_PREFERRED),
                enable_segments=bool(user_input.get(CONF_ENABLE_SEGMENTS, False)),
                discovered=dev,
                skip_probe=True,
            )

        existing = _get_existing_hub(self.hass, GOVEE_LISTEN_PORT)
        hub = existing or GoveeLanHub()
        owns_hub = existing is None
        try:
            if owns_hub:
                await hub.async_start(allow_ephemeral_fallback=False)
            if not hub.receive_enabled:
                devices = []
                errors["base"] = "port_in_use"
            else:
                devices = await hub.async_discover(timeout=3.0)
        except OSError as err:
            _LOGGER.warning("Discovery socket failed: %s", err)
            devices = []
            errors["base"] = "port_in_use"
        finally:
            if owns_hub:
                await hub.async_stop(force=True)

        if not devices:
            return await self.async_step_manual(
                user_input=None,
                initial_errors=errors or {"base": "no_devices_found"},
            )

        self._discovered_devices = {d.ip: d for d in devices}
        device_options = {
            d.ip: f"{d.sku or 'Govee'} — {d.ip} ({d.device_id or 'LAN'})"
            for d in devices
        }

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): vol.In(device_options),
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Optional(CONF_BLE_ADDRESS, default=""): str,
                vol.Required(
                    CONF_TRANSPORT_MODE, default=TRANSPORT_WIFI_PREFERRED
                ): vol.In(TRANSPORT_MODES),
                vol.Optional(CONF_ENABLE_SEGMENTS, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="discover",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_manual(
        self,
        user_input: dict[str, Any] | None = None,
        initial_errors: dict[str, str] | None = None,
    ) -> FlowResult:
        """Manual setup form for Wi-Fi IP and optional Bluetooth MAC."""
        errors: dict[str, str] = dict(initial_errors or {})

        if user_input is not None:
            host = (user_input.get(CONF_HOST) or "").strip()
            ble_address = _normalize_mac(user_input.get(CONF_BLE_ADDRESS))
            transport_mode = user_input.get(CONF_TRANSPORT_MODE, TRANSPORT_WIFI_PREFERRED)
            name = (user_input.get(CONF_NAME) or DEFAULT_NAME).strip()
            enable_segments = bool(user_input.get(CONF_ENABLE_SEGMENTS, False))
            listen_port = int(user_input.get(CONF_LISTEN_PORT, GOVEE_LISTEN_PORT))

            if ble_address and not MAC_RE.match(ble_address):
                errors[CONF_BLE_ADDRESS] = "invalid_mac"
            elif transport_mode == TRANSPORT_BLE_ONLY and not ble_address:
                errors[CONF_BLE_ADDRESS] = "ble_required"
            elif transport_mode in (TRANSPORT_WIFI_PREFERRED, TRANSPORT_WIFI_ONLY) and not host:
                errors[CONF_HOST] = "host_required"
            else:
                result = await self._create_or_validate_entry(
                    name=name,
                    host=host,
                    ble_address=ble_address,
                    transport_mode=transport_mode,
                    enable_segments=enable_segments,
                    listen_port=listen_port,
                    skip_probe=bool(user_input.get("skip_validation", False)),
                )
                if result is not None:
                    return result
                errors["base"] = "cannot_connect"

        ble_suggestions = _get_discovered_ble_govee(self.hass)
        default_ble = next(iter(ble_suggestions.keys()), "")

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Optional(CONF_HOST, default=""): str,
                vol.Optional(CONF_BLE_ADDRESS, default=default_ble): str,
                vol.Required(
                    CONF_TRANSPORT_MODE, default=TRANSPORT_WIFI_PREFERRED
                ): vol.In(TRANSPORT_MODES),
                vol.Optional(CONF_ENABLE_SEGMENTS, default=False): bool,
                vol.Optional(CONF_LISTEN_PORT, default=GOVEE_LISTEN_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1024, max=65535)
                ),
                vol.Optional("skip_validation", default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="manual",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "ble_hint": ", ".join(ble_suggestions.values()) or "None detected",
            },
        )

    async def _create_or_validate_entry(
        self,
        *,
        name: str,
        host: str,
        ble_address: str,
        transport_mode: str,
        enable_segments: bool,
        listen_port: int = GOVEE_LISTEN_PORT,
        discovered: DiscoveredDevice | None = None,
        skip_probe: bool = False,
    ) -> FlowResult | None:
        if transport_mode != TRANSPORT_BLE_ONLY and host and not skip_probe:
            ok, probed = await _probe_lan_device(
                self.hass, host, listen_port=listen_port
            )
            if not ok and not (transport_mode == TRANSPORT_WIFI_PREFERRED and ble_address):
                return None
            if probed is not None:
                discovered = probed

        unique_id = (
            (discovered.device_id if discovered and discovered.device_id else None)
            or (host.lower() if host else None)
            or ble_address.lower()
        )
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        data: dict[str, Any] = {
            CONF_NAME: name,
            CONF_HOST: host,
            CONF_BLE_ADDRESS: ble_address,
            CONF_TRANSPORT_MODE: transport_mode,
            CONF_ENABLE_SEGMENTS: enable_segments,
            CONF_LISTEN_PORT: listen_port,
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
            CONF_COMMAND_DELAY_MS: DEFAULT_COMMAND_DELAY_MS,
            CONF_SKU: (discovered.sku if discovered and discovered.sku else MODEL_SKU),
            CONF_DEVICE_ID: (discovered.device_id if discovered else ""),
            CONF_WIFI_VERSION: (discovered.wifi_version_soft if discovered else ""),
            CONF_BLE_VERSION: (discovered.ble_version_soft if discovered else ""),
        }
        return self.async_create_entry(title=name, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return GoveeH60A6OptionsFlow()


class GoveeH60A6OptionsFlow(config_entries.OptionsFlow):
    """Allow changing IP, BLE MAC, transport preference, polling, and segment entities."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            ble_address = _normalize_mac(user_input.get(CONF_BLE_ADDRESS))
            if ble_address and not MAC_RE.match(ble_address):
                errors[CONF_BLE_ADDRESS] = "invalid_mac"
            else:
                cleaned = {
                    **user_input,
                    CONF_HOST: (user_input.get(CONF_HOST) or "").strip(),
                    CONF_BLE_ADDRESS: ble_address,
                }
                return self.async_create_entry(title="", data=cleaned)

        schema = vol.Schema(
            {
                vol.Optional(CONF_HOST, default=current.get(CONF_HOST, "")): str,
                vol.Optional(
                    CONF_BLE_ADDRESS, default=current.get(CONF_BLE_ADDRESS, "")
                ): str,
                vol.Required(
                    CONF_TRANSPORT_MODE,
                    default=current.get(CONF_TRANSPORT_MODE, TRANSPORT_WIFI_PREFERRED),
                ): vol.In(TRANSPORT_MODES),
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=int(current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                ),
                vol.Required(
                    CONF_COMMAND_DELAY_MS,
                    default=int(
                        current.get(CONF_COMMAND_DELAY_MS, DEFAULT_COMMAND_DELAY_MS)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=1000)),
                vol.Optional(
                    CONF_ENABLE_SEGMENTS,
                    default=bool(current.get(CONF_ENABLE_SEGMENTS, False)),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
