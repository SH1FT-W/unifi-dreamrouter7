"""WLAN-Passwort je WLAN mit Passwort (WPA-Personal) ändern."""

from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Wlan
from .const import DOMAIN
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import UdrEntity, track_new_items

PARALLEL_UPDATES = 1

# WPA2/WPA3-Personal: 8 bis 63 Zeichen (prüft HA selbst anhand native_min/native_max)
MIN_PASSPHRASE = 8
MAX_PASSPHRASE = 63


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entry.async_on_unload(
        track_new_items(
            coordinator,
            lambda d: [w.id for w in d.wlans.values() if w.security == "wpapsk"],
            lambda ids: (WlanPassword(coordinator, coordinator.data.wlans[i]) for i in ids),
            async_add_entities,
        )
    )


class WlanPassword(UdrEntity, TextEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.PASSWORD
    _attr_native_min = MIN_PASSPHRASE
    _attr_native_max = MAX_PASSPHRASE
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: UdrCoordinator, wlan: Wlan) -> None:
        super().__init__(coordinator, f"wlan_{wlan.id}_password")
        self._attr_translation_key = "wlan_password"
        self._attr_translation_placeholders = {"name": wlan.name}
        self._id = wlan.id

    @property
    def available(self) -> bool:
        return super().available and self._id in self.coordinator.data.wlans

    @property
    def native_value(self) -> str | None:
        wlan = self.coordinator.data.wlans.get(self._id)
        return wlan.passphrase if wlan else None

    async def async_set_value(self, value: str) -> None:
        wlan = self.coordinator.data.wlans.get(self._id)
        if wlan is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        await self.coordinator.async_command(lambda: self.coordinator.client.set_wlan(wlan, x_passphrase=value))
