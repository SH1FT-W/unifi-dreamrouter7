"""Gemeinsame Basis: der Dream Router ist das Hauptgerät, Access Points und Switches hängen als Untergeräte darunter
(je MAC), Clients bekommen ein eigenes Gerät über ihre MAC (verschmilzt mit Geräten anderer Integrationen)."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Client, Device, Rule, UdrData
from .const import CONF_TRACKED_CLIENTS, DOMAIN, LEGACY_ENTITY_IDS, MANUFACTURER
from .coordinator import UdrConfigEntry, UdrCoordinator


def entry_key(entry: UdrConfigEntry) -> str:
    """Eindeutiger Schlüssel des Eintrags: MAC des Gateways, sonst die Eintrags-ID."""
    return entry.unique_id or entry.entry_id


def hub_identifier(coordinator: UdrCoordinator) -> tuple[str, str]:
    return (DOMAIN, entry_key(coordinator.config_entry))


def device_info(coordinator: UdrCoordinator, device: Device) -> DeviceInfo:
    """Gerät in HA: das Gateway ist der Hub, alle anderen hängen per via_device darunter."""
    hub = device.is_gateway
    info = DeviceInfo(
        identifiers={hub_identifier(coordinator) if hub else (DOMAIN, device.mac)},
        connections={(dr.CONNECTION_NETWORK_MAC, device.mac)},
        manufacturer=MANUFACTURER,
        name=device.name,
        model=device.shortname,
        model_id=device.model,
        serial_number=str(device.raw.get("serial") or "") or None,
        sw_version=device.version or None,
    )
    if hub:
        info["configuration_url"] = f"https://{coordinator.client.host}/network/{coordinator.client.site}/dashboard"
    elif coordinator.device_id:
        info["via_device_id"] = coordinator.device_id
    return info


def hub_device_info(coordinator: UdrCoordinator) -> DeviceInfo:
    gateway = coordinator.data.gateway
    if gateway is not None:
        return device_info(coordinator, gateway)
    return DeviceInfo(identifiers={hub_identifier(coordinator)}, manufacturer=MANUFACTURER)


class UdrEntity(CoordinatorEntity[UdrCoordinator]):
    """Gehört zum Dream Router selbst (Werte des ganzen Netzes)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UdrCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_key(coordinator.config_entry)}_{key}"
        if legacy := LEGACY_ENTITY_IDS.get(key):
            # Nur beim ersten Anlegen wirksam (und nur, wenn UniFi Network Plus die ID nicht mehr belegt)
            self.entity_id = legacy
        self._attr_device_info = hub_device_info(coordinator)


class DeviceEntity(CoordinatorEntity[UdrCoordinator]):
    """Gehört zu einem UniFi-Gerät (Gateway, Access Point, Switch); fehlt es, ist die Entität nicht verfügbar."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: UdrCoordinator,
        device: Device,
        key: str,
        translation_key: str | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        """`key` macht die unique_id eindeutig („port_3_speed“), `translation_key` benennt („port_speed“)."""
        super().__init__(coordinator)
        self._mac = device.mac
        self._attr_translation_key = translation_key or key
        if placeholders:
            self._attr_translation_placeholders = placeholders
        self._attr_unique_id = f"{device.mac}_{key}"
        self._attr_device_info = device_info(coordinator, device)

    @property
    def device(self) -> Device | None:
        return self.coordinator.data.devices.get(self._mac)

    @property
    def available(self) -> bool:
        device = self.device
        return super().available and device is not None and device.online


class RuleEntity(CoordinatorEntity[UdrCoordinator]):
    """Eine schaltbare Einstellung (Firewall-Regel, Richtlinie, Filter …) am Dream Router."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UdrCoordinator, rule: Rule) -> None:
        super().__init__(coordinator)
        self._kind = rule.kind
        self._id = rule.id
        self._attr_translation_key = rule.kind
        self._attr_translation_placeholders = {"name": rule.name}
        self._attr_unique_id = f"{entry_key(coordinator.config_entry)}_{rule.kind}_{rule.id}"
        self._attr_device_info = hub_device_info(coordinator)

    @property
    def rule(self) -> Rule | None:
        return self.coordinator.data.rules.get(self._kind, {}).get(self._id)

    @property
    def available(self) -> bool:
        return super().available and self.rule is not None


class ClientEntity(CoordinatorEntity[UdrCoordinator]):
    """Gehört zu einem ausgewählten Client (Optionen); dessen Gerät hängt über die MAC an."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UdrCoordinator, mac: str, key: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_translation_key = key
        self._attr_unique_id = f"client_{mac}_{key}"
        client = coordinator.data.clients.get(mac) or coordinator.data.known_clients.get(mac)
        info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, mac)},
            name=client.display_name if client else mac,
        )
        if coordinator.device_id:
            info["via_device_id"] = coordinator.device_id
        self._attr_device_info = info

    @property
    def client(self) -> Client | None:
        """Verbundener Client, sonst der bekannte Eintrag (Name, Sperre)."""
        data = self.coordinator.data
        return data.clients.get(self._mac) or data.known_clients.get(self._mac)


def tracked_clients(entry: UdrConfigEntry) -> list[str]:
    return [str(mac).lower() for mac in entry.options.get(CONF_TRACKED_CLIENTS, [])]


def device_keys(data: UdrData) -> Iterable[str]:
    return data.devices.keys()


def rule_keys(kind: str) -> Callable[[UdrData], Iterable[str]]:
    return lambda data: data.rules.get(kind, {}).keys()


def track_new_items(
    coordinator: UdrCoordinator,
    keys: Callable[[UdrData], Iterable[str]],
    create: Callable[[set[str]], Iterable[Entity]],
    add: Callable[[list[Entity]], None],
) -> Callable[[], None]:
    """Entitäten für neu auftauchende Geräte, WLANs und Regeln nachlegen (auch zur Laufzeit)."""
    known: set[str] = set()

    @callback
    def _check() -> None:
        new = set(keys(coordinator.data)) - known
        if new:
            known.update(new)
            if entities := list(create(new)):
                add(entities)

    _check()
    return coordinator.async_add_listener(_check)
