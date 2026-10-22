"""Erkennung per DHCP/ARP (Ubiquiti-MAC, Hostname „unifi“), Bestätigung über /api/system, Zugangsdaten."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_DHCP
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.udr7.const import DOMAIN

from .conftest import BASE, ENTRY_DATA, FINGERPRINT, GW, HOST, mock_router

SYSTEM = {"name": "Dream Router 7", "mac": "AABBCC000001", "hardware": {"shortname": "UDR7"}}
DHCP = DhcpServiceInfo(ip=HOST, hostname="unifi", macaddress="aabbcc000009")


async def _discover(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP)
    return dict(result)


async def test_dhcp_flow(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/api/system", json=SYSTEM)
    result = await _discover(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"name": "Dream Router 7", "host": HOST}
    mock_router(aioclient_mock, login_status=403)
    result = dict(
        await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "ha-user", "password": "falsch"})
    )
    assert result["errors"] == {"base": "invalid_auth"}
    mock_router(aioclient_mock)
    result = dict(
        await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "ha-user", "password": "geheim"})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**ENTRY_DATA, "fingerprint": FINGERPRINT}
    assert result["result"].unique_id == GW


@pytest.mark.parametrize(
    "response",
    [
        {"json": {**SYSTEM, "hardware": {"shortname": "UNAS2W"}}},
        {"json": {**SYSTEM, "mac": ""}},
        {"status": 404},
        {"text": "<html/>"},
        {"exc": TimeoutError()},
    ],
)
async def test_dhcp_not_a_dream_router(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, response: dict[str, Any]
) -> None:
    aioclient_mock.get(f"{BASE}/api/system", **response)
    result = await _discover(hass)
    assert result["reason"] == "not_udr"


async def test_dhcp_known_router_keeps_host(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Der Router hat eine Adresse je Netz: die eingerichtete bleibt."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=GW, data={**ENTRY_DATA, "host": "192.0.2.99"})
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{BASE}/api/system", json=SYSTEM)
    result = await _discover(hass)
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.0.2.99"


async def test_dhcp_other_router_at_address(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Gefunden wurde ein Router, unter der Adresse antwortet beim Anmelden aber ein anderer."""
    aioclient_mock.get(f"{BASE}/api/system", json={**SYSTEM, "mac": "112233445566"})
    result = await _discover(hass)
    mock_router(aioclient_mock)
    result = dict(
        await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "ha-user", "password": "geheim"})
    )
    assert result["errors"] == {"base": "wrong_device"}
