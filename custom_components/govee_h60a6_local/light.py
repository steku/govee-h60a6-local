"""Light entities for the Govee H60A6 Local integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import COORDINATORS_KEY
from .const import (
    ALL_EFFECTS,
    CONF_BLE_ADDRESS,
    CONF_ENABLE_SEGMENTS,
    CONF_HOST,
    DEFAULT_NAME,
    DOMAIN,
    EFFECT_SOLID,
    MAIN_CENTER_SEGMENT,
    MANUFACTURER,
    MAX_KELVIN,
    MIN_KELVIN,
    MODEL_NAME,
    RING_SEGMENT_COUNT,
)
from .coordinator import GoveeH60A6Coordinator
from .protocol import govee_brightness_to_ha, ha_brightness_to_govee


def build_device_info(coordinator: GoveeH60A6Coordinator) -> DeviceInfo:
    """Build shared DeviceInfo grouping all entities under one H60A6 device."""
    entry = coordinator.entry
    merged = {**entry.data, **entry.options}
    identifier = entry.unique_id or entry.entry_id
    sw_parts = [
        p
        for p in (
            f"Wi-Fi {coordinator.state.wifi_version}"
            if coordinator.state.wifi_version
            else None,
            f"BLE {coordinator.state.ble_version}"
            if coordinator.state.ble_version
            else None,
        )
        if p
    ]
    return DeviceInfo(
        identifiers={(DOMAIN, identifier)},
        name=merged.get(CONF_NAME, DEFAULT_NAME),
        manufacturer=MANUFACTURER,
        model=MODEL_NAME,
        sw_version=" / ".join(sw_parts) if sw_parts else None,
        configuration_url=(
            f"http://{merged[CONF_HOST]}" if merged.get(CONF_HOST) else None
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Govee H60A6 light entities from a config entry."""
    coordinator: GoveeH60A6Coordinator = hass.data[DOMAIN][COORDINATORS_KEY][
        entry.entry_id
    ]
    merged = {**entry.data, **entry.options}
    enable_segments = bool(merged.get(CONF_ENABLE_SEGMENTS, False))

    entities: list[LightEntity] = [
        GoveeH60A6MainLight(coordinator),
        GoveeH60A6CenterLight(coordinator),
        GoveeH60A6RingLight(coordinator),
    ]
    if enable_segments:
        for seg_idx in range(1, RING_SEGMENT_COUNT + 1):
            entities.append(GoveeH60A6SegmentLight(coordinator, seg_idx))

    async_add_entities(entities)


