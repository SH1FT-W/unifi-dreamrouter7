"""Status-LED je Gerät als Licht: an/aus, bei Geräten mit LED-Ring auch Farbe und Helligkeit; Display des Routers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ColorMode, LightEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Device
from .const import DOMAIN
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import DeviceEntity, UdrEntity, device_keys, track_new_items

PARALLEL_UPDATES = 1


def hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    value = value.lstrip("#")
    if len(value) != 6:
        return None
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return None


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    def _device_entities(macs: set[str]) -> Iterable[Entity]:
        for mac in macs:
            device = coordinator.data.devices[mac]
            if device.led_override is not None:
                yield LedLight(coordinator, device)

    entry.async_on_unload(track_new_items(coordinator, device_keys, _device_entities, async_add_entities))
    if "lcmSettings" in coordinator.data.os_settings:
        async_add_entities([DisplayLight(coordinator)])


class LedLight(DeviceEntity, LightEntity):
    """led_override: on/off/default (default = Einstellung der Site, hier wie „an“ angezeigt)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "led")
        mode = ColorMode.RGB if device.has_led_color else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or device.led_override is None:
            return None
        return device.led_override != "off"

    @property
    def brightness(self) -> int | None:
        device = self.device
        if device is None or device.led_brightness is None or not device.has_led_color:
            return None
        # UniFi: 0–100 %, HA: 0–255
        return round(device.led_brightness * 255 / 100)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        device = self.device
        return hex_to_rgb(device.led_color) if device and device.led_color else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        return {"override": device.led_override} if device else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        color = brightness = None
        if device.has_led_color:
            color = rgb_to_hex(kwargs[ATTR_RGB_COLOR]) if ATTR_RGB_COLOR in kwargs else None
            brightness = max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)) if ATTR_BRIGHTNESS in kwargs else None
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_led(device, on=True, color=color, brightness=brightness)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        await self.coordinator.async_command(lambda: self.coordinator.client.set_led(device, on=False))


class DisplayLight(UdrEntity, LightEntity):
    """Display des Dream Routers (UniFi OS „lcmSettings“): an/aus und Helligkeit; eine Farbe gibt es nur bei Konsolen
    mit „Bezel“ (UDR7: nein)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator) -> None:
        super().__init__(coordinator, "display")
        mode = ColorMode.RGB if coordinator.data.os_settings.get("hasBezel") else ColorMode.BRIGHTNESS
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def _settings(self) -> dict[str, Any]:
        settings = self.coordinator.data.os_settings.get("lcmSettings")
        return settings if isinstance(settings, dict) else {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._settings)

    @property
    def is_on(self) -> bool | None:
        enabled = self._settings.get("enabled")
        return bool(enabled) if enabled is not None else None

    @property
    def brightness(self) -> int | None:
        value = self._settings.get("brightness")
        return round(int(value) * 255 / 100) if isinstance(value, int | float) else None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        color = self._settings.get("color")
        if self._attr_color_mode != ColorMode.RGB or not isinstance(color, str):
            return None
        return hex_to_rgb(color)

    async def async_turn_on(self, **kwargs: Any) -> None:
        changes: dict[str, Any] = {"enabled": True}
        if ATTR_RGB_COLOR in kwargs and self._attr_color_mode == ColorMode.RGB:
            changes["color"] = rgb_to_hex(kwargs[ATTR_RGB_COLOR]).lstrip("#").upper()
        if ATTR_BRIGHTNESS in kwargs:
            changes["brightness"] = max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255))
        await self.coordinator.async_command(lambda: self.coordinator.client.set_os({"lcmSettings": changes}))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_os({"lcmSettings": {"enabled": False}})
        )
