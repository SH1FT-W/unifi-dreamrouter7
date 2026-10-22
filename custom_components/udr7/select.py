"""Auswahl: Sendeleistung je Funkband (auto, hoch, mittel, niedrig) am Dream Router und an jedem Access Point."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Device
from .const import DOMAIN, RADIO_BANDS, TX_POWER_MODES
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import DeviceEntity, device_keys, track_new_items
from .sensor import BAND_LABELS

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    def _device_entities(macs: set[str]) -> Iterable[Entity]:
        for mac in macs:
            device = coordinator.data.devices[mac]
            for band, radio in device.radios.items():
                if radio.tx_power_mode is not None:
                    yield TxPowerSelect(coordinator, device, band)

    entry.async_on_unload(track_new_items(coordinator, device_keys, _device_entities, async_add_entities))


class TxPowerSelect(DeviceEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = TX_POWER_MODES

    def __init__(self, coordinator: UdrCoordinator, device: Device, band: str) -> None:
        super().__init__(
            coordinator,
            device,
            f"tx_power_{RADIO_BANDS.get(band, band)}",
            "tx_power",
            {"band": BAND_LABELS.get(band, band)},
        )
        self._band = band

    @property
    def current_option(self) -> str | None:
        device = self.device
        radio = device.radios.get(self._band) if device else None
        # „custom“ (feste dBm) gibt es nur in der Network-App; dann steht hier keine Auswahl
        if radio is None or radio.tx_power_mode not in TX_POWER_MODES:
            return None
        return radio.tx_power_mode

    async def async_select_option(self, option: str) -> None:
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_radio(device, self._band, tx_power_mode=option)
        )
