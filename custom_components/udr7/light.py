"""Status-LED je Gerät als Licht: an/aus, bei Geräten mit LED-Ring auch Farbe und Helligkeit; Display des Routers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ColorMode, LightEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Device, UdrData, as_dict
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
    """Status LED of a device. Access points and switches: the Network app's led_override (on/off/default; default =
    the site setting, shown as on). The console itself (gateway): UniFi OS "ledSettings" like the console settings
    page; the Network app stores led_override for it too, but it never reaches the LED (live 05.11.2026, UDR7 on
    UniFi OS 5.1.35). Firmware without ledSettings keeps led_override."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "led")
        mode = ColorMode.RGB if device.has_led_color else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def _console_led(self) -> dict[str, Any] | None:
        """UniFi OS ledSettings if this is the console and its firmware has them."""
        device = self.device
        if device is None or not device.is_gateway:
            return None
        settings = self.coordinator.data.os_settings.get("ledSettings")
        return settings if isinstance(settings, dict) and "enabled" in settings else None

    @property
    def is_on(self) -> bool | None:
        if (console := self._console_led) is not None:
            return bool(console["enabled"])
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
        # The console's led_override has no effect, showing it would only confuse
        return {"override": device.led_override} if device and self._console_led is None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._console_led is not None:
            await self._set_console_led(True)
            return
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        color = brightness = None
        if device.has_led_color:
            color = rgb_to_hex(kwargs[ATTR_RGB_COLOR]) if ATTR_RGB_COLOR in kwargs else None
            brightness = max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)) if ATTR_BRIGHTNESS in kwargs else None
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_led(device, on=True, color=color, brightness=brightness),
            applied=self._applied("on", color, brightness),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._console_led is not None:
            await self._set_console_led(False)
            return
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_led(device, on=False), applied=self._applied("off")
        )

    async def _set_console_led(self, on: bool) -> None:
        def apply(data: UdrData) -> None:
            data.os_settings["ledSettings"] = {**as_dict(data.os_settings.get("ledSettings")), "enabled": on}

        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_os({"ledSettings": {"enabled": on}}), applied=apply
        )

    def _applied(
        self, override: str, color: str | None = None, brightness: int | None = None
    ) -> Callable[[UdrData], None]:
        """Show the accepted LED setting until the next refresh confirms it (see UdrCoordinator.async_command)."""
        mac = self._mac

        def apply(data: UdrData) -> None:
            if (current := data.devices.get(mac)) is None:
                return
            data.devices[mac] = replace(
                current,
                led_override=override,
                led_color=color if color is not None else current.led_color,
                led_brightness=brightness if brightness is not None else current.led_brightness,
            )

        return apply


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
