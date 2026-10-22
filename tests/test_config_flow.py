"""Einrichtung, Neu anmelden, Neu konfigurieren, Optionen."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.udr7.api import UdrError
from custom_components.udr7.const import DOMAIN

from .conftest import API, BASE, ENTRY_DATA, FINGERPRINT, GW, HOST, PHONE, fixture, mock_router


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return str(result["flow_id"])


async def test_user_flow(hass: HomeAssistant, router: AiohttpClientMocker) -> None:
    result = await hass.config_entries.flow.async_configure(await _start(hass), ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Dream Router 7"
    assert result["data"] == {**ENTRY_DATA, "fingerprint": FINGERPRINT}
    assert result["options"] == {"verify_ssl": False}
    assert result["result"].unique_id == GW


@pytest.mark.parametrize(("login_status", "error"), [(403, "invalid_auth"), (500, "cannot_connect")])
async def test_user_flow_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, login_status: int, error: str
) -> None:
    mock_router(aioclient_mock, login_status=login_status)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, ENTRY_DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    mock_router(aioclient_mock)  # danach klappt es
    result = await hass.config_entries.flow.async_configure(flow_id, ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("devices", "error"),
    [
        (403, "no_permission"),
        ({"meta": {"rc": "error", "msg": "api.err.NoSiteContext"}, "data": []}, "unknown_site"),
        ({"meta": {"rc": "ok"}, "data": []}, "no_gateway"),
    ],
)
async def test_user_flow_device_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, devices: Any, error: str
) -> None:
    mock_router(aioclient_mock, devices=devices)
    result = await hass.config_entries.flow.async_configure(await _start(hass), ENTRY_DATA)
    assert result["errors"] == {"base": error}


async def test_already_configured_updates_host(hass: HomeAssistant, router: AiohttpClientMocker) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=GW, data={**ENTRY_DATA, "host": "192.0.2.99"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(await _start(hass), ENTRY_DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == HOST


async def test_user_flow_certificate_unreadable(
    hass: HomeAssistant, router: AiohttpClientMocker, read_fingerprint: AsyncMock
) -> None:
    read_fingerprint.side_effect = UdrError("weg")
    result = await hass.config_entries.flow.async_configure(await _start(hass), ENTRY_DATA)
    assert result["errors"] == {"base": "cannot_connect"}
    # Zugangsdaten gingen nirgendwohin
    assert not [c for c in router.mock_calls if str(c[1]).endswith("/api/auth/login")]


async def test_user_flow_verify_ssl(hass: HomeAssistant, router: AiohttpClientMocker) -> None:
    with patch("custom_components.udr7.async_get_clientsession", wraps=async_get_clientsession) as session:
        result = await hass.config_entries.flow.async_configure(await _start(hass), {**ENTRY_DATA, "verify_ssl": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA
    assert result["options"] == {"verify_ssl": True}
    assert session.call_args.kwargs == {"verify_ssl": True}


async def test_reauth(hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker) -> None:
    result = await setup.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mock_router(aioclient_mock, login_status=403)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "ha-user", "password": "falsch"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    mock_router(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "ha-user", "password": "neu"}
    )
    assert result["reason"] == "reauth_successful"
    assert setup.data["password"] == "neu"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(setup.entry_id)


async def test_reauth_certificate_changed(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    result = await setup.start_reauth_flow(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{BASE}/api/auth/login", exc=aiohttp.ServerFingerprintMismatch(b"\xab" * 32, b"\xcd" * 32, HOST, 443)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "ha-user", "password": "neu"}
    )
    assert result["errors"] == {"base": "cert_changed"}
    await hass.config_entries.async_unload(setup.entry_id)


async def test_reconfigure(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker, read_fingerprint: AsyncMock
) -> None:
    result = await setup.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    # Unter der Adresse antwortet ein anderer Router
    other = fixture("devices")
    other["data"][0]["mac"] = "11:22:33:44:55:66"
    mock_router(aioclient_mock, devices=other)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["reason"] == "wrong_device"

    result = await setup.start_reconfigure_flow(hass)
    mock_router(aioclient_mock, login_status=500)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "cannot_connect"}
    mock_router(aioclient_mock)
    read_fingerprint.return_value = "cd" * 32
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**ENTRY_DATA, "password": "neu"})
    assert result["reason"] == "reconfigure_successful"
    assert setup.data["fingerprint"] == "cd" * 32
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(setup.entry_id)


async def test_options(hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker) -> None:
    result = await hass.config_entries.options.async_init(setup.entry_id)
    assert result["type"] is FlowResultType.FORM
    # Auswahl: Clients mit Namen, auch nicht verbundene; der ausgewählte TV ohne Namen bleibt wählbar
    schema = result["data_schema"].schema
    selector = next(v for k, v in schema.items() if k == "tracked_clients")
    options = {o["value"]: o["label"] for o in selector.config["options"]}
    assert options[PHONE] == f"Example Phone ({PHONE})"
    assert options["aa:bb:cc:10:00:03"] == "Old Tablet (aa:bb:cc:10:00:03)"
    assert "aa:bb:cc:10:00:02" in options

    user_input = {
        "tracked_clients": [PHONE],
        "away_seconds": 120,
        "speedtest_minutes": 30,
        "expected_download": 200,
        "expected_upload": 50,
        "tolerance": 10,
        "verify_ssl": False,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dream_router_7_download_vs_contract").state == "faster"
    assert hass.states.get("sensor.dream_router_7_upload_vs_contract").state == "slower"
    auto = hass.states.get("switch.unifi_wan_auto_speedtest")
    assert auto is not None and auto.attributes["interval_minutes"] == 30
    # TV nicht mehr ausgewählt: seine Entitäten verschwinden nach dem Neuladen nicht von selbst, sind aber weg
    assert hass.states.get("device_tracker.tv") is None or hass.states.get("device_tracker.tv").state == "unavailable"


async def test_options_verify_ssl_checked(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    result = await hass.config_entries.options.async_init(setup.entry_id)
    mock_router(aioclient_mock, login_status=500)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"away_seconds": 300, "speedtest_minutes": 60, "tolerance": 10, "verify_ssl": True},
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert API  # gleiche Basis wie in den anderen Tests
