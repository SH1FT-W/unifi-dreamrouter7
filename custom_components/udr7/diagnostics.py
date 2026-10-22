"""Diagnose-Download: alle abgefragten Werte, ohne Adresse, Zugang, MACs, IPs, Seriennummern und Schlüssel."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_FINGERPRINT, CONF_TRACKED_CLIENTS
from .coordinator import UdrConfigEntry

TO_REDACT = {
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_FINGERPRINT,
    CONF_TRACKED_CLIENTS,
    "mac",
    "gw_mac",
    "ip",
    "serial",
    "hostname",
    "name",
    "essid",
    "x_passphrase",
    "passphrase",
    "wan_ip",
    "ipv6",
    "address",
    "latitude",
    "longitude",
    "city",
    "raw",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: UdrConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    err = coordinator.last_exception
    error = str(err).replace(coordinator.client.host, "**REDACTED**") if err else None
    data = coordinator.data
    summary: dict[str, Any] | None = None
    if data is not None:
        summary = {
            "health": async_redact_data(data.health, TO_REDACT),
            "devices": [
                async_redact_data(asdict(d), TO_REDACT) | {"model": d.model, "type": d.type}
                for d in data.devices.values()
            ],
            "clients": len(data.clients),
            "known_clients": len(data.known_clients),
            "wlans": len(data.wlans),
            "rules": {kind: len(rules) for kind, rules in data.rules.items()},
            "gateway_settings": sorted(data.gateway_settings),
            "critical_events": len(data.critical_events) if data.critical_events is not None else None,
            "network_version": data.sysinfo.get("version"),
            "network_app": async_redact_data(data.network_app, TO_REDACT),
        }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "last_error": None if coordinator.last_update_success else error,
        "data": summary,
    }
