"""Zertifikat-Pinning mit Reparatur, Ausfälle, Teilabfragen, Speedtest, neue Geräte, Diagnose."""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
import hashlib
import json
import ssl
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.repairs import DOMAIN as REPAIRS_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse
from yarl import URL

from custom_components.udr7.api import UdrClient, UdrError, format_fingerprint
from custom_components.udr7.const import DOMAIN
from custom_components.udr7.diagnostics import async_get_config_entry_diagnostics

from .conftest import API, BASE, ENTRY_DATA, FINGERPRINT, GW, HOST, SW, V2, calls, fixture, mock_router

NEW_FINGERPRINT = "cd" * 32
# Echte Methode, bevor conftest sie für alle anderen Tests ersetzt
READ_FINGERPRINT = UdrClient.certificate_fingerprint
LATENCY = "sensor.dream_router_7_internet_latency"


async def _tick(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, data: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Dream Router 7", unique_id=GW, data=data or ENTRY_DATA)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _mismatch() -> aiohttp.ServerFingerprintMismatch:
    return aiohttp.ServerFingerprintMismatch(bytes.fromhex(FINGERPRINT), bytes.fromhex(NEW_FINGERPRINT), HOST, 443)


# --- Zertifikat ---------------------------------------------------------------------------------------------------


async def test_entry_remembers_certificate(hass: HomeAssistant, router: AiohttpClientMocker) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["fingerprint"] == FINGERPRINT
    assert entry.runtime_data.client.fingerprint == FINGERPRINT


async def test_certificate_unreadable_at_setup_retries(
    hass: HomeAssistant, router: AiohttpClientMocker, read_fingerprint: AsyncMock
) -> None:
    read_fingerprint.side_effect = UdrError("weg")
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_certificate_changed_creates_repair_and_fix_accepts_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, read_fingerprint: AsyncMock
) -> None:
    mock_router(aioclient_mock)
    entry = await _setup(hass, {**ENTRY_DATA, "fingerprint": FINGERPRINT})
    issue_id = f"cert_changed_{entry.entry_id}"
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/stat/health", exc=_mismatch())
    mock_router(aioclient_mock, clear=False)
    await _tick(hass, entry)
    assert hass.states.get(LATENCY).state == STATE_UNAVAILABLE
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders == {
        "name": "Dream Router 7",
        "old": format_fingerprint(FINGERPRINT),
        "new": format_fingerprint(NEW_FINGERPRINT),
    }
    read_fingerprint.return_value = NEW_FINGERPRINT
    assert await async_setup_component(hass, REPAIRS_DOMAIN, {})
    from custom_components.udr7.repairs import async_create_fix_flow

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    flow.issue_id = issue_id
    result = await flow.async_step_init()
    assert result["description_placeholders"]["new"] == format_fingerprint(NEW_FINGERPRINT)
    mock_router(aioclient_mock)
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.data["fingerprint"] == NEW_FINGERPRINT
    # Wieder erreichbar: Reparatur verschwindet
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_certificate_ok_again_removes_repair(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_router(aioclient_mock)
    entry = await _setup(hass, {**ENTRY_DATA, "fingerprint": FINGERPRINT})
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/stat/health", exc=_mismatch())
    mock_router(aioclient_mock, clear=False)
    await _tick(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"cert_changed_{entry.entry_id}") is not None
    mock_router(aioclient_mock)
    await _tick(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"cert_changed_{entry.entry_id}") is None


@pytest.mark.parametrize(("remove_entry", "unreachable"), [(True, False), (False, True)])
async def test_repair_flow_aborts(
    hass: HomeAssistant, router: AiohttpClientMocker, read_fingerprint: AsyncMock, remove_entry: bool, unreachable: bool
) -> None:
    from custom_components.udr7.repairs import async_create_fix_flow

    entry = await _setup(hass, {**ENTRY_DATA, "fingerprint": FINGERPRINT})
    if remove_entry:
        await hass.config_entries.async_remove(entry.entry_id)
    if unreachable:
        read_fingerprint.side_effect = UdrError("weg")
    flow = await async_create_fix_flow(hass, "x", {"entry_id": entry.entry_id})
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["reason"] == ("entry_removed" if remove_entry else "cannot_connect")


async def test_read_fingerprint_from_tls() -> None:
    der = b"zertifikat"
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = der
    writer = MagicMock()
    writer.get_extra_info.return_value = ssl_object
    client = UdrClient(MagicMock(), HOST, "", "")
    with patch("asyncio.open_connection", AsyncMock(return_value=(MagicMock(), writer))):
        assert await READ_FINGERPRINT(client, ssl.create_default_context()) == hashlib.sha256(der).hexdigest()
    writer.close.assert_called_once()


@pytest.mark.parametrize("failure", ["connect", "no_cert"])
async def test_read_fingerprint_failures(failure: str) -> None:
    writer = MagicMock()
    writer.get_extra_info.return_value = None
    connect = AsyncMock(side_effect=OSError("weg")) if failure == "connect" else AsyncMock(return_value=(None, writer))
    client = UdrClient(MagicMock(), HOST, "", "")
    with patch("asyncio.open_connection", connect), pytest.raises(UdrError):
        await READ_FINGERPRINT(client, ssl.create_default_context())


# --- Ausfälle -------------------------------------------------------------------------------------------------------


async def test_unreachable_and_back(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/stat/health", status=502)
    await _tick(hass, setup)
    assert hass.states.get(LATENCY).state == STATE_UNAVAILABLE
    mock_router(aioclient_mock)
    await _tick(hass, setup)
    assert hass.states.get(LATENCY).state == "11.0"


async def test_login_rejected_starts_reauth(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/stat/health", status=401)
    mock_router(aioclient_mock, clear=False, login_status=403)
    await _tick(hass, setup)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_session_still_rejected_is_unavailable(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """401 trotz frischer Anmeldung (Network-App startet noch): nicht verfügbar, kein Reauth."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/stat/health", status=401)
    mock_router(aioclient_mock, clear=False)
    await _tick(hass, setup)
    assert len(calls(aioclient_mock, "POST", "/api/auth/login")) == 1
    assert hass.states.get(LATENCY).state == STATE_UNAVAILABLE
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_parallel_401_logs_in_once(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Die Regel-Abfragen laufen parallel in ein 401: genau eine neue Anmeldung."""
    first: set[str] = set()

    def expire_once(name: str) -> Any:
        async def _side_effect(method: str, url: URL, _data: Any) -> AiohttpClientMockResponse:
            if name not in first:
                first.add(name)
                await asyncio.sleep(0)
                return AiohttpClientMockResponse(method, url, status=401)
            return AiohttpClientMockResponse(method, url, json=fixture(name))

        return _side_effect

    aioclient_mock.clear_requests()
    for name, path in (("firewall_policies", "firewall-policies"), ("traffic_routes", "trafficroutes")):
        aioclient_mock.get(f"{V2}/{path}", side_effect=expire_once(name))
    mock_router(aioclient_mock, clear=False)
    setup.runtime_data._full_refresh = True
    await _tick(hass, setup)
    assert len(first) == 2
    assert len(calls(aioclient_mock, "POST", "/api/auth/login")) == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"exc": TimeoutError()}, "cannot connect"),
        ({"exc": aiohttp.ClientConnectionError("kaputt")}, "cannot connect"),
        ({"text": "<html>Anmeldeseite</html>"}, "no JSON response"),
        ({"status": 302, "headers": {"Location": "/login"}}, "HTTP 302"),
        ({"json": {"meta": {"rc": "error", "msg": "api.err.Invalid"}}}, "api.err.Invalid"),
    ],
)
async def test_request_failures(
    hass: HomeAssistant,
    setup: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
    message: str,
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/stat/health", **response)
    mock_router(aioclient_mock, clear=False)
    await _tick(hass, setup)
    assert hass.states.get(LATENCY).state == STATE_UNAVAILABLE
    assert message in str(setup.runtime_data.last_exception)


@pytest.mark.parametrize("exc", [TimeoutError(), aiohttp.ClientConnectionError("kaputt")])
async def test_login_unreachable(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, exc: Exception) -> None:
    aioclient_mock.post(f"{BASE}/api/auth/login", exc=exc)
    mock_router(aioclient_mock, clear=False)
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_side_endpoints_fail_softly(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Was diese Network-Version nicht kennt oder dem Konto verwehrt ist, lässt den Rest weiterlaufen."""
    mock_router(
        aioclient_mock,
        traffic_routes=404,
        content_filters=500,
        critical_events=403,
        system=403,
        known_clients=500,
        clients=500,
    )
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(LATENCY).state == "11.0"
    assert hass.states.get("switch.dream_router_7_route_example_route") is None
    assert hass.states.get("switch.dream_router_7_firewall_allow_example").state == STATE_ON
    assert hass.states.get("sensor.dream_router_7_critical_events_24_h").state == "unknown"
    app = hass.states.get("update.dream_router_7_unifi_network")
    assert app.state == STATE_OFF and app.attributes["installed_version"] == "10.6.106"


# --- Speedtest ------------------------------------------------------------------------------------------------------


async def test_speedtest_running_until_result(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    running = "binary_sensor.unifi_speedtest_in_progress"
    await hass.services.async_call("button", "press", {"entity_id": "button.unifi_run_speedtest"}, blocking=True)
    assert hass.states.get(running).state == STATE_ON
    # Neues Ergebnis in stat/health: läuft nicht mehr
    health = copy.deepcopy(fixture("health"))
    health["data"][2]["speedtest_lastrun"] = 1792671100
    mock_router(aioclient_mock, health=health)
    freezer.tick(timedelta(seconds=45))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(running).state == STATE_OFF


async def test_speedtest_gives_up(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    running = "binary_sensor.unifi_speedtest_in_progress"
    await hass.services.async_call("button", "press", {"entity_id": "button.unifi_run_speedtest"}, blocking=True)
    await _tick(hass, setup)
    assert hass.states.get(running).state == STATE_ON
    freezer.tick(timedelta(minutes=4))
    await _tick(hass, setup)
    assert hass.states.get(running).state == STATE_OFF


async def test_auto_speedtest(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Der Schalter plant den Speedtest im eingestellten Abstand; der Zeitgeber wird hier direkt ausgelöst, statt
    auf die Uhr zu warten (sonst hängt der Test am Timing der CI-Rechner)."""
    timers: list[tuple[Any, timedelta]] = []

    def track(_hass: HomeAssistant, action: Any, interval: timedelta) -> Any:
        timers.append((action, interval))
        return lambda: None

    mock_router(aioclient_mock)
    with patch("custom_components.udr7.switch.async_track_time_interval", side_effect=track):
        await _setup(hass)
        # Standard an: ein Zeitgeber im Abstand aus den Optionen (Standard 60 Minuten)
        assert [interval for _action, interval in timers] == [timedelta(minutes=60)]
        await timers[-1][0](dt_util.utcnow())
        await hass.async_block_till_done()
        assert {"cmd": "speedtest"} in calls(aioclient_mock, "POST", "cmd/devmgr")
        # Läuft schon ein Speedtest, startet der Zeitgeber keinen zweiten
        aioclient_mock.mock_calls.clear()
        await timers[-1][0](dt_util.utcnow())
        assert calls(aioclient_mock, "POST", "cmd/devmgr") == []
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.unifi_wan_auto_speedtest"}, blocking=True
        )
    assert hass.states.get("switch.unifi_wan_auto_speedtest").state == STATE_OFF


# --- Geräte -----------------------------------------------------------------------------------------------------------


async def test_new_and_removed_device(
    hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    devices = copy.deepcopy(fixture("devices"))
    new = copy.deepcopy(devices["data"][1])
    new.update({"_id": "ap2", "mac": "aa:bb:cc:00:00:04", "name": "U7 Pro"})
    devices["data"].append(new)
    mock_router(aioclient_mock, devices=devices)
    setup.runtime_data._full_refresh = True
    await _tick(hass, setup)
    assert hass.states.get("sensor.u7_pro_connected_clients").state == "5"

    # Switch abgebaut: seine Entitäten sind nicht verfügbar, sein Gerät darf gelöscht werden
    devices["data"] = [d for d in devices["data"] if d["mac"] != SW]
    mock_router(aioclient_mock, devices=devices)
    setup.runtime_data._full_refresh = True
    await _tick(hass, setup)
    assert hass.states.get("sensor.usw_flex_2_5g_cpu_load").state == STATE_UNAVAILABLE
    registry = dr.async_get(hass)
    from custom_components.udr7 import async_remove_config_entry_device

    switch = registry.async_get_device_by_identifier((DOMAIN, SW), setup.entry_id)
    hub = registry.async_get_device_by_identifier((DOMAIN, GW), setup.entry_id)
    ap = registry.async_get_device_by_identifier((DOMAIN, "aa:bb:cc:00:00:04"), setup.entry_id)
    assert switch and hub and ap
    assert await async_remove_config_entry_device(hass, setup, switch)
    assert not await async_remove_config_entry_device(hass, setup, hub)
    assert not await async_remove_config_entry_device(hass, setup, ap)


async def test_offline_device(hass: HomeAssistant, setup: MockConfigEntry, aioclient_mock: AiohttpClientMocker) -> None:
    devices = copy.deepcopy(fixture("devices"))
    devices["data"][1]["state"] = 0
    mock_router(aioclient_mock, devices=devices)
    setup.runtime_data._full_refresh = True
    await _tick(hass, setup)
    assert hass.states.get("binary_sensor.u7_lite_online").state == STATE_OFF
    assert hass.states.get("sensor.u7_lite_cpu_load").state == STATE_UNAVAILABLE


# --- Diagnose -------------------------------------------------------------------------------------------------------


async def test_diagnostics_redacted(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    data = await async_get_config_entry_diagnostics(hass, setup)
    text = json.dumps(data, default=str)
    assert data["entry"]["password"] == "**REDACTED**"
    assert data["data"]["rules"]["firewall_policy"] == 2
    assert data["data"]["devices"][0]["model"] == "UDMA67A"
    for secret in (HOST, "geheim", "aa:bb:cc", "203.0.113.7", "example-pass", "AABBCC", "Example Phone", "2001:db8"):
        assert secret not in text, secret


@pytest.mark.parametrize(
    ("www_status", "gateway_status", "running"),
    [
        ("Idle", {"status_summary": 1, "rundate": 0}, False),
        ("Running", {}, True),
        (None, {"status_summary": 1, "rundate": 0}, False),
        (None, {"status_summary": 1, "rundate": 1792671000}, True),
    ],
)
async def test_speedtest_running_from_router(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    www_status: str | None,
    gateway_status: dict[str, Any],
    running: bool,
) -> None:
    """Der Platzhalter im Gerätedatensatz (status_summary 1, rundate 0) heißt nicht, dass ein Test läuft."""
    health = copy.deepcopy(fixture("health"))
    if www_status is None:
        health["data"][2].pop("speedtest_status")
    else:
        health["data"][2]["speedtest_status"] = www_status
    devices = copy.deepcopy(fixture("devices"))
    devices["data"][0]["speedtest-status"] = gateway_status
    mock_router(aioclient_mock, health=health, devices=devices)
    await _setup(hass)
    assert hass.states.get("binary_sensor.unifi_speedtest_in_progress").state == (STATE_ON if running else STATE_OFF)


# --- Produktbilder --------------------------------------------------------------------------------------------------

CATALOG = {
    "devices": [
        {"sysid": "a67a", "icon": {"id": "icon-udr7", "resolutions": [[256, 256], [257, 257], [512, 512]]}},
        {"sysid": "a693", "icon": {"id": "icon-u7", "resolutions": [[128, 128]]}},
        {"sysid": "", "icon": {"id": "x", "resolutions": [[257, 257]]}},
        {"sysid": "ffff", "icon": {}},
        "kaputt",
    ]
}


async def test_device_pictures(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    from custom_components.udr7.catalog import CATALOG_URL

    aioclient_mock.get(CATALOG_URL, json=CATALOG)
    mock_router(aioclient_mock, clear=False)
    entry = await _setup(hass)
    await hass.async_block_till_done()
    update = hass.states.get("update.dream_router_7_firmware")
    assert update.attributes["entity_picture"].endswith("/icon-udr7_257x257.png")
    online = hass.states.get("binary_sensor.u7_lite_online")
    assert online.attributes["entity_picture"].endswith("/icon-u7_128x128.png")
    # Switch nicht im Katalog: kein Bild
    assert "entity_picture" not in hass.states.get("binary_sensor.usw_flex_2_5g_online").attributes
    # Zweiter Start: aus dem Speicher, ohne Download
    calls_before = len([c for c in aioclient_mock.mock_calls if str(c[1]) == CATALOG_URL])
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len([c for c in aioclient_mock.mock_calls if str(c[1]) == CATALOG_URL]) == calls_before


async def test_device_pictures_offline(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    from custom_components.udr7.catalog import CATALOG_URL

    aioclient_mock.get(CATALOG_URL, exc=aiohttp.ClientConnectionError("offline"))
    mock_router(aioclient_mock, clear=False)
    await _setup(hass)
    await hass.async_block_till_done()
    assert "entity_picture" not in hass.states.get("binary_sensor.u7_lite_online").attributes
