"""Aktionen für beliebige Clients per MAC (nicht nur die in den Optionen ausgewählten): sperren, freigeben,
neu verbinden, Gast freischalten."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import format_mac
import voluptuous as vol

from .api import UdrClient
from .const import DOMAIN
from .coordinator import UdrConfigEntry

ATTR_CONFIG_ENTRY = "config_entry_id"
ATTR_MAC = "mac"
ATTR_MINUTES = "minutes"

_BASE = {vol.Optional(ATTR_CONFIG_ENTRY): cv.string, vol.Required(ATTR_MAC): cv.string}
SCHEMA = vol.Schema(_BASE)
GUEST_SCHEMA = vol.Schema({**_BASE, vol.Optional(ATTR_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=1))})


def _entry(hass: HomeAssistant, call: ServiceCall) -> UdrConfigEntry:
    """Gewählter Eintrag; ohne Angabe der einzige geladene."""
    entries: list[UdrConfigEntry] = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY):
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_found")
    return entries[0]


def _mac(call: ServiceCall) -> str:
    mac = format_mac(str(call.data[ATTR_MAC]).strip())
    if len(mac) != 17:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_mac")
    return mac


def _handler(
    hass: HomeAssistant, action: Callable[[UdrClient, ServiceCall, str], Awaitable[None]]
) -> Callable[[ServiceCall], Coroutine[Any, Any, None]]:
    async def handle(call: ServiceCall) -> None:
        coordinator = _entry(hass, call).runtime_data
        mac = _mac(call)
        await coordinator.async_command(lambda: action(coordinator.client, call, mac))

    return handle


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    for name, schema, action in (
        ("block_client", SCHEMA, lambda c, _call, mac: c.block_client(mac, True)),
        ("unblock_client", SCHEMA, lambda c, _call, mac: c.block_client(mac, False)),
        ("reconnect_client", SCHEMA, lambda c, _call, mac: c.reconnect_client(mac)),
        ("authorize_guest", GUEST_SCHEMA, lambda c, call, mac: c.authorize_guest(mac, call.data.get(ATTR_MINUTES))),
        ("unauthorize_guest", SCHEMA, lambda c, _call, mac: c.unauthorize_guest(mac)),
    ):
        hass.services.async_register(DOMAIN, name, _handler(hass, action), schema=schema)
