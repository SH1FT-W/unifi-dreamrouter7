"""Gemeinsame Test-Bausteine: ein nachgebauter Dream Router 7 mit U7 Lite und Switch (Antworten fiktiv)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.udr7.const import CONF_TRACKED_CLIENTS, DOMAIN

HOST = "192.0.2.1"
BASE = f"https://{HOST}"
API = f"{BASE}/proxy/network/api/s/default"
V2 = f"{BASE}/proxy/network/v2/api/site/default"
GW = "aa:bb:cc:00:00:01"
AP = "aa:bb:cc:00:00:02"
SW = "aa:bb:cc:00:00:03"
PHONE = "aa:bb:cc:10:00:01"
TV = "aa:bb:cc:10:00:02"
CSRF = "csrf-token"
ENTRY_DATA = {"host": HOST, "username": "ha-user", "password": "geheim", "site": "default"}
# SHA-256 des Zertifikats, das der nachgebaute Router zeigt
FINGERPRINT = "ab" * 32
# Kurz nach dem last_seen der Clients in den Fixtures
NOW = 1792671060

# Fixture-Datei → (Methode, URL)
ENDPOINTS = {
    "health": f"{API}/stat/health",
    "devices": f"{API}/stat/device",
    "clients": f"{API}/stat/sta",
    "known_clients": f"{API}/rest/user",
    "wlans": f"{API}/rest/wlanconf",
    "firewall_policies": f"{V2}/firewall-policies",
    "traffic_rules": f"{V2}/trafficrules",
    "traffic_routes": f"{V2}/trafficroutes",
    "port_forwards": f"{API}/rest/portforward",
    "content_filters": f"{V2}/content-filtering",
    "client_policies": f"{V2}/object-oriented-network-configs",
    "networks": f"{API}/rest/networkconf",
    "qos_rules": f"{V2}/qos-rules",
    "gateway_settings": f"{API}/get/setting/usg",
    "sysinfo": f"{API}/stat/sysinfo",
    "system": f"{BASE}/api/system",
}
OK: dict[str, Any] = {"meta": {"rc": "ok"}, "data": []}


def fixture(name: str) -> Any:
    return json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())


def mock_router(mock: AiohttpClientMocker, *, login_status: int = 200, clear: bool = True, **overrides: Any) -> None:
    """Alle Endpunkte; `overrides` ersetzt einzelne Antworten (Name wie die Fixture-Datei, Zahl = HTTP-Status).

    Mit clear=False bleiben vorher registrierte Antworten vorne (der Mocker nimmt die erste passende)."""
    if clear:
        mock.clear_requests()
    mock.post(
        f"{BASE}/api/auth/login", status=login_status, json={}, headers={"X-Csrf-Token": CSRF}, cookies={"TOKEN": "tok"}
    )
    for name, url in ENDPOINTS.items():
        value = overrides.get(name, fixture(name))
        if isinstance(value, int):
            mock.get(url, status=value, text="")
        else:
            mock.get(url, json=value)
    # Befehle
    events = overrides.get("critical_events", fixture("critical_events"))
    if isinstance(events, int):
        mock.post(f"{V2}/system-log/critical", status=events, text="")
    else:
        mock.post(f"{V2}/system-log/critical", json=events)
    for path in ("cmd/devmgr", "cmd/stamgr"):
        mock.post(f"{API}/{path}", json=OK)
    # Der Mocker vergleicht die ganze Adresse: je Objekt aus den Fixtures eine Antwort
    for path, name in (
        ("rest/device", "devices"),
        ("rest/wlanconf", "wlans"),
        ("rest/portforward", "port_forwards"),
        ("rest/networkconf", "networks"),
    ):
        for item in fixture(name)["data"]:
            mock.put(f"{API}/{path}/{item['_id']}", json=OK)
    mock.put(f"{API}/set/setting/usg", json=OK)
    for path, name in (
        ("firewall-policies", "firewall_policies"),
        ("trafficroutes", "traffic_routes"),
        ("content-filtering", "content_filters"),
        ("object-oriented-network-config", "client_policies"),
    ):
        for item in fixture(name):
            mock.put(f"{V2}/{path}/{item['_id']}", json={})
    mock.post(f"{BASE}/api/controllers/network/update", json={})
    mock.patch(f"{BASE}/api/system", json={})
    # Produktbilder: ohne eigene Antwort im Test kein Katalog (wie offline)
    mock.get("https://static.ui.com/fingerprint/ui/public.json", status=404)


def calls(mock: AiohttpClientMocker, method: str, path: str) -> list[Any]:
    """Bodies aller Aufrufe einer Methode auf einen Pfad (Pfadende)."""
    return [data for m, url, data, _headers in mock.mock_calls if m.upper() == method and str(url).endswith(path)]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return


@pytest.fixture(autouse=True)
def read_fingerprint() -> Generator[AsyncMock]:
    """Zertifikat lesen braucht eine echte TLS-Verbindung; hier liefert es immer FINGERPRINT."""
    with patch(
        "custom_components.udr7.api.UdrClient.certificate_fingerprint", AsyncMock(return_value=FINGERPRINT)
    ) as mock:
        yield mock


@pytest.fixture
def router(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    mock_router(aioclient_mock)
    return aioclient_mock


@pytest.fixture
async def setup(hass: HomeAssistant, router: AiohttpClientMocker, freezer: FrozenDateTimeFactory) -> MockConfigEntry:
    freezer.move_to(datetime.fromtimestamp(NOW, tz=UTC))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dream Router 7",
        unique_id=GW,
        data=ENTRY_DATA,
        options={CONF_TRACKED_CLIENTS: [PHONE, TV]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
