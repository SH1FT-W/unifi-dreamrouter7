"""Event: a device the integration has never seen before connected to the network."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import Client, UdrData
from .const import CONF_NEW_DEVICES_WIRED
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import UdrEntity

PARALLEL_UPDATES = 0

EVENT_NEW_DEVICE = "new_device"


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    wired = bool(entry.options.get(CONF_NEW_DEVICES_WIRED, False))
    async_add_entities([NewDeviceEvent(entry.runtime_data, wired=wired)])


def event_attributes(client: Client) -> dict[str, Any]:
    return {
        "mac": client.mac,
        "name": client.name or None,
        "hostname": client.hostname or None,
        "manufacturer": client.manufacturer or None,
        "ssid": client.essid or None,
        "ip": client.ip or None,
        "network": client.network or None,
        "wired": client.wired,
        "random_mac": client.random_mac,
        "time": dt_util.utcnow().isoformat(),
    }


class NewDeviceEvent(UdrEntity, EventEntity):
    """Fires once per MAC, the first time the client shows up (WiFi only unless the option includes LAN)."""

    def __init__(self, coordinator: UdrCoordinator, *, wired: bool) -> None:
        super().__init__(coordinator, EVENT_NEW_DEVICE)
        self._attr_event_types = [EVENT_NEW_DEVICE]
        self._wired = wired
        # Listeners are also called without a new poll (pictures, speed test): handle each poll once
        self._handled: UdrData | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Devices that joined while HA was stopped show up in the first poll, before this entity existed
        if self.coordinator.data.new_clients:
            self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        if data is not self._handled:
            self._handled = data
            for client in data.new_clients:
                if client.wired and not self._wired:
                    continue
                self._trigger_event(EVENT_NEW_DEVICE, event_attributes(client))
                # One state per event, so automations see every device of the same poll
                self.async_write_ha_state()
        super()._handle_coordinator_update()
