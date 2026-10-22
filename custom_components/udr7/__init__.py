"""UniFi Dream Router 7: Gateway, WLAN, Firewall, Clients und angeschlossene UniFi-Geräte lokal steuern."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.ssl import get_default_no_verify_context

from .api import UdrClient, UdrError
from .const import CONF_FINGERPRINT, CONF_SITE, DEFAULT_SITE, DOMAIN
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import client_device_info, hub_device_info, hub_identifier, tracked_clients
from .services import async_setup_services

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.IMAGE,
    Platform.LIGHT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.TIME,
    Platform.UPDATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def create_client(hass: HomeAssistant, data: Mapping[str, Any], *, verify_ssl: bool = False) -> UdrClient:
    # Selbstsigniertes Zertifikat von UniFi OS: standardmäßig HAs Session ohne CA-Prüfung, dafür nur das gemerkte
    # Zertifikat (Fingerabdruck in entry.data); mit der Option „Zertifikat prüfen“ die normale Session mit Prüfung
    return UdrClient(
        async_get_clientsession(hass, verify_ssl=verify_ssl),
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data.get(CONF_SITE, DEFAULT_SITE),
        verify_ssl=verify_ssl,
        fingerprint=data.get(CONF_FINGERPRINT),
    )


async def async_read_fingerprint(hass: HomeAssistant, host: str) -> str:
    """Fingerabdruck des Zertifikats, das der Router gerade zeigt (zum Merken bei Einrichtung oder Reparatur)."""
    client = UdrClient(async_get_clientsession(hass, verify_ssl=False), host, "", "")
    return await client.certificate_fingerprint(get_default_no_verify_context())


def verify_ssl_option(entry: UdrConfigEntry) -> bool:
    return bool(entry.options.get(CONF_VERIFY_SSL, False))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UdrConfigEntry) -> bool:
    verify_ssl = verify_ssl_option(entry)
    if not verify_ssl and not entry.data.get(CONF_FINGERPRINT):
        try:
            fingerprint = await async_read_fingerprint(hass, entry.data[CONF_HOST])
        except UdrError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="unreachable", translation_placeholders={"error": str(err)}
            ) from err
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_FINGERPRINT: fingerprint})
    client = create_client(hass, entry.data, verify_ssl=verify_ssl)
    coordinator = UdrCoordinator(hass, entry, client)
    await coordinator.async_load_storage()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(config_entry_id=entry.entry_id, **hub_device_info(coordinator))
    coordinator.device_id = device.id
    # Client-Geräte vor den Plattformen: HA schaltet einen device_tracker ab, wenn sein Gerät (MAC) noch fehlt
    for mac in tracked_clients(entry):
        registry.async_get_or_create(config_entry_id=entry.entry_id, **client_device_info(coordinator, mac))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Produktbilder kommen aus dem Internet; der Start wartet nicht darauf
    entry.async_create_background_task(hass, coordinator.async_load_pictures(), "udr7 device pictures")
    # Geänderte Optionen (Clients, Speedtest, Zertifikat) gelten nach dem Neuladen
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: UdrConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: UdrConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: UdrConfigEntry, device: dr.DeviceEntry) -> bool:
    """Abgebaute UniFi-Geräte und nicht mehr ausgewählte Clients dürfen weg, der Router selbst nicht."""
    data = entry.runtime_data.data
    if hub_identifier(entry.runtime_data) in device.identifiers:
        return False
    if any(domain == DOMAIN and ident in data.devices for domain, ident in device.identifiers):
        return False
    tracked = set(tracked_clients(entry))
    return not any(kind == dr.CONNECTION_NETWORK_MAC and mac in tracked for kind, mac in device.connections)
