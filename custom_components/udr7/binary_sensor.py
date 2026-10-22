"""Binärsensoren: Internet, WAN, WLAN-/LAN-Problem, Speedtest läuft, je Gerät online und Überhitzung."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Device, UdrData, as_dict
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import DeviceEntity, UdrEntity, device_keys, track_new_items

PARALLEL_UPDATES = 0


def _status_ok(subsystem: str) -> Callable[[UdrData], bool | None]:
    def value(d: UdrData) -> bool | None:
        status = d.get(subsystem, "status")
        return status == "ok" if status else None

    return value


def _problem(subsystem: str) -> Callable[[UdrData], bool | None]:
    ok = _status_ok(subsystem)
    # Problem = an, deshalb umgedreht
    return lambda d: None if (value := ok(d)) is None else not value


def _wan_up(d: UdrData) -> bool | None:
    """Aktiver WAN-Anschluss online (last_wan_interfaces.alive, sonst uplink.up)."""
    gateway = d.gateway_raw
    wan = as_dict(as_dict(gateway.get("last_wan_interfaces")).get("WAN"))
    if "alive" in wan:
        return bool(wan["alive"])
    uplink = as_dict(gateway.get("uplink"))
    return bool(uplink["up"]) if "up" in uplink else None


@dataclass(frozen=True, kw_only=True)
class UdrBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[UdrData], bool | None]
    attrs_fn: Callable[[UdrData], dict[str, Any]] | None = None


NETWORK_BINARY_SENSORS: tuple[UdrBinaryDescription, ...] = (
    UdrBinaryDescription(
        key="internet",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=_status_ok("www"),
        attrs_fn=lambda d: {"status": d.get("www", "status")},
    ),
    UdrBinaryDescription(
        key="wlan_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_problem("wlan"),
        attrs_fn=lambda d: {"status": d.get("wlan", "status"), "access_points": d.get("wlan", "num_ap")},
    ),
    UdrBinaryDescription(
        key="lan_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_problem("lan"),
        attrs_fn=lambda d: {"status": d.get("lan", "status"), "switches": d.get("lan", "num_sw")},
    ),
    UdrBinaryDescription(
        key="wan_up",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_wan_up,
        attrs_fn=lambda d: {"wan": w[0] if (w := d.active_wan()) else None},
    ),
    UdrBinaryDescription(
        key="speedtest_running",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.speedtest_running,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(NetworkBinarySensor(coordinator, d) for d in NETWORK_BINARY_SENSORS)

    def _device_entities(macs: set[str]) -> Iterable[Entity]:
        for mac in macs:
            device = coordinator.data.devices[mac]
            yield OnlineSensor(coordinator, device)
            if "overheating" in device.raw:
                yield OverheatingSensor(coordinator, device)

    entry.async_on_unload(track_new_items(coordinator, device_keys, _device_entities, async_add_entities))


class NetworkBinarySensor(UdrEntity, BinarySensorEntity):
    entity_description: UdrBinaryDescription

    def __init__(self, coordinator: UdrCoordinator, description: UdrBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None


class OnlineSensor(DeviceEntity, BinarySensorEntity):
    """Gerät mit der Network-App verbunden; „Zustand“ (Update, Neustart …) als Attribut."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UdrCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "online")

    @property
    def available(self) -> bool:
        # Bleibt verfügbar, wenn das Gerät getrennt ist: genau das soll er zeigen
        return self.coordinator.last_update_success and self.device is not None

    @property
    def entity_picture(self) -> str | None:
        return self.coordinator.picture(self.device)

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.online if device else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        return {"state": device.state, "ip": device.ip} if device else None


class OverheatingSensor(DeviceEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UdrCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "overheating")

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.overheating if device else None
