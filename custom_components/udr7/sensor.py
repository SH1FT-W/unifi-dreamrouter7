"""Sensoren: Netz (Durchsatz, Clients, Internet, Speedtest), je UniFi-Gerät, je Funkband, je Port, je Client."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfDataRate,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .api import Client, Device, Port, Radio, UdrData, as_dict, num
from .const import CONF_EXPECTED_DOWNLOAD, CONF_EXPECTED_UPLOAD, CONF_TOLERANCE, DEFAULT_TOLERANCE, RADIO_BANDS
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import ClientEntity, DeviceEntity, UdrEntity, device_keys, track_new_items, tracked_clients

PARALLEL_UPDATES = 0

# Anzeige der Funkbänder im Namen
BAND_LABELS = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}


def _rate(subsystem: str, key: str) -> Callable[[UdrData], StateType | datetime]:
    return lambda d: num(d.get(subsystem, key))


def _clients(*subsystems: str, key: str = "num_user") -> Callable[[UdrData], StateType | datetime]:
    def value(d: UdrData) -> int | None:
        counts = [d.get(s, key) for s in subsystems]
        numbers = [int(c) for c in counts if isinstance(c, int | float) and not isinstance(c, bool)]
        return sum(numbers) if numbers else None

    return value


def started(uptime: float | None) -> datetime | None:
    """Startzeitpunkt aus der Laufzeit; auf die Minute gerundet, sonst wandert er bei jeder Abfrage."""
    if uptime is None:
        return None
    return (dt_util.utcnow() - timedelta(seconds=uptime)).replace(second=0, microsecond=0)


def _wan_availability(d: UdrData) -> float | None:
    return num(as_dict(as_dict(d.get("wan", "uptime_stats")).get("WAN")).get("availability"))


def _speedtest_last(d: UdrData) -> datetime | None:
    value = num(d.get("www", "speedtest_lastrun"))
    return datetime.fromtimestamp(value, tz=UTC) if value else None


def _wan_ipv6(d: UdrData) -> str | None:
    """Erste globale IPv6 des aktiven WAN-Ports (Link-local fe80:: zählt nicht)."""
    wan = d.active_wan()
    for address in (wan[1].get("ipv6") if wan else None) or ():
        if isinstance(address, str) and not address.lower().startswith("fe80"):
            return address.split("/", 1)[0]
    return None


def _active_wan_attrs(d: UdrData) -> dict[str, Any]:
    wan = d.active_wan()
    if wan is None:
        return {}
    port = wan[1]
    return {
        "interface": port.get("ifname") or port.get("name"),
        "link_speed": port.get("speed"),
        "media": port.get("media"),
    }


def _speedtest_server(d: UdrData) -> dict[str, Any]:
    return as_dict(d.speedtest_status.get("server"))


def _speedtest_server_attrs(d: UdrData) -> dict[str, Any]:
    server = _speedtest_server(d)
    return {"city": server.get("city"), "country": server.get("country"), "url": server.get("provider_url")}


def _isp_attrs(d: UdrData) -> dict[str, Any]:
    geo = d.geo()
    return {
        "organization": geo.get("isp_organization") or d.get("wan", "isp_organization"),
        "asn": geo.get("asn") or d.get("wan", "asn"),
        "city": geo.get("city"),
        "country": geo.get("country_name"),
    }


def _latest_event(d: UdrData) -> dict[str, Any]:
    """Jüngstes kritisches Ereignis: Schlüssel, Bereich und Text, wie im Systemprotokoll."""
    events = d.critical_events or []
    latest = max(events, key=lambda e: num(e.get("timestamp")) or 0, default=None)
    if latest is None:
        return {}
    text = latest.get("message_raw") or latest.get("title_raw") or latest.get("message") or latest.get("title")
    return {"latest": text, "latest_key": latest.get("key"), "latest_category": latest.get("category")}


@dataclass(frozen=True, kw_only=True)
class UdrSensorDescription(SensorEntityDescription):
    value_fn: Callable[[UdrData], StateType | datetime]
    attrs_fn: Callable[[UdrData], dict[str, Any]] | None = None


def _throughput(key: str, subsystem: str, field: str, **kwargs: Any) -> UdrSensorDescription:
    """Bytes/s aus stat/health, angezeigt in Mbit/s."""
    return UdrSensorDescription(
        key=key,
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_rate(subsystem, field),
        **kwargs,
    )


def _speed(key: str, field: str) -> UdrSensorDescription:
    return UdrSensorDescription(
        key=key,
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_rate("www", field),
    )


def _count(key: str, fn: Callable[[UdrData], StateType | datetime], **kwargs: Any) -> UdrSensorDescription:
    return UdrSensorDescription(key=key, state_class=SensorStateClass.MEASUREMENT, value_fn=fn, **kwargs)


NETWORK_SENSORS: tuple[UdrSensorDescription, ...] = (
    # Aus Sicht der Access Points/Switches: rx = was Clients senden (Upload), tx = was sie empfangen (Download)
    _throughput("wlan_upload", "wlan", "rx_bytes-r"),
    _throughput("wlan_download", "wlan", "tx_bytes-r"),
    _throughput("lan_upload", "lan", "rx_bytes-r"),
    _throughput("lan_download", "lan", "tx_bytes-r"),
    # Am Gateway umgekehrt: rx = aus dem Internet (Download)
    _throughput("wan_download", "wan", "rx_bytes-r"),
    _throughput("wan_upload", "wan", "tx_bytes-r"),
    _count("clients", _clients("wlan", "lan")),
    _count("wlan_clients", _clients("wlan")),
    _count("lan_clients", _clients("lan")),
    _count("guest_clients", _clients("wlan", "lan", key="num_guest")),
    _count("vpn_clients", lambda d: num(d.get("vpn", "remote_user_num_active"))),
    UdrSensorDescription(
        key="internet_latency",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_rate("www", "latency"),
    ),
    UdrSensorDescription(
        key="internet_drops",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_rate("www", "drops"),
    ),
    UdrSensorDescription(
        key="wan_availability",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_wan_availability,
    ),
    _speed("speedtest_download", "xput_down"),
    _speed("speedtest_upload", "xput_up"),
    UdrSensorDescription(
        key="speedtest_ping",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_rate("www", "speedtest_ping"),
    ),
    UdrSensorDescription(key="speedtest_last", device_class=SensorDeviceClass.TIMESTAMP, value_fn=_speedtest_last),
    UdrSensorDescription(
        key="isp",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("wan", "isp_name") or d.geo().get("isp_name") or None,
        attrs_fn=_isp_attrs,
    ),
    UdrSensorDescription(
        key="wan_ip", entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda d: d.get("wan", "wan_ip") or None
    ),
    UdrSensorDescription(key="wan_ipv6", entity_category=EntityCategory.DIAGNOSTIC, value_fn=_wan_ipv6),
    UdrSensorDescription(
        key="active_wan",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: w[0] if (w := d.active_wan()) else None,
        attrs_fn=_active_wan_attrs,
    ),
    UdrSensorDescription(
        key="speedtest_server",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _speedtest_server(d).get("provider") or None,
        attrs_fn=_speedtest_server_attrs,
    ),
    UdrSensorDescription(
        key="critical_events",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.critical_events) if d.critical_events is not None else None,
        attrs_fn=_latest_event,
    ),
    UdrSensorDescription(
        key="network_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.sysinfo.get("version") or None,
    ),
)


RATING = ["as_expected", "faster", "slower"]


def rate(measured: float | None, expected: float, tolerance_percent: float) -> str | None:
    """Speedtest gegen die gebuchte Geschwindigkeit: innerhalb ± Toleranz = wie erwartet."""
    if measured is None or expected <= 0:
        return None
    band = expected * tolerance_percent / 100
    if measured < expected - band:
        return "slower"
    if measured > expected + band:
        return "faster"
    return "as_expected"


def _rating(field: str, expected: float, tolerance: float) -> Callable[[UdrData], StateType | datetime]:
    return lambda d: rate(num(d.get("www", field)), expected, tolerance)


def _rating_attrs(expected: float, tolerance: float) -> Callable[[UdrData], dict[str, Any]]:
    return lambda _d: {"expected": expected, "tolerance_percent": tolerance}


# --- je Gerät -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DeviceSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Device], StateType | datetime]
    exists_fn: Callable[[Device], bool] = lambda _d: True


DEVICE_SENSORS: tuple[DeviceSensorDescription, ...] = (
    DeviceSensorDescription(
        key="cpu_load",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.cpu,
        exists_fn=lambda d: d.cpu is not None,
    ),
    DeviceSensorDescription(
        key="memory_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.memory,
        exists_fn=lambda d: d.memory is not None,
    ),
    DeviceSensorDescription(
        key="cpu_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.temperature,
        exists_fn=lambda d: d.temperature is not None,
    ),
    DeviceSensorDescription(
        key="started",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: started(d.uptime),
    ),
    DeviceSensorDescription(
        key="connected_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.clients,
        exists_fn=lambda d: d.clients is not None,
    ),
    DeviceSensorDescription(
        key="poe_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.poe_power,
        exists_fn=lambda d: any(p.poe_capable for p in d.ports.values()),
    ),
)


@dataclass(frozen=True, kw_only=True)
class RadioSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Radio], StateType]


RADIO_SENSORS: tuple[RadioSensorDescription, ...] = (
    RadioSensorDescription(key="radio_clients", state_class=SensorStateClass.MEASUREMENT, value_fn=lambda r: r.clients),
    RadioSensorDescription(
        key="radio_channel",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: str(r.channel) if r.channel is not None else None,
    ),
    RadioSensorDescription(
        key="radio_utilization",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.utilization,
    ),
    RadioSensorDescription(
        key="radio_tx_power",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.tx_power,
    ),
)


@dataclass(frozen=True, kw_only=True)
class PortSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Port], StateType]
    exists_fn: Callable[[Port], bool] = lambda _p: True


def _port_rate(key: str, fn: Callable[[Port], float | None]) -> PortSensorDescription:
    return PortSensorDescription(
        key=key,
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=fn,
    )


PORT_SENSORS: tuple[PortSensorDescription, ...] = (
    PortSensorDescription(
        key="port_link_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.speed if p.up else 0,
    ),
    _port_rate("port_receive", lambda p: p.rx_rate),
    _port_rate("port_transmit", lambda p: p.tx_rate),
    PortSensorDescription(
        key="port_poe_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda p: p.poe_power,
        exists_fn=lambda p: p.poe_capable,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ClientSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Client], StateType | datetime]


CLIENT_SENSORS: tuple[ClientSensorDescription, ...] = (
    ClientSensorDescription(
        key="client_download",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        # Aus Sicht des Netzes gesendet = beim Client angekommen
        value_fn=lambda c: c.tx_rate,
    ),
    ClientSensorDescription(
        key="client_upload",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.rx_rate,
    ),
    ClientSensorDescription(
        key="client_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.signal,
    ),
    ClientSensorDescription(
        key="client_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.ip or None,
    ),
    ClientSensorDescription(
        key="client_connected_since",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: started(c.uptime),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [NetworkSensor(coordinator, description) for description in NETWORK_SENSORS]
    tolerance = float(entry.options.get(CONF_TOLERANCE, DEFAULT_TOLERANCE))
    for key, field, option in (
        ("download_rating", "xput_down", CONF_EXPECTED_DOWNLOAD),
        ("upload_rating", "xput_up", CONF_EXPECTED_UPLOAD),
    ):
        # Nur wenn in den Optionen eine gebuchte Geschwindigkeit eingetragen ist
        if (expected := float(entry.options.get(option) or 0)) > 0:
            entities.append(
                NetworkSensor(
                    coordinator,
                    UdrSensorDescription(
                        key=key,
                        device_class=SensorDeviceClass.ENUM,
                        options=RATING,
                        value_fn=_rating(field, expected, tolerance),
                        attrs_fn=_rating_attrs(expected, tolerance),
                    ),
                )
            )
    entities.extend(
        ClientSensor(coordinator, mac, description) for mac in tracked_clients(entry) for description in CLIENT_SENSORS
    )
    async_add_entities(entities)

    def _device_entities(macs: set[str]) -> Iterable[Entity]:
        for mac in macs:
            device = coordinator.data.devices[mac]
            for description in DEVICE_SENSORS:
                if description.exists_fn(device):
                    yield DeviceSensor(coordinator, device, description)
            for band in device.radios:
                for radio_description in RADIO_SENSORS:
                    yield RadioSensor(coordinator, device, band, radio_description)
            for port in device.ports.values():
                for port_description in PORT_SENSORS:
                    if port_description.exists_fn(port):
                        yield PortSensor(coordinator, device, port, port_description)

    entry.async_on_unload(track_new_items(coordinator, device_keys, _device_entities, async_add_entities))


class NetworkSensor(UdrEntity, SensorEntity):
    entity_description: UdrSensorDescription

    def __init__(self, coordinator: UdrCoordinator, description: UdrSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None


class DeviceSensor(DeviceEntity, SensorEntity):
    entity_description: DeviceSensorDescription

    def __init__(self, coordinator: UdrCoordinator, device: Device, description: DeviceSensorDescription) -> None:
        super().__init__(coordinator, device, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        device = self.device
        return self.entity_description.value_fn(device) if device else None


class RadioSensor(DeviceEntity, SensorEntity):
    entity_description: RadioSensorDescription

    def __init__(
        self, coordinator: UdrCoordinator, device: Device, band: str, description: RadioSensorDescription
    ) -> None:
        super().__init__(
            coordinator,
            device,
            f"{description.key}_{RADIO_BANDS.get(band, band)}",
            description.key,
            {"band": BAND_LABELS.get(band, band)},
        )
        self.entity_description = description
        self._band = band

    @property
    def native_value(self) -> StateType:
        device = self.device
        radio = device.radios.get(self._band) if device else None
        return self.entity_description.value_fn(radio) if radio else None


class PortSensor(DeviceEntity, SensorEntity):
    entity_description: PortSensorDescription

    def __init__(
        self, coordinator: UdrCoordinator, device: Device, port: Port, description: PortSensorDescription
    ) -> None:
        super().__init__(
            coordinator,
            device,
            f"{description.key.replace('port_', f'port_{port.idx}_', 1)}",
            description.key,
            {"port": port.name},
        )
        self.entity_description = description
        self._idx = port.idx

    @property
    def native_value(self) -> StateType:
        device = self.device
        port = device.ports.get(self._idx) if device else None
        return self.entity_description.value_fn(port) if port else None


class ClientSensor(ClientEntity, SensorEntity):
    entity_description: ClientSensorDescription

    def __init__(self, coordinator: UdrCoordinator, mac: str, description: ClientSensorDescription) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        # Werte gibt es nur, solange der Client verbunden ist
        return super().available and self._mac in self.coordinator.data.clients

    @property
    def native_value(self) -> StateType | datetime:
        client = self.coordinator.data.clients.get(self._mac)
        return self.entity_description.value_fn(client) if client else None
