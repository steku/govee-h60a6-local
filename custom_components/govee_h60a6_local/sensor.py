"""Diagnostic sensor entities for Govee H60A6 Local."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import COORDINATORS_KEY
from .const import DOMAIN
from .coordinator import GoveeH60A6Coordinator
from .light import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic sensors for Govee H60A6 Local."""
    coordinator: GoveeH60A6Coordinator = hass.data[DOMAIN][COORDINATORS_KEY][
        entry.entry_id
    ]
    async_add_entities([GoveeH60A6TransportSensor(coordinator)])


class GoveeH60A6TransportSensor(
    CoordinatorEntity[GoveeH60A6Coordinator], SensorEntity
):
    """Reports the active local transport ('wifi', 'ble', or 'offline')."""

    _attr_has_entity_name = True
    _attr_name = "Active Transport"
    _attr_icon = "mdi:wifi-arrow-left-right"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: GoveeH60A6Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_transport"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def native_value(self) -> str:
        return self.coordinator.state.active_transport

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "last_error": self.coordinator.state.last_error,
            "configured_mode": self.coordinator.client.transport_mode,
        }
