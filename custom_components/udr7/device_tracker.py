"""Anwesenheit der in den Optionen ausgewählten Clients (z. B. Handys)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import ClientEntity, tracked_clients

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ClientTracker(coordinator, mac) for mac in tracked_clients(entry))


class ClientTracker(ClientEntity, ScannerEntity):
    """Zu Hause, solange der Client verbunden ist; Handys schlafen im WLAN, deshalb zählt der letzte Kontakt
    noch `away_seconds` lang (Optionen, Standard 5 Minuten)."""

    # Ein device_tracker legt kein eigenes Gerät an (HA hängt ihn über die MAC an); Name = Client
    _attr_has_entity_name = False

    def __init__(self, coordinator: UdrCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "client")
        self._away = int(coordinator.config_entry.options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS))
        client = self.client
        self._attr_name = client.display_name if client else mac

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def ip_address(self) -> str | None:
        client = self.coordinator.data.clients.get(self._mac)
        return client.ip or None if client else None

    @property
    def hostname(self) -> str | None:
        # HA benennt das Gerät des Trackers nach dem Hostnamen; der Name aus UniFi ist der sprechendere
        client = self.client
        return client.display_name if client else None

    @property
    def is_connected(self) -> bool:
        data = self.coordinator.data
        active = data.clients.get(self._mac)
        # Nach dem Trennen steht der letzte Kontakt noch in rest/user
        client = active or data.known_clients.get(self._mac)
        if client is None or client.last_seen is None:
            return active is not None
        return (dt_util.utcnow() - client.last_seen).total_seconds() <= self._away

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        client = self.coordinator.data.clients.get(self._mac)
        if client is None:
            return None
        attrs: dict[str, Any] = {"network": client.network or None, "wired": client.wired}
        if client.essid:
            attrs["ssid"] = client.essid
        if (device := self.coordinator.data.devices.get(client.uplink_mac)) is not None:
            attrs["connected_to"] = device.name
        return attrs