class GoveeH60A6MainLight(CoordinatorEntity[GoveeH60A6Coordinator], LightEntity):
    """Primary Govee H60A6 Ceiling Light entity (RGB + Kelvin + Effects + Transitions)."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_supported_features = (
        LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION
    )
    _attr_min_color_temp_kelvin = MIN_KELVIN
    _attr_max_color_temp_kelvin = MAX_KELVIN

    def __init__(self, coordinator: GoveeH60A6Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_main"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def available(self) -> bool:
        return self.coordinator.state.available or self.coordinator.client.ble_enabled

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.is_on

    @property
    def brightness(self) -> int:
        return govee_brightness_to_ha(self.coordinator.state.brightness)

    @property
    def color_mode(self) -> ColorMode:
        if self.coordinator.state.color_temp_kelvin > 0:
            return ColorMode.COLOR_TEMP
        return ColorMode.RGB

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self.color_mode == ColorMode.RGB:
            return self.coordinator.state.rgb
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        if self.color_mode == ColorMode.COLOR_TEMP:
            return self.coordinator.state.color_temp_kelvin
        return None

    @property
    def effect_list(self) -> list[str]:
        return self.coordinator.available_effects

    @property
    def effect(self) -> str | None:
        return self.coordinator.state.effect

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        merged = {**self.coordinator.entry.data, **self.coordinator.entry.options}
        return {
            "active_transport": self.coordinator.state.active_transport,
            "host": merged.get(CONF_HOST) or None,
            "ble_address": merged.get(CONF_BLE_ADDRESS) or None,
            "sku": self.coordinator.state.sku,
            "razer_active": self.coordinator.state.razer_active,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness_pct: int | None = None
        if ATTR_BRIGHTNESS in kwargs:
            brightness_pct = ha_brightness_to_govee(int(kwargs[ATTR_BRIGHTNESS]))

        rgb: tuple[int, int, int] | None = None
        if ATTR_RGB_COLOR in kwargs:
            raw = kwargs[ATTR_RGB_COLOR]
            rgb = (int(raw[0]), int(raw[1]), int(raw[2]))

        kelvin: int | None = None
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = int(kwargs[ATTR_COLOR_TEMP_KELVIN])

        effect: str | None = kwargs.get(ATTR_EFFECT)
        transition: float | None = (
            float(kwargs[ATTR_TRANSITION]) if ATTR_TRANSITION in kwargs else None
        )

        await self.coordinator.async_turn_on(
            brightness_pct=brightness_pct,
            rgb=rgb,
            color_temp_kelvin=kelvin,
            effect=effect,
            transition=transition,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        transition: float | None = (
            float(kwargs[ATTR_TRANSITION]) if ATTR_TRANSITION in kwargs else None
        )
        await self.coordinator.async_turn_off(transition=transition)


class GoveeH60A6RingLight(
    CoordinatorEntity[GoveeH60A6Coordinator], RestoreEntity, LightEntity
):
    """Ambient backlight halo ring (all 12 outer segments via ptReal/BLE)."""

    _attr_has_entity_name = True
    _attr_name = "Ambient Ring"
    _attr_icon = "mdi:ceiling-light-outline"
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_KELVIN
    _attr_max_color_temp_kelvin = MAX_KELVIN

    def __init__(self, coordinator: GoveeH60A6Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_ring"
        self._attr_device_info = build_device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state == "unavailable":
            return
        attrs = last_state.attributes
        rgb = attrs.get("rgb_color")
        if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
            self.coordinator.state.ring.rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            self.coordinator.state.ring.color_temp_kelvin = 0
            self.coordinator._segments_initialized = True
        kelvin = attrs.get("color_temp_kelvin")
        if isinstance(kelvin, (int, float)) and kelvin > 0:
            self.coordinator.state.ring.color_temp_kelvin = int(kelvin)
            self.coordinator._segments_initialized = True
        br = attrs.get("brightness")
        if isinstance(br, (int, float)):
            self.coordinator.state.ring.brightness = max(1, ha_brightness_to_govee(int(br)))
        ring_enabled = attrs.get("ring_enabled")
        if isinstance(ring_enabled, bool):
            self.coordinator.state.ring.is_on = ring_enabled
        elif self.coordinator.state.is_on:
            self.coordinator.state.ring.is_on = last_state.state == "on"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"ring_enabled": self.coordinator.state.ring.is_on}

    @property
    def available(self) -> bool:
        return self.coordinator.state.available or self.coordinator.client.ble_enabled

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.is_on and self.coordinator.state.ring.is_on

    @property
    def color_mode(self) -> ColorMode:
        if self.coordinator.state.ring.color_temp_kelvin > 0:
            return ColorMode.COLOR_TEMP
        return ColorMode.RGB

    @property
    def brightness(self) -> int:
        return govee_brightness_to_ha(self.coordinator.state.ring.brightness)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self.color_mode == ColorMode.RGB:
            return self.coordinator.state.ring.rgb
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        if self.color_mode == ColorMode.COLOR_TEMP:
            return self.coordinator.state.ring.color_temp_kelvin
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness_pct: int | None = None
        if ATTR_BRIGHTNESS in kwargs:
            brightness_pct = ha_brightness_to_govee(int(kwargs[ATTR_BRIGHTNESS]))

        rgb: tuple[int, int, int] | None = None
        if ATTR_RGB_COLOR in kwargs:
            raw = kwargs[ATTR_RGB_COLOR]
            rgb = (int(raw[0]), int(raw[1]), int(raw[2]))

        kelvin: int | None = (
            int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            if ATTR_COLOR_TEMP_KELVIN in kwargs
            else None
        )

        await self.coordinator.async_set_ring(
            is_on=True,
            rgb=rgb,
            color_temp_kelvin=kelvin,
            brightness_pct=brightness_pct,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_ring(is_on=False)


class GoveeH60A6CenterLight(
    CoordinatorEntity[GoveeH60A6Coordinator], RestoreEntity, LightEntity
):
    """Main center light panel (segment 13, mask 0x1000) via ptReal/BLE."""

    _attr_has_entity_name = True
    _attr_name = "Main Center"
    _attr_icon = "mdi:ceiling-light"
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_KELVIN
    _attr_max_color_temp_kelvin = MAX_KELVIN

    def __init__(self, coordinator: GoveeH60A6Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_center"
        self._attr_device_info = build_device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state == "unavailable":
            return
        seg = self.coordinator.state.segments[MAIN_CENTER_SEGMENT - 1]
        attrs = last_state.attributes
        rgb = attrs.get("rgb_color")
        if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
            seg.rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            seg.color_temp_kelvin = 0
            self.coordinator._segments_initialized = True
        kelvin = attrs.get("color_temp_kelvin")
        if isinstance(kelvin, (int, float)) and kelvin > 0:
            seg.color_temp_kelvin = int(kelvin)
            self.coordinator._segments_initialized = True
        br = attrs.get("brightness")
        if isinstance(br, (int, float)):
            seg.brightness = max(1, ha_brightness_to_govee(int(br)))
        seg_enabled = attrs.get("segment_enabled")
        if isinstance(seg_enabled, bool):
            seg.is_on = seg_enabled
        elif self.coordinator.state.is_on:
            seg.is_on = last_state.state == "on"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seg = self.coordinator.state.segments[MAIN_CENTER_SEGMENT - 1]
        return {"segment_enabled": seg.is_on}

    @property
    def available(self) -> bool:
        return self.coordinator.state.available or self.coordinator.client.ble_enabled

    @property
    def is_on(self) -> bool:
        seg = self.coordinator.state.segments[MAIN_CENTER_SEGMENT - 1]
        return self.coordinator.state.is_on and seg.is_on

    @property
    def color_mode(self) -> ColorMode:
        seg = self.coordinator.state.segments[MAIN_CENTER_SEGMENT - 1]
        if seg.color_temp_kelvin > 0:
            return ColorMode.COLOR_TEMP
        return ColorMode.RGB

    @property
    def brightness(self) -> int:
        seg = self.coordinator.state.segments[MAIN_CENTER_SEGMENT - 1]
        return govee_brightness_to_ha(seg.brightness)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self.color_mode == ColorMode.RGB:
            return self.coordinator.state.segments[MAIN_CENTER_SEGMENT - 1].rgb
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        if self.color_mode == ColorMode.COLOR_TEMP:
            return self.coordinator.state.segments[MAIN_CENTER_SEGMENT - 1].color_temp_kelvin
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness_pct: int | None = None
        if ATTR_BRIGHTNESS in kwargs:
            brightness_pct = ha_brightness_to_govee(int(kwargs[ATTR_BRIGHTNESS]))

        rgb: tuple[int, int, int] | None = None
        if ATTR_RGB_COLOR in kwargs:
            raw = kwargs[ATTR_RGB_COLOR]
            rgb = (int(raw[0]), int(raw[1]), int(raw[2]))

        kelvin: int | None = (
            int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            if ATTR_COLOR_TEMP_KELVIN in kwargs
            else None
        )

        await self.coordinator.async_set_center(
            is_on=True,
            rgb=rgb,
            color_temp_kelvin=kelvin,
            brightness_pct=brightness_pct,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_center(is_on=False)


class GoveeH60A6SegmentLight(
    CoordinatorEntity[GoveeH60A6Coordinator], RestoreEntity, LightEntity
):
    """Individual addressable ambient ring segment (1..12) via ptReal/BLE."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:led-strip-variant"
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(
        self, coordinator: GoveeH60A6Coordinator, segment_index: int
    ) -> None:
        super().__init__(coordinator)
        self._segment_index = segment_index
        self._attr_name = f"Ring Segment {segment_index}"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_segment_{segment_index}"
        self._attr_device_info = build_device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state == "unavailable":
            return
        seg = self.coordinator.state.segments[self._segment_index - 1]
        attrs = last_state.attributes
        rgb = attrs.get("rgb_color")
        if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
            seg.rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            self.coordinator._segments_initialized = True
        br = attrs.get("brightness")
        if isinstance(br, (int, float)):
            seg.brightness = max(1, ha_brightness_to_govee(int(br)))
        seg_enabled = attrs.get("segment_enabled")
        if isinstance(seg_enabled, bool):
            seg.is_on = seg_enabled
        elif self.coordinator.state.is_on:
            seg.is_on = last_state.state == "on"
        self.coordinator.state.ring.is_on = any(
            s.is_on for s in self.coordinator.state.segments[:RING_SEGMENT_COUNT]
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seg = self.coordinator.state.segments[self._segment_index - 1]
        return {"segment_enabled": seg.is_on}

    @property
    def available(self) -> bool:
        return self.coordinator.state.available or self.coordinator.client.ble_enabled

    @property
    def is_on(self) -> bool:
        seg = self.coordinator.state.segments[self._segment_index - 1]
        return self.coordinator.state.is_on and seg.is_on

    @property
    def color_mode(self) -> ColorMode:
        return ColorMode.RGB

    @property
    def brightness(self) -> int:
        seg = self.coordinator.state.segments[self._segment_index - 1]
        return govee_brightness_to_ha(seg.brightness)

    @property
    def rgb_color(self) -> tuple[int, int, int]:
        return self.coordinator.state.segments[self._segment_index - 1].rgb

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness_pct: int | None = None
        if ATTR_BRIGHTNESS in kwargs:
            brightness_pct = ha_brightness_to_govee(int(kwargs[ATTR_BRIGHTNESS]))

        rgb: tuple[int, int, int] | None = None
        if ATTR_RGB_COLOR in kwargs:
            raw = kwargs[ATTR_RGB_COLOR]
            rgb = (int(raw[0]), int(raw[1]), int(raw[2]))

        await self.coordinator.async_set_segments(
            [self._segment_index],
            is_on=True,
            rgb=rgb,
            brightness_pct=brightness_pct,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_segments(
            [self._segment_index],
            is_on=False,
        )
