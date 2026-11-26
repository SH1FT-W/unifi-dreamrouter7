"""Short failures of the Network app (single timeouts, HTTP 502/503, a restart) must not look like outages."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.udr7.const import CONF_TRACKED_CLIENTS, DOMAIN

from .conftest import ENDPOINTS, GW, PHONE, TV, fixture, mock_router

WAN_IP = "sensor.unifi_wan_ipv4"
INTERNET = "binary_sensor.unifi_wan_internet"
OUTAGES = "sensor.dream_router_7_internet_outages_today"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dream Router 7",
        unique_id=GW,
        data={"host": "192.0.2.1", "username": "u", "password": "p", "site": "default"},
        options={CONF_TRACKED_CLIENTS: [PHONE, TV]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _timeout(mock: AiohttpClientMocker) -> None:
    mock.clear_requests()
    mock.get(ENDPOINTS["health"], exc=TimeoutError())


def _health_error() -> dict[str, Any]:
    """stat/health right after the Network app started: www "error" before its first check (no latency yet)."""
    health = copy.deepcopy(fixture("health"))
    for item in health["data"]:
        if item["subsystem"] == "www":
            item["status"] = "error"
            item.pop("latency", None)
    return health


async def test_single_failed_polls_keep_entities(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Up to two failed polls in a row keep the last values; the third makes the entities unavailable."""
    mock_router(aioclient_mock)
    entry = await _setup(hass)
    ip = hass.states.get(WAN_IP).state
    changes: list[str] = []
    hass.bus.async_listen("state_changed", lambda e: changes.append(e.data["entity_id"]))

    _timeout(aioclient_mock)
    await _poll(hass, entry)
    mock_router(aioclient_mock, health=503)
    await _poll(hass, entry)
    assert hass.states.get(WAN_IP).state == ip
    assert entry.runtime_data.last_update_success
    assert WAN_IP not in changes

    # Back: the counter starts again
    mock_router(aioclient_mock)
    await _poll(hass, entry)
    _timeout(aioclient_mock)
    await _poll(hass, entry)
    await _poll(hass, entry)
    assert hass.states.get(WAN_IP).state == ip

    # Third failure in a row: now it is a real loss of the router
    await _poll(hass, entry)
    assert hass.states.get(WAN_IP).state == "unavailable"
    assert not entry.runtime_data.last_update_success


async def test_timeout_names_its_type(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """str(TimeoutError()) is empty: the error text names the type instead of ending in "cannot connect: "."""
    mock_router(aioclient_mock)
    entry = await _setup(hass)
    _timeout(aioclient_mock)
    for _ in range(3):
        await _poll(hass, entry)
    assert "cannot connect: TimeoutError" in str(entry.runtime_data.last_exception)


async def test_network_app_restart_is_no_outage(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    """After a restart of the Network app (router unreachable, then www "error" for ~40 s) no outage is counted."""
    freezer.move_to("2026-11-23 08:55:00+00:00")
    mock_router(aioclient_mock)
    entry = await _setup(hass)
    mock_router(aioclient_mock, health=502)
    for _ in range(6):
        freezer.tick(timedelta(seconds=30))
        await _poll(hass, entry)
    assert hass.states.get(WAN_IP).state == "unavailable"
    mock_router(aioclient_mock, health=_health_error())
    for _ in range(3):
        freezer.tick(timedelta(seconds=15))
        await _poll(hass, entry)
    mock_router(aioclient_mock)
    freezer.tick(timedelta(seconds=15))
    await _poll(hass, entry)
    assert hass.states.get(INTERNET).state == "on"
    assert hass.states.get(OUTAGES).state == "0"
    assert entry.runtime_data.data.outages == []


async def test_outage_after_reconnect_still_counts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    """Internet still down once the app has settled: the outage counts, from the first "error" on."""
    freezer.move_to("2026-11-23 08:55:00+00:00")
    mock_router(aioclient_mock)
    entry = await _setup(hass)
    mock_router(aioclient_mock, health=502)
    for _ in range(4):
        freezer.tick(timedelta(seconds=30))
        await _poll(hass, entry)
    mock_router(aioclient_mock, health=_health_error())
    freezer.tick(timedelta(seconds=15))
    await _poll(hass, entry)
    for _ in range(10):
        freezer.tick(timedelta(seconds=15))
        await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "1"
    assert entry.runtime_data.data.outages[0].start.isoformat() == "2026-11-23T08:57:15+00:00"
    assert entry.runtime_data.data.outages[0].end is None


async def test_failed_poll_does_not_repeat_new_device_event(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Keeping the last data must not hand the event entity the same new device a second time."""
    mock_router(aioclient_mock)
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    assert coordinator.data is not None
    _timeout(aioclient_mock)
    await _poll(hass, entry)
    assert coordinator.data.new_clients == []
