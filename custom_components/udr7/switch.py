"""Schalter: WLANs, Firewall-Regeln, Richtlinien, Filter, VPN, Ports, PoE, UPnP, Gerät suchen, Client-Sperre und
automatischer Speedtest."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .api import RULE_KINDS, Device, Port, Wlan
from .const import CONF_SPEEDTEST_MINUTES, DEFAULT_SPEEDTEST_MINUTES, DOMAIN
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import (
    ClientEntity,
    DeviceEntity,
    RuleEntity,
    UdrEntity,
    device_keys,
    rule_keys,
    track_new_items,
    tracked_clients,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


def switchable_port(device: Device, port: Port) -> bool:
    """Ports, die man abschalten darf: nicht der Uplink zum Router und kein WAN-Port (sonst ist das Netz weg)."""
    return not port.is_uplink and port.network != "wan" and not (device.is_gateway and port.media == "SFP+")


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [AutoSpeedtestSwitch(coordinator)]
    if "upnp_enabled" in coordinator.data.gateway_settings:
        entities.append(UpnpSwitch(coordinator))
    entities.extend(ClientBlockSwitch(coordinator, mac) for mac in tracked_clients(entry))
    async_add_entities(entities)

    entry.async_on_unload(
        track_new_items(
            coordinator,
            lambda d: d.wlans.keys(),
            lambda ids: (WlanSwitch(coordinator, coordinator.data.wlans[i]) for i in ids),
            async_add_entities,
        )
    )

    def _rule_entities(kind: str) -> Callable[[set[str]], Iterable[Entity]]:
        return lambda ids: (RuleSwitch(coordinator, coordinator.data.rules[kind][i]) for i in ids)

    for kind in RULE_KINDS:
        entry.async_on_unload(track_new_items(coordinator, rule_keys(kind), _rule_entities(kind), async_add_entities))

    def _device_entities(macs: set[str]) -> Iterable[Entity]:
        for mac in macs:
            device = coordinator.data.devices[mac]
            yield LocateSwitch(coordinator, device)
            for port in device.ports.values():
                if switchable_port(device, port):
                    yield PortSwitch(coordinator, device, port)
                if port.poe_capable:
                    yield PoeSwitch(coordinator, device, port)

    entry.async_on_unload(track_new_items(coordinator, device_keys, _device_entities, async_add_entities))


class WlanSwitch(UdrEntity, SwitchEntity):
    """WLAN ein/aus (z. B. Gäste-WLAN nur bei Besuch)."""

    def __init__(self, coordinator: UdrCoordinator, wlan: Wlan) -> None:
        super().__init__(coordinator, f"wlan_{wlan.id}")
        self._attr_translation_key = "wlan"
        self._attr_translation_placeholders = {"name": wlan.name}
        self._id = wlan.id

    @property
    def wlan(self) -> Wlan | None:
        return self.coordinator.data.wlans.get(self._id)

    @property
    def available(self) -> bool:
        return super().available and self.wlan is not None

    @property
    def is_on(self) -> bool | None:
        wlan = self.wlan
        return wlan.enabled if wlan else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        wlan = self.wlan
        if wlan is None:
            return None
        return {"ssid": wlan.name, "security": wlan.security, "hidden": wlan.hidden, "guest": wlan.guest}

    async def _set(self, on: bool) -> None:
        wlan = self.wlan
        if wlan is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        await self.coordinator.async_command(lambda: self.coordinator.client.set_wlan(wlan, enabled=on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class RuleSwitch(RuleEntity, SwitchEntity):
    """Firewall-Regel, Traffic-Regel, Route, Portweiterleitung, Inhaltsfilter, Richtlinie, VPN ein/aus."""

    @property
    def is_on(self) -> bool | None:
        rule = self.rule
        return rule.enabled if rule else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rule = self.rule
        if rule is None:
            return None
        raw = rule.raw
        attrs: dict[str, Any] = {}
        for key in ("action", "description", "protocol", "hits", "vpn_type", "fwd_port", "dst_port", "fwd"):
            if raw.get(key) not in (None, ""):
                attrs[key] = raw[key]
        return attrs or None

    async def _set(self, on: bool) -> None:
        rule = self.rule
        if rule is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        await self.coordinator.async_command(lambda: self.coordinator.client.set_enabled(rule, on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class UpnpSwitch(UdrEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator) -> None:
        super().__init__(coordinator, "upnp")

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.gateway_settings.get("upnp_enabled")
        return bool(value) if value is not None else None

    async def _set(self, on: bool) -> None:
        settings = self.coordinator.data.gateway_settings
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_gateway_setting(settings, upnp_enabled=on)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class LocateSwitch(DeviceEntity, SwitchEntity):
    """Gerät suchen: LED blinkt, bis wieder ausgeschaltet."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "locate")

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.locating if device else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(lambda: self.coordinator.client.locate(self._mac, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(lambda: self.coordinator.client.locate(self._mac, False))


class PortSwitch(DeviceEntity, SwitchEntity):
    """Port aktiv/deaktiviert (wie „Port deaktivieren“ in der Network-App)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator, device: Device, port: Port) -> None:
        super().__init__(coordinator, device, f"port_{port.idx}_enabled", "port_enabled", {"port": port.name})
        self._idx = port.idx

    @property
    def port(self) -> Port | None:
        device = self.device
        return device.ports.get(self._idx) if device else None

    @property
    def is_on(self) -> bool | None:
        port = self.port
        return port.enabled if port else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        port = self.port
        return {"link": port.up, "media": port.media} if port else None

    async def _set(self, on: bool) -> None:
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        await self.coordinator.async_set_port_enabled(device, self._idx, on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class PoeSwitch(DeviceEntity, SwitchEntity):
    """PoE eines Ports: an = auto, aus = off."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator, device: Device, port: Port) -> None:
        super().__init__(coordinator, device, f"port_{port.idx}_poe", "port_poe", {"port": port.name})
        self._idx = port.idx

    @property
    def is_on(self) -> bool | None:
        device = self.device
        port = device.ports.get(self._idx) if device else None
        if port is None or port.poe_mode is None:
            return None
        return port.poe_mode != "off"

    async def _set(self, mode: str) -> None:
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_missing")
        await self.coordinator.async_command(lambda: self.coordinator.client.set_port(device, self._idx, poe_mode=mode))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set("auto")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set("off")


class ClientBlockSwitch(ClientEntity, SwitchEntity):
    """Internetzugang eines Clients: an = darf ins Netz, aus = gesperrt (Sperre in der Network-App)."""

    def __init__(self, coordinator: UdrCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "client_allowed")

    @property
    def available(self) -> bool:
        return super().available and self.client is not None

    @property
    def is_on(self) -> bool | None:
        client = self.client
        return not client.blocked if client else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(lambda: self.coordinator.client.block_client(self._mac, False))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(lambda: self.coordinator.client.block_client(self._mac, True))


class AutoSpeedtestSwitch(UdrEntity, SwitchEntity, RestoreEntity):
    """Automatischer Speedtest im eingestellten Abstand (Optionen, Standard stündlich)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UdrCoordinator) -> None:
        super().__init__(coordinator, "auto_speedtest")
        minutes = coordinator.config_entry.options.get(CONF_SPEEDTEST_MINUTES, DEFAULT_SPEEDTEST_MINUTES)
        self._interval = timedelta(minutes=int(minutes))
        # Standard an, wie in UniFi Network Plus (das diese Integration ablöst)
        self._attr_is_on = True
        self._cancel: CALLBACK_TYPE | None = None

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        return {"interval_minutes": int(self._interval.total_seconds() // 60)}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state != STATE_OFF
        self._schedule()
        self.async_on_remove(self._unschedule)

    @callback
    def _schedule(self) -> None:
        self._unschedule()
        if self._attr_is_on:
            self._cancel = async_track_time_interval(self.hass, self._run, self._interval)

    @callback
    def _unschedule(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    async def _run(self, _now: datetime) -> None:
        if self.coordinator.data is not None and self.coordinator.data.speedtest_running:
            return
        try:
            await self.coordinator.async_start_speedtest()
        except HomeAssistantError as err:
            _LOGGER.warning("Automatic speed test not started: %s", err)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._schedule()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._unschedule()
        self.async_write_ha_state()
