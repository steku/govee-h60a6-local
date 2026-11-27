"""DataUpdateCoordinator and local effect/transition engine for Govee H60A6."""

from __future__ import annotations

import asyncio
import colorsys
from datetime import timedelta
import logging
import math
import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ALL_RING_SEGMENTS_MASK,
    CONF_BLE_VERSION,
    CONF_DEVICE_ID,
    CONF_SKU,
    CONF_WIFI_VERSION,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    EFFECT_SOLID,
    LOCAL_EFFECTS,
    MAIN_CENTER_SEGMENT,
    MAX_KELVIN,
    MIN_KELVIN,
    MODEL_SKU,
    NATIVE_MUSIC_MODES,
    RING_SEGMENT_COUNT,
    TOTAL_SEGMENT_COUNT,
)
from .protocol import (
    GoveeState,
    clamp,
    kelvin_to_rgb,
    parse_hex_to_ble_packet,
    segments_to_bitmask,
)
from .scenes import GoveeScene, load_h60a6_scenes
from .transport import GoveeH60A6Client, GoveeTransportError

_LOGGER = logging.getLogger(__name__)


def _compute_local_effect_rgb(effect: str, phase: float) -> tuple[int, int, int]:
    """Compute the RGB color for a local software effect at a given phase (seconds)."""
    if effect == "Rainbow Flow":
        r, g, b = colorsys.hsv_to_rgb((phase * 0.05) % 1.0, 1.0, 1.0)
        return (round(r * 255), round(g * 255), round(b * 255))

    if effect == "Aurora Pulse":
        # Cycles smoothly through teal, emerald, violet, and cyan
        hue = 0.42 + 0.18 * math.sin(phase * 0.35)
        sat = 0.85 + 0.15 * math.cos(phase * 0.5)
        r, g, b = colorsys.hsv_to_rgb(hue % 1.0, clamp(round(sat * 100), 0, 100) / 100.0, 1.0)
        return (round(r * 255), round(g * 255), round(b * 255))

    if effect == "Sunset Drift":
        # Warm amber, coral, magenta, deep orange
        t = (math.sin(phase * 0.25) + 1.0) / 2.0
        r = 255
        g = round(45 + 115 * t)
        b = round(10 + 95 * (1.0 - t))
        return (r, g, b)

    if effect == "Ocean Wave":
        t = (math.sin(phase * 0.4) + 1.0) / 2.0
        return (round(10 + 30 * t), round(90 + 145 * t), round(180 + 75 * (1.0 - t)))

    if effect == "Candle Flicker":
        jitter = random.uniform(-0.12, 0.12)
        base = max(0.55, min(1.0, 0.82 + 0.1 * math.sin(phase * 3.1) + jitter))
        return (round(255 * base), round(125 * base), round(22 * base))

    if effect == "Forest Breeze":
        t = (math.sin(phase * 0.3) + 1.0) / 2.0
        return (round(20 + 60 * t), round(160 + 85 * t), round(35 + 65 * (1.0 - t)))

    if effect == "Cyber Neon":
        # Alternates electric pink and cyan
        t = (math.sin(phase * 0.7) + 1.0) / 2.0
        return (round(255 * t), round(40 + 200 * (1.0 - t)), round(180 + 75 * (1.0 - t)))

    if effect == "Warm Breathing":
        t = (math.sin(phase * 0.5) + 1.0) / 2.0
        kelvin = round(2200 + t * 1400)
        return kelvin_to_rgb(kelvin)

    return (255, 255, 255)


