"""Utility button entities for Govee H60A6 Local."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up button entities for Govee H60A6 Local."""
    coordinator: GoveeH60A6Coordinator = hass.data[DOMAIN][COORDINATORS_KEY][
        entry.entry_id
    ]
    async_add_entities(
        [
            GoveeH60A6RefreshButton(coordinator),
            GoveeH60A6StopEffectButton(coordinator),
        ]
    )


class GoveeH60A6RefreshButton(
    CoordinatorEntity[GoveeH60A6Coordinator], ButtonEntity
):
    """Triggers an immediate local `devStatus` refresh."""

    _attr_has_entity_name = True
    _attr_name = "Refresh Status"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: GoveeH60A6Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_refresh"
        self._attr_device_info = build_device_info(coordinator)

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class GoveeH60A6StopEffectButton(
    CoordinatorEntity[GoveeH60A6Coordinator], ButtonEntity
):
    """Stops any running dynamic effect and returns to solid color/white."""

    _attr_has_entity_name = True
    _attr_name = "Stop Effect"
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, coordinator: GoveeH60A6Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_stop_effect"
        self._attr_device_info = build_device_info(coordinator)

    async def async_press(self) -> None:
        await self.coordinator.async_stop_effect()
