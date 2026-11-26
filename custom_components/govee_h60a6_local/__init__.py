"""The Govee H60A6 Local Home Assistant integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_BLE_ADDRESS,
    CONF_COMMAND_DELAY_MS,
    CONF_HOST,
    CONF_LISTEN_PORT,
    CONF_POLL_INTERVAL,
    CONF_TRANSPORT_MODE,
    DEFAULT_COMMAND_DELAY_MS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    GOVEE_LISTEN_PORT,
    RING_SEGMENT_COUNT,
    SERVICE_SEND_MUSIC_MODE,
    SERVICE_SEND_RAW_PTREAL,
    SERVICE_SEND_SCENE,
    SERVICE_SET_RAZER_SEGMENTS,
    SERVICE_SET_SEGMENT_COLOR,
    TRANSPORT_WIFI_PREFERRED,
)
from .coordinator import GoveeH60A6Coordinator
from .transport import GoveeBleTransport, GoveeH60A6Client, GoveeLanHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SENSOR, Platform.BUTTON]

HUBS_KEY = "hubs"
COORDINATORS_KEY = "coordinators"
SERVICES_REGISTERED_KEY = "services_registered"

RGB_SCHEMA = vol.All(
    vol.ExactSequence((vol.Coerce(int), vol.Coerce(int), vol.Coerce(int))),
)

SERVICE_SET_SEGMENT_COLOR_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("segments"): vol.All(
            cv.ensure_list,
            [vol.All(vol.Coerce(int), vol.Range(min=1, max=RING_SEGMENT_COUNT))],
        ),
        vol.Required("rgb_color"): RGB_SCHEMA,
        vol.Optional("brightness_pct"): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        ),
    }
)

SERVICE_SEND_SCENE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("scene_id"): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
    }
)

SERVICE_SEND_MUSIC_MODE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("mode_id"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional("sensitivity", default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
    }
)

SERVICE_SEND_RAW_PTREAL_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("commands"): vol.All(cv.ensure_list, [cv.string]),
    }
)

SERVICE_SET_RAZER_SEGMENTS_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("colors"): vol.All(cv.ensure_list, [RGB_SCHEMA]),
        vol.Optional("gradient", default=True): cv.boolean,
    }
)


def _get_domain_store(hass: HomeAssistant) -> dict[str, Any]:
    store = hass.data.setdefault(
        DOMAIN,
        {
            HUBS_KEY: {},
            COORDINATORS_KEY: {},
            SERVICES_REGISTERED_KEY: False,
        },
    )
    return store


def _resolve_coordinators(
    hass: HomeAssistant, entry_id: str | None
) -> list[GoveeH60A6Coordinator]:
    coordinators: dict[str, GoveeH60A6Coordinator] = _get_domain_store(hass)[
        COORDINATORS_KEY
    ]
    if entry_id:
        coord = coordinators.get(entry_id)
        if coord is None:
            raise HomeAssistantError(f"Unknown Govee H60A6 entry_id: {entry_id}")
        return [coord]
    if not coordinators:
        raise HomeAssistantError("No Govee H60A6 Local integrations are configured")
    return list(coordinators.values())


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee H60A6 Local from a config entry."""
    store = _get_domain_store(hass)

    merged = {**entry.data, **entry.options}
    host: str | None = merged.get(CONF_HOST) or None
    ble_address: str | None = merged.get(CONF_BLE_ADDRESS) or None
    transport_mode: str = merged.get(CONF_TRANSPORT_MODE, TRANSPORT_WIFI_PREFERRED)
    poll_interval: int = int(merged.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    command_delay_ms: int = int(
        merged.get(CONF_COMMAND_DELAY_MS, DEFAULT_COMMAND_DELAY_MS)
    )
    listen_port: int = int(merged.get(CONF_LISTEN_PORT, GOVEE_LISTEN_PORT))

    hub: GoveeLanHub | None = None
    if host:
        hubs: dict[int, GoveeLanHub] = store[HUBS_KEY]
        hub = hubs.get(listen_port)
        if hub is None:
            hub = GoveeLanHub(listen_port=listen_port)
            hubs[listen_port] = hub
        await hub.async_start()

    ble_transport = (
        GoveeBleTransport(ble_address, hass=hass) if ble_address else None
    )

    client = GoveeH60A6Client(
        host=host,
        hub=hub,
        ble_transport=ble_transport,
        transport_mode=transport_mode,
        command_delay_ms=command_delay_ms,
    )

    coordinator = GoveeH60A6Coordinator(
        hass=hass,
        entry=entry,
        client=client,
        poll_interval=poll_interval,
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        # Don't block setup if device is briefly offline; entities will recover on next poll
        _LOGGER.warning(
            "Initial status poll for Govee H60A6 (%s) failed: %s; continuing setup",
            host or ble_address,
            err,
        )
        coordinator.async_set_updated_data(coordinator.state)

    store[COORDINATORS_KEY][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    _async_register_services(hass)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry cleanly."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    store = _get_domain_store(hass)
    coordinator: GoveeH60A6Coordinator | None = store[COORDINATORS_KEY].pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_shutdown_tasks()
        if coordinator.client.hub is not None:
            hub = coordinator.client.hub
            await hub.async_stop()
            if not hub.is_running:
                store[HUBS_KEY].pop(hub.listen_port, None)

    if not store[COORDINATORS_KEY] and store[SERVICES_REGISTERED_KEY]:
        for svc in (
            SERVICE_SET_SEGMENT_COLOR,
            SERVICE_SEND_SCENE,
            SERVICE_SEND_MUSIC_MODE,
            SERVICE_SEND_RAW_PTREAL,
            SERVICE_SET_RAZER_SEGMENTS,
        ):
            hass.services.async_remove(DOMAIN, svc)
        store[SERVICES_REGISTERED_KEY] = False

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services once across all loaded H60A6 entries."""
    store = _get_domain_store(hass)
    if store[SERVICES_REGISTERED_KEY]:
        return

    async def handle_set_segment_color(call: ServiceCall) -> None:
        coords = _resolve_coordinators(hass, call.data.get("entry_id"))
        segments = list(call.data["segments"])
        rgb = tuple(call.data["rgb_color"])
        brightness_pct = call.data.get("brightness_pct")
        for coord in coords:
            await coord.async_set_segments(
                segments,
                is_on=(brightness_pct != 0),
                rgb=(rgb[0], rgb[1], rgb[2]),
                brightness_pct=brightness_pct,
            )

    async def handle_send_scene(call: ServiceCall) -> None:
        coords = _resolve_coordinators(hass, call.data.get("entry_id"))
        scene_id = int(call.data["scene_id"])
        for coord in coords:
            await coord.async_send_scene_id(scene_id)

    async def handle_send_music_mode(call: ServiceCall) -> None:
        coords = _resolve_coordinators(hass, call.data.get("entry_id"))
        mode_id = int(call.data["mode_id"])
        sensitivity = int(call.data["sensitivity"])
        for coord in coords:
            await coord.async_send_music_mode(mode_id, sensitivity)

    async def handle_send_raw_ptreal(call: ServiceCall) -> None:
        coords = _resolve_coordinators(hass, call.data.get("entry_id"))
        commands = list(call.data["commands"])
        for coord in coords:
            await coord.async_send_raw_ptreal(commands)

    async def handle_set_razer_segments(call: ServiceCall) -> None:
        coords = _resolve_coordinators(hass, call.data.get("entry_id"))
        colors = [(c[0], c[1], c[2]) for c in call.data["colors"]]
        gradient = bool(call.data["gradient"])
        for coord in coords:
            await coord.async_set_razer_segments(colors, gradient=gradient)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SEGMENT_COLOR,
        handle_set_segment_color,
        schema=SERVICE_SET_SEGMENT_COLOR_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_SCENE,
        handle_send_scene,
        schema=SERVICE_SEND_SCENE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MUSIC_MODE,
        handle_send_music_mode,
        schema=SERVICE_SEND_MUSIC_MODE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_RAW_PTREAL,
        handle_send_raw_ptreal,
        schema=SERVICE_SEND_RAW_PTREAL_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_RAZER_SEGMENTS,
        handle_set_razer_segments,
        schema=SERVICE_SET_RAZER_SEGMENTS_SCHEMA,
    )
    store[SERVICES_REGISTERED_KEY] = True