class GoveeH60A6Coordinator(DataUpdateCoordinator[GoveeState]):
    """Coordinates local state polling, command dispatch, and effects for H60A6."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: GoveeH60A6Client,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.entry = entry
        self.client = client
        self.state = GoveeState(
            device_id=entry.data.get(CONF_DEVICE_ID),
            sku=entry.data.get(CONF_SKU) or MODEL_SKU,
            wifi_version=entry.data.get(CONF_WIFI_VERSION),
            ble_version=entry.data.get(CONF_BLE_VERSION),
        )

        self._effect_task: asyncio.Task[None] | None = None
        self._transition_task: asyncio.Task[None] | None = None
        self._segments_initialized: bool = False
        self.scenes: dict[str, GoveeScene] = load_h60a6_scenes()

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=poll_interval),
        )

    @property
    def available_effects(self) -> list[str]:
        """Return verified hardware scenes + local effects for HA's Effect dropdown."""
        native_scenes = [
            name for name, scene in self.scenes.items() if scene.scene_param
        ]
        return [
            EFFECT_SOLID,
            *native_scenes,
            *LOCAL_EFFECTS,
        ]

    async def _async_update_data(self) -> GoveeState:
        """Poll `devStatus` over Wi-Fi LAN (or maintain optimistic BLE state)."""
        try:
            status = await self.client.async_get_status()
        except GoveeTransportError as err:
            self.state.active_transport = self.client.active_transport
            self.state.last_error = str(err)
            self.state.available = False
            raise UpdateFailed(str(err)) from err

        if status is not None:
            prev_is_on = self.state.is_on
            prev_brightness = self.state.brightness
            prev_rgb = self.state.rgb
            prev_kelvin = self.state.color_temp_kelvin
            self.state.is_on = status.is_on
            self.state.brightness = status.brightness
            self.state.color_temp_kelvin = status.color_temp_kelvin
            if status.color_temp_kelvin == 0:
                self.state.rgb = status.rgb
            seed_rgb = (
                status.rgb
                if status.color_temp_kelvin == 0
                else kelvin_to_rgb(status.color_temp_kelvin)
            )
            if not self._segments_initialized:
                self._sync_segments_color(
                    seed_rgb,
                    color_temp_kelvin=status.color_temp_kelvin,
                    mark_on=status.is_on,
                )
                self._sync_segments_brightness(status.brightness)
                self._segments_initialized = True
            else:
                color_changed = (
                    status.color_temp_kelvin != prev_kelvin
                    or (status.color_temp_kelvin == 0 and status.rgb != prev_rgb)
                )
                if color_changed:
                    self._sync_segments_color(
                        seed_rgb,
                        color_temp_kelvin=status.color_temp_kelvin,
                        mark_on=status.is_on,
                        reset_brightness=True,
                    )
                elif (
                    not prev_is_on
                    and status.is_on
                    and not any(seg.is_on for seg in self.state.segments)
                ):
                    self.state.ring.is_on = True
                    for seg in self.state.segments:
                        seg.is_on = True
                if status.brightness != prev_brightness:
                    self._sync_segments_brightness(status.brightness)
            if not status.is_on:
                await self._cancel_effect_task()
                self.state.effect = None

        self.state.active_transport = self.client.active_transport
        self.state.available = self.client.active_transport != "offline"
        self.state.last_error = None
        return self.state

    async def _cancel_effect_task(self) -> None:
        """Stop any running local software animation loop."""
        if self._effect_task is not None and not self._effect_task.done():
            self._effect_task.cancel()
            try:
                await self._effect_task
            except asyncio.CancelledError:
                pass
        self._effect_task = None
        if self.state.razer_active:
            self.state.razer_active = False
            if self.client.wifi_enabled:
                try:
                    await self.client.async_set_razer_mode(False)
                except Exception:
                    pass

    async def _cancel_transition_task(self) -> None:
        """Stop any active smooth transition."""
        if self._transition_task is not None and not self._transition_task.done():
            self._transition_task.cancel()
            try:
                await self._transition_task
            except asyncio.CancelledError:
                pass
        self._transition_task = None

    async def async_shutdown_tasks(self) -> None:
        """Cancel background tasks and close BLE connections on unload."""
        await self._cancel_transition_task()
        await self._cancel_effect_task()
        await self.client.async_close()

    def _sync_segments_color(
        self,
        rgb: tuple[int, int, int],
        *,
        color_temp_kelvin: int = 0,
        mark_on: bool = False,
        reset_brightness: bool = False,
    ) -> None:
        """Keep tracked ring and per-segment RGB/Kelvin aligned with whole-fixture changes.

        Individual ring segments (1..12) remain in RGB mode (`color_temp_kelvin = 0`)
        so toggling a single Ring Segment entity always targets only its bitmask via
        pure RGB `33 05 15 01` rather than a multi-zone white/brightness frame.
        """
        self.state.ring.rgb = rgb
        self.state.ring.color_temp_kelvin = color_temp_kelvin
        if mark_on:
            self.state.ring.is_on = True
        if reset_brightness:
            self.state.ring.brightness = 100
        for seg in self.state.segments[:RING_SEGMENT_COUNT]:
            seg.rgb = rgb
            seg.color_temp_kelvin = 0
        center_seg = self.state.segments[MAIN_CENTER_SEGMENT - 1]
        center_seg.rgb = rgb
        center_seg.color_temp_kelvin = color_temp_kelvin
        for seg in self.state.segments:
            if mark_on:
                seg.is_on = True
            if reset_brightness:
                seg.brightness = 100

    def _sync_segments_brightness(self, brightness_pct: int) -> None:
        """Keep tracked ring and per-segment brightness aligned with main-light dimming."""
        pct = clamp(brightness_pct, 1, 100)
        self.state.ring.brightness = pct
        for seg in self.state.segments:
            seg.brightness = pct

    def _notify_state(self) -> None:
        self.state.active_transport = self.client.active_transport
        self.state.available = self.client.active_transport != "offline"
        self.state.last_error = None
        self.async_set_updated_data(self.state)

    async def async_refresh_status(self) -> None:
        """Force an immediate full status refresh (preferring BLE 0xAC query when available)."""
        if self.client.ble_enabled:
            try:
                ble_status = await self.client.async_get_ble_status()
            except Exception as err:
                _LOGGER.debug(
                    "BLE full status query failed (%s); falling back to coordinator refresh",
                    err,
                )
                ble_status = None

            if ble_status is not None:
                self.state.is_on = ble_status.is_on
                self.state.brightness = ble_status.brightness
                self.state.color_temp_kelvin = ble_status.color_temp_kelvin
                if ble_status.color_temp_kelvin == 0:
                    self.state.rgb = ble_status.rgb

                if not ble_status.is_on:
                    await self._cancel_effect_task()
                    self.state.effect = None
                elif ble_status.scene_code is not None:
                    await self._cancel_effect_task()
                    matched_name = next(
                        (
                            name
                            for name, scene in self.scenes.items()
                            if scene.scene_code == ble_status.scene_code
                        ),
                        None,
                    )
                    self.state.effect = matched_name or f"Scene #{ble_status.scene_code}"
                elif self._effect_task is None:
                    self.state.effect = None

                ring_segments = self.state.segments[:RING_SEGMENT_COUNT]
                for seg, (seg_b, seg_rgb) in zip(ring_segments, ble_status.ring_segments):
                    has_color = any(seg_rgb)
                    seg.is_on = seg_b > 0 and has_color
                    if seg_b > 0:
                        seg.brightness = clamp(seg_b, 1, 100)
                    if has_color:
                        seg.rgb = seg_rgb
                    seg.color_temp_kelvin = 0

                if ble_status.ring_segments:
                    self.state.ring.is_on = any(s.is_on for s in ring_segments)
                else:
                    self.state.ring.is_on = ble_status.is_on
                self.state.ring.color_temp_kelvin = ble_status.color_temp_kelvin
                self.state.ring.rgb = ble_status.rgb
                self.state.ring.brightness = ble_status.brightness

                center_seg = self.state.segments[MAIN_CENTER_SEGMENT - 1]
                if ble_status.center_on is not None:
                    center_seg.is_on = ble_status.center_on
                    center_seg.color_temp_kelvin = ble_status.color_temp_kelvin
                    center_seg.brightness = ble_status.brightness
                    if ble_status.center_rgb is not None and any(ble_status.center_rgb):
                        center_seg.rgb = ble_status.center_rgb

                self._segments_initialized = True
                self._notify_state()
                return

        await self.async_request_refresh()

    # -----------------------------------------------------------------------
    # Primary Light Controls
    # -----------------------------------------------------------------------

    async def async_turn_on(
        self,
        *,
        brightness_pct: int | None = None,
        rgb: tuple[int, int, int] | None = None,
        color_temp_kelvin: int | None = None,
        effect: str | None = None,
        transition: float | None = None,
    ) -> None:
        """Turn on the H60A6 and apply any requested attributes."""
        await self._cancel_transition_task()

        if effect is not None:
            if effect == EFFECT_SOLID:
                await self._cancel_effect_task()
                self.state.effect = None
            else:
                await self.async_start_effect(effect)
                if brightness_pct is not None:
                    await self.client.async_set_brightness(brightness_pct)
                    pct = clamp(brightness_pct, 1, 100)
                    self.state.brightness = pct
                    self._sync_segments_brightness(pct)
                self._notify_state()
                return

        if rgb is not None or color_temp_kelvin is not None:
            await self._cancel_effect_task()
            self.state.effect = None

        if transition is not None and transition > 0.25 and (
            brightness_pct is not None
            or rgb is not None
            or color_temp_kelvin is not None
        ):
            if not self.state.is_on:
                await self.client.async_turn(True)
                self.state.is_on = True
            self._transition_task = self.hass.async_create_task(
                self._run_transition(
                    duration=transition,
                    target_brightness=brightness_pct,
                    target_rgb=rgb,
                    target_kelvin=color_temp_kelvin,
                )
            )
            self._notify_state()
            return

        all_segments_off = not any(seg.is_on for seg in self.state.segments)
        if (
            not self.state.is_on
            or self.client.active_transport != "wifi"
            or (brightness_pct is None and rgb is None and color_temp_kelvin is None)
        ):
            await self.client.async_turn(True)
            self.state.is_on = True
            if all_segments_off:
                self.state.ring.is_on = True
                for seg in self.state.segments:
                    seg.is_on = True

        if all_segments_off and rgb is None and color_temp_kelvin is None:
            if self.state.color_temp_kelvin > 0:
                await self.client.async_set_color_temp(self.state.color_temp_kelvin)
                self._sync_segments_color(
                    kelvin_to_rgb(self.state.color_temp_kelvin),
                    color_temp_kelvin=self.state.color_temp_kelvin,
                    mark_on=True,
                )
            else:
                await self.client.async_set_rgb(*self.state.rgb)
                self._sync_segments_color(self.state.rgb, mark_on=True)
            self._segments_initialized = True

        if rgb is not None:
            r, g, b = clamp(rgb[0], 0, 255), clamp(rgb[1], 0, 255), clamp(rgb[2], 0, 255)
            await self.client.async_set_rgb(r, g, b)
            self.state.rgb = (r, g, b)
            self.state.color_temp_kelvin = 0
            self._sync_segments_color((r, g, b), mark_on=True, reset_brightness=True)
            self._segments_initialized = True

        if color_temp_kelvin is not None:
            kelvin = clamp(color_temp_kelvin, MIN_KELVIN, MAX_KELVIN)
            await self.client.async_set_color_temp(kelvin)
            self.state.color_temp_kelvin = kelvin
            self._sync_segments_color(
                kelvin_to_rgb(kelvin),
                color_temp_kelvin=kelvin,
                mark_on=True,
                reset_brightness=True,
            )
            self._segments_initialized = True

        if brightness_pct is not None:
            pct = clamp(brightness_pct, 1, 100)
            await self.client.async_set_brightness(pct)
            self.state.brightness = pct
            self._sync_segments_brightness(pct)

        self._notify_state()

    async def async_turn_off(self, *, transition: float | None = None) -> None:
        """Turn off the H60A6 (with optional smooth fade-out)."""
        await self._cancel_transition_task()
        await self._cancel_effect_task()
        self.state.effect = None

        if transition is not None and transition > 0.25 and self.state.is_on:
            self._transition_task = self.hass.async_create_task(
                self._run_fade_off(duration=transition)
            )
            return

        await self.client.async_turn(False)
        self.state.is_on = False
        self._notify_state()

    async def _run_transition(
        self,
        *,
        duration: float,
        target_brightness: int | None,
        target_rgb: tuple[int, int, int] | None,
        target_kelvin: int | None,
    ) -> None:
        """Smoothly interpolate brightness, RGB, or Kelvin without flooding UDP."""
        steps = max(2, min(20, round(duration / 0.25)))
        step_sleep = duration / steps

        start_b = self.state.brightness
        start_rgb = (
            self.state.rgb
            if self.state.color_temp_kelvin == 0
            else kelvin_to_rgb(self.state.color_temp_kelvin)
        )
        start_k = self.state.color_temp_kelvin or 4000

        try:
            for idx in range(1, steps + 1):
                frac = idx / steps
                if target_rgb is not None:
                    r = round(start_rgb[0] + (target_rgb[0] - start_rgb[0]) * frac)
                    g = round(start_rgb[1] + (target_rgb[1] - start_rgb[1]) * frac)
                    b = round(start_rgb[2] + (target_rgb[2] - start_rgb[2]) * frac)
                    await self.client.async_set_rgb(r, g, b)
                    self.state.rgb = (r, g, b)
                    self.state.color_temp_kelvin = 0
                    self._sync_segments_color((r, g, b), mark_on=True, reset_brightness=True)
                    self._segments_initialized = True
                elif target_kelvin is not None:
                    k = round(start_k + (target_kelvin - start_k) * frac)
                    await self.client.async_set_color_temp(k)
                    self.state.color_temp_kelvin = k
                    self._sync_segments_color(
                        kelvin_to_rgb(k),
                        color_temp_kelvin=k,
                        mark_on=True,
                        reset_brightness=True,
                    )
                    self._segments_initialized = True

                if target_brightness is not None:
                    pct = clamp(
                        round(start_b + (target_brightness - start_b) * frac), 1, 100
                    )
                    await self.client.async_set_brightness(pct)
                    self.state.brightness = pct
                    self._sync_segments_brightness(pct)

                self._notify_state()
                if idx < steps:
                    await asyncio.sleep(step_sleep)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.warning("Transition interrupted on H60A6: %s", err)

    async def _run_fade_off(self, *, duration: float) -> None:
        """Fade brightness down to 1% then switch off."""
        start_b = self.state.brightness
        steps = max(2, min(15, round(duration / 0.25)))
        step_sleep = duration / steps
        try:
            for idx in range(1, steps + 1):
                pct = clamp(round(start_b * (1.0 - idx / steps)), 1, 100)
                await self.client.async_set_brightness(pct)
                self.state.brightness = pct
                self._notify_state()
                await asyncio.sleep(step_sleep)
            await self.client.async_turn(False)
            self.state.is_on = False
            self._notify_state()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.warning("Fade-off interrupted on H60A6: %s", err)

    # -----------------------------------------------------------------------
    # Effects, Native Scenes & Music Modes
    # -----------------------------------------------------------------------

    async def async_start_effect(self, effect: str) -> None:
        """Start either a native ptReal scene/music mode or a local software effect."""
        await self._cancel_effect_task()

        if not self.state.is_on:
            await self.client.async_turn(True)
            self.state.is_on = True

        if effect in self.scenes:
            scene = self.scenes[effect]
            await self.client.async_set_scene(scene.scene_code, scene.scene_param)
            self.state.effect = effect
            self.state.ring.is_on = True
            for seg in self.state.segments:
                seg.is_on = True
            self._notify_state()
            return

        if effect in NATIVE_MUSIC_MODES:
            await self.client.async_set_music_mode(NATIVE_MUSIC_MODES[effect], sensitivity=60)
            self.state.effect = effect
            self.state.ring.is_on = True
            for seg in self.state.segments:
                seg.is_on = True
            self._notify_state()
            return

        if effect in LOCAL_EFFECTS:
            self.state.effect = effect
            self.state.ring.is_on = True
            for seg in self.state.segments:
                seg.is_on = True
            self._effect_task = self.hass.async_create_task(
                self._run_local_effect(effect)
            )
            self._notify_state()
            return

        raise ValueError(f"Unknown effect: {effect}")

    async def async_stop_effect(self) -> None:
        """Stop any running dynamic effect and restore the last solid color/white."""
        await self._cancel_effect_task()
        self.state.effect = None
        if self.state.is_on:
            if self.state.color_temp_kelvin > 0:
                await self.client.async_set_color_temp(self.state.color_temp_kelvin)
            else:
                await self.client.async_set_rgb(*self.state.rgb)
        self._notify_state()

    async def _run_local_effect(self, effect: str) -> None:
        """Drive a smooth 100% local animated color effect over LAN/BLE."""
        phase = 0.0
        interval = 0.45 if effect == "Candle Flicker" else 0.55
        try:
            while True:
                r, g, b = _compute_local_effect_rgb(effect, phase)
                await self.client.async_set_rgb(r, g, b)
                self.state.rgb = (r, g, b)
                self.state.color_temp_kelvin = 0
                phase += interval
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.warning("Local effect '%s' stopped due to error: %s", effect, err)
            self.state.effect = None
            self._notify_state()

    # -----------------------------------------------------------------------
    # Ambient Backlight Ring & Individual Segment Controls (ptReal / BLE)
    # -----------------------------------------------------------------------

    async def async_set_ring(
        self,
        *,
        is_on: bool,
        rgb: tuple[int, int, int] | None = None,
        color_temp_kelvin: int | None = None,
        brightness_pct: int | None = None,
    ) -> None:
        """Control the H60A6 ambient halo ring (segments 1..12) via ptReal/BLE."""
        await self._cancel_effect_task()
        ring_segments = self.state.segments[:RING_SEGMENT_COUNT]
        if not is_on or brightness_pct == 0:
            await self.client.async_set_segment_brightness(0, ALL_RING_SEGMENTS_MASK)
            self.state.ring.is_on = False
            for seg in ring_segments:
                seg.is_on = False
            self._segments_initialized = True
            self._notify_state()
            return

        if not self.state.is_on:
            await self.client.async_turn(True)
            self.state.is_on = True

        if rgb is None and color_temp_kelvin is None and brightness_pct is None:
            # Restore each ring segment's own remembered RGB/Kelvin & brightness
            grouped: dict[tuple[tuple[int, int, int], int, int], list[int]] = {}
            for seg_idx, seg in enumerate(ring_segments, start=1):
                key = (seg.rgb, seg.color_temp_kelvin, max(1, seg.brightness))
                grouped.setdefault(key, []).append(seg_idx)
            for (seg_rgb, seg_k, seg_b), indices in grouped.items():
                mask = segments_to_bitmask(indices)
                if seg_k > 0:
                    await self.client.async_set_segment_color_temp(
                        seg_k, segment_mask=mask, brightness_pct=seg_b
                    )
                else:
                    await self.client.async_set_segment_color(
                        seg_rgb[0],
                        seg_rgb[1],
                        seg_rgb[2],
                        segment_mask=mask,
                        brightness_pct=seg_b,
                    )
            self.state.ring.is_on = True
            for seg in ring_segments:
                seg.is_on = True
            self._segments_initialized = True
            self._notify_state()
            return

        target_b = (
            clamp(brightness_pct, 1, 100)
            if brightness_pct is not None
            else max(1, self.state.ring.brightness)
        )
        if color_temp_kelvin is not None:
            kelvin = clamp(color_temp_kelvin, MIN_KELVIN, MAX_KELVIN)
            await self.client.async_set_segment_color_temp(
                kelvin, segment_mask=ALL_RING_SEGMENTS_MASK, brightness_pct=target_b
            )
            ring_rgb = kelvin_to_rgb(kelvin)
            self.state.ring.is_on = True
            self.state.ring.rgb = ring_rgb
            self.state.ring.color_temp_kelvin = kelvin
            self.state.ring.brightness = target_b
            for seg in ring_segments:
                seg.is_on = True
                seg.rgb = ring_rgb
                seg.color_temp_kelvin = 0
                seg.brightness = target_b
        elif rgb is not None:
            await self.client.async_set_segment_color(
                rgb[0],
                rgb[1],
                rgb[2],
                segment_mask=ALL_RING_SEGMENTS_MASK,
                brightness_pct=target_b,
            )
            self.state.ring.is_on = True
            self.state.ring.rgb = rgb
            self.state.ring.color_temp_kelvin = 0
            self.state.ring.brightness = target_b
            for seg in ring_segments:
                seg.is_on = True
                seg.rgb = rgb
                seg.color_temp_kelvin = 0
                seg.brightness = target_b
        else:
            # Brightness-only update preserves ring Kelvin vs RGB mode
            if self.state.ring.color_temp_kelvin > 0:
                await self.client.async_set_segment_color_temp(
                    self.state.ring.color_temp_kelvin,
                    segment_mask=ALL_RING_SEGMENTS_MASK,
                    brightness_pct=target_b,
                )
            else:
                await self.client.async_set_segment_color(
                    self.state.ring.rgb[0],
                    self.state.ring.rgb[1],
                    self.state.ring.rgb[2],
                    segment_mask=ALL_RING_SEGMENTS_MASK,
                    brightness_pct=target_b,
                )
            self.state.ring.is_on = True
            self.state.ring.brightness = target_b
            for seg in ring_segments:
                seg.is_on = True
                seg.brightness = target_b

        self._segments_initialized = True
        self._notify_state()

    async def async_set_center(
        self,
        *,
        is_on: bool,
        rgb: tuple[int, int, int] | None = None,
        color_temp_kelvin: int | None = None,
        brightness_pct: int | None = None,
    ) -> None:
        """Control the H60A6 main center panel (segment 13, bitmask 0x1000)."""
        await self._cancel_effect_task()
        await self.async_set_segments(
            [MAIN_CENTER_SEGMENT],
            is_on=is_on,
            rgb=rgb,
            color_temp_kelvin=color_temp_kelvin,
            brightness_pct=brightness_pct,
        )

    async def async_set_segments(
        self,
        segment_indices: list[int],
        *,
        is_on: bool = True,
        rgb: tuple[int, int, int] | None = None,
        color_temp_kelvin: int | None = None,
        brightness_pct: int | None = None,
    ) -> None:
        """Control specific 1-based segments (1..12 = ring, 13 = main center) via ptReal/BLE."""
        valid = [idx for idx in segment_indices if 1 <= idx <= TOTAL_SEGMENT_COUNT]
        if not valid:
            raise ValueError(f"Segments must be between 1 and {TOTAL_SEGMENT_COUNT}")

        mask = segments_to_bitmask(valid)
        if not is_on or brightness_pct == 0:
            await self.client.async_set_segment_brightness(0, mask)
            for idx in valid:
                self.state.segments[idx - 1].is_on = False
            self.state.ring.is_on = any(
                seg.is_on for seg in self.state.segments[:RING_SEGMENT_COUNT]
            )
            self._segments_initialized = True
            self._notify_state()
            return

        if not self.state.is_on:
            await self.client.async_turn(True)
            self.state.is_on = True

        if rgb is None and color_temp_kelvin is None and brightness_pct is None:
            # Restore each requested segment's own remembered RGB/Kelvin and brightness
            grouped: dict[tuple[tuple[int, int, int], int, int], list[int]] = {}
            for idx in valid:
                seg = self.state.segments[idx - 1]
                key = (seg.rgb, seg.color_temp_kelvin, max(1, seg.brightness))
                grouped.setdefault(key, []).append(idx)
            for (seg_rgb, seg_k, seg_b), indices in grouped.items():
                sub_mask = segments_to_bitmask(indices)
                if seg_k > 0:
                    await self.client.async_set_segment_color_temp(
                        seg_k, segment_mask=sub_mask, brightness_pct=seg_b
                    )
                else:
                    await self.client.async_set_segment_color(
                        seg_rgb[0],
                        seg_rgb[1],
                        seg_rgb[2],
                        segment_mask=sub_mask,
                        brightness_pct=seg_b,
                    )
            for idx in valid:
                self.state.segments[idx - 1].is_on = True
        else:
            first_seg = self.state.segments[valid[0] - 1]
            target_b = (
                clamp(brightness_pct, 1, 100)
                if brightness_pct is not None
                else max(1, first_seg.brightness)
            )
            if color_temp_kelvin is not None:
                kelvin = clamp(color_temp_kelvin, MIN_KELVIN, MAX_KELVIN)
                await self.client.async_set_segment_color_temp(
                    kelvin, segment_mask=mask, brightness_pct=target_b
                )
                kelvin_rgb = kelvin_to_rgb(kelvin)
                for idx in valid:
                    seg = self.state.segments[idx - 1]
                    seg.is_on = True
                    seg.rgb = kelvin_rgb
                    seg.color_temp_kelvin = kelvin
                    seg.brightness = target_b
            elif rgb is not None:
                await self.client.async_set_segment_color(
                    rgb[0],
                    rgb[1],
                    rgb[2],
                    segment_mask=mask,
                    brightness_pct=target_b,
                )
                for idx in valid:
                    seg = self.state.segments[idx - 1]
                    seg.is_on = True
                    seg.rgb = rgb
                    seg.color_temp_kelvin = 0
                    seg.brightness = target_b
            elif first_seg.color_temp_kelvin > 0:
                await self.client.async_set_segment_color_temp(
                    first_seg.color_temp_kelvin,
                    segment_mask=mask,
                    brightness_pct=target_b,
                )
                for idx in valid:
                    seg = self.state.segments[idx - 1]
                    seg.is_on = True
                    seg.brightness = target_b
            else:
                await self.client.async_set_segment_color(
                    first_seg.rgb[0],
                    first_seg.rgb[1],
                    first_seg.rgb[2],
                    segment_mask=mask,
                    brightness_pct=target_b,
                )
                for idx in valid:
                    seg = self.state.segments[idx - 1]
                    seg.is_on = True
                    seg.brightness = target_b

        if any(idx <= RING_SEGMENT_COUNT for idx in valid):
            self.state.ring.is_on = any(
                seg.is_on for seg in self.state.segments[:RING_SEGMENT_COUNT]
            )
        self._segments_initialized = True
        self._notify_state()

    # -----------------------------------------------------------------------
    # Advanced Service Helpers
    # -----------------------------------------------------------------------

    async def async_send_scene_id(self, scene_id: int) -> None:
        """Send an arbitrary numeric hardware scene ID via ptReal/BLE."""
        await self._cancel_effect_task()
        if not self.state.is_on:
            await self.client.async_turn(True)
            self.state.is_on = True
        await self.client.async_set_scene(scene_id)
        self.state.effect = f"Scene #{scene_id}"
        self._notify_state()

    async def async_send_music_mode(self, mode_id: int, sensitivity: int = 50) -> None:
        """Activate an onboard music mode via ptReal/BLE."""
        await self._cancel_effect_task()
        if not self.state.is_on:
            await self.client.async_turn(True)
            self.state.is_on = True
        await self.client.async_set_music_mode(mode_id, sensitivity)
        self.state.effect = f"Music #{mode_id}"
        self._notify_state()

    async def async_send_raw_ptreal(self, hex_commands: list[str]) -> None:
        """Parse hex frames (auto-padding & XOR checksumming) and transmit."""
        packets = [parse_hex_to_ble_packet(cmd) for cmd in hex_commands]
        await self.client.async_send_raw_packets(packets)
        self._notify_state()

    async def async_set_razer_segments(
        self,
        colors: list[tuple[int, int, int]],
        gradient: bool = True,
    ) -> None:
        """Stream an arbitrary RGB color array directly via Razer/DreamView UDP."""
        await self._cancel_effect_task()
        if not self.state.is_on:
            await self.client.async_turn(True)
            self.state.is_on = True
        if not self.state.razer_active:
            await self.client.async_set_razer_mode(True)
            self.state.razer_active = True
        await self.client.async_send_razer_colors(colors, gradient=gradient)
        self.state.effect = "Razer Stream"
        self._notify_state()
