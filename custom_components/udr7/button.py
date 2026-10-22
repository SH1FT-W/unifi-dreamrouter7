"""Knöpfe: Speedtest, Neustart je Gerät, PoE-Port neu starten, Client neu verbinden."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Device, Port
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import ClientEntity, DeviceEntity, UdrEntity, device_keys, track_new_items, tracked_clients

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = [SpeedtestButton(coordinator)]
    entities.extend(ReconnectButton(coordinator, mac) for mac in tracked_clients(entry))
    async_add_entities(entities)

    def _device_entities(macs: set[str]) -> Iterable[Entity]:
        for mac in macs:
            device = coordinator.data.devices[mac]
            yield RestartButton(coordinator, device)
            for port in device.ports.values():
                if port.poe_capable:
                    yield PowerCycleButton(coordinator, device, port)

    entry.async_on_unload(track_new_items(coordinator, device_keys, _device_entities, async_add_entities))


class SpeedtestButton(UdrEntity, ButtonEntity):
    def __init__(self, coordinator: UdrCoordinator) -> None:
        super().__init__(coordinator, "speedtest")

    async def async_press(self) -> None:
        await self.coordinator.async_start_speedtest()


class RestartButton(DeviceEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "restart")

    async def async_press(self) -> None:
        await self.coordinator.async_command(lambda: self.coordinator.client.restart_device(self._mac))


class PowerCycleButton(DeviceEntity, ButtonEntity):
    """PoE eines Ports kurz aus- und wieder einschalten (startet das angeschlossene Gerät neu)."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator, device: Device, port: Port) -> None:
        super().__init__(coordinator, device, f"port_{port.idx}_power_cycle", "port_power_cycle", {"port": port.name})
        self._idx = port.idx

    async def async_press(self) -> None:
        await self.coordinator.async_command(lambda: self.coordinator.client.power_cycle_port(self._mac, self._idx))


class ReconnectButton(ClientEntity, ButtonEntity):
    """WLAN-Client trennen; er verbindet sich gleich wieder (hilft bei hängenden Geräten)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "client_reconnect")

    @property
    def available(self) -> bool:
        client = self.coordinator.data.clients.get(self._mac)
        return super().available and client is not None and not client.wired

    async def async_press(self) -> None:
        await self.coordinator.async_command(lambda: self.coordinator.client.reconnect_client(self._mac))
