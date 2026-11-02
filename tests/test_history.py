"""New devices (event), internet outages and the attributes of latency and VPN."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Event, HomeAssistant, callback
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.udr7.api import parse_client
from custom_components.udr7.const import CONF_NEW_DEVICES_WIRED, CONF_TRACKED_CLIENTS, DOMAIN

from .conftest import GW, PHONE, TV, fixture, mock_router

EVENT = "event.dream_router_7_new_device_connected"
OUTAGES = "sensor.dream_router_7_internet_outages_today"
LAST_OUTAGE = "sensor.dream_router_7_last_internet_outage"
INTERNET = "binary_sensor.unifi_wan_internet"
# Private (randomized) address: locally administered bit set (0xda = 1101 1010)
RANDOM_WIFI = "da:bb:cc:10:00:09"
NEW_WIRED = "aa:bb:cc:10:00:0a"


def _clients(*extra: dict[str, Any]) -> dict[str, Any]:
    clients = fixture("clients")
    clients["data"].extend(extra)
    return clients


NEW_WIFI_CLIENT = {
    "mac": RANDOM_WIFI,
    "hostname": "visitor-phone",
    "oui": "Example Vendor",
    "ip": "192.0.2.60",
    "is_wired": False,
    "essid": "Example Guest",
    "network": "Guests",
}
NEW_WIRED_CLIENT = {"mac": NEW_WIRED, "name": "Printer", "ip": "192.0.2.61", "is_wired": True, "network": "Home"}


async def _setup(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dream Router 7",
        unique_id=GW,
        data={"host": "192.0.2.1", "username": "u", "password": "p", "site": "default"},
        options={CONF_TRACKED_CLIENTS: [PHONE, TV], **options},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.runtime_data._full_refresh = True
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _restart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def test_random_mac() -> None:
    assert parse_client({"mac": RANDOM_WIFI}).random_mac
    assert not parse_client({"mac": "ac:bb:cc:10:00:0b"}).random_mac
    assert not parse_client({"mac": "zz:00"}).random_mac


async def test_new_device_event(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage: dict[str, Any]
) -> None:
    mock_router(aioclient_mock)
    entry = await _setup(hass)
    fired: list[Event] = []

    @callback
    def _record(event: Event) -> None:
        if event.data["entity_id"] == EVENT:
            fired.append(event)

    hass.bus.async_listen("state_changed", _record)
    # First start: connected and known clients count as seen, no event
    assert hass.states.get(EVENT).state == "unknown"
    await _poll(hass, entry)
    assert fired == []

    # A new WiFi device and a new wired one; only WiFi is reported by default
    mock_router(aioclient_mock, clients=_clients(NEW_WIFI_CLIENT, NEW_WIRED_CLIENT))
    await _poll(hass, entry)
    state = hass.states.get(EVENT)
    assert state.attributes["event_type"] == "new_device"
    assert state.attributes["mac"] == RANDOM_WIFI
    assert state.attributes["hostname"] == "visitor-phone"
    assert state.attributes["name"] is None
    assert state.attributes["manufacturer"] == "Example Vendor"
    assert state.attributes["ssid"] == "Example Guest"
    assert state.attributes["ip"] == "192.0.2.60"
    assert state.attributes["random_mac"] is True
    assert state.attributes["wired"] is False
    assert state.attributes["time"]
    assert len(fired) == 1

    # Same devices again, and a listener update without a poll: nothing new
    await _poll(hass, entry)
    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    assert len(fired) == 1

    # Known MACs survive a restart (written on unload)
    await _restart(hass, entry)
    known = hass_storage[f"{DOMAIN}.{entry.entry_id}.history"]["data"]["known_macs"]
    assert RANDOM_WIFI in known and NEW_WIRED in known and "aa:bb:cc:10:00:03" in known
    # The restored state is the last event; a poll after the restart fires nothing
    before = hass.states.get(EVENT).state
    await _poll(hass, entry)
    assert hass.states.get(EVENT).state == before


async def test_new_wired_device_with_option(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_router(aioclient_mock)
    entry = await _setup(hass, **{CONF_NEW_DEVICES_WIRED: True})
    mock_router(aioclient_mock, clients=_clients(NEW_WIFI_CLIENT, NEW_WIRED_CLIENT))
    fired: list[str] = []

    @callback
    def _record(event: Event) -> None:
        if event.data["entity_id"] == EVENT:
            fired.append(event.data["new_state"].attributes["mac"])

    hass.bus.async_listen("state_changed", _record)
    await _poll(hass, entry)
    # Both devices of the same poll get their own state
    assert fired == [RANDOM_WIFI, NEW_WIRED]
    state = hass.states.get(EVENT)
    assert state.attributes["wired"] is True
    assert state.attributes["ssid"] is None
    assert state.attributes["manufacturer"] is None


async def test_no_seed_without_clients(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """stat/sta failing at the first start must not turn every client into a new device later."""
    mock_router(aioclient_mock, clients=500)
    entry = await _setup(hass)
    assert entry.runtime_data._known_macs is None
    mock_router(aioclient_mock)
    await _poll(hass, entry)
    assert hass.states.get(EVENT).state == "unknown"
    assert entry.runtime_data._known_macs == {PHONE, TV, "aa:bb:cc:10:00:03"}


def _health(status: str) -> dict[str, Any]:
    health = copy.deepcopy(fixture("health"))
    for item in health["data"]:
        if item["subsystem"] == "www":
            item["status"] = status
    return health


async def test_internet_outages(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    freezer.move_to("2026-11-02 10:00:00+00:00")
    mock_router(aioclient_mock)
    entry = await _setup(hass)
    assert hass.states.get(OUTAGES).state == "0"
    assert hass.states.get(LAST_OUTAGE).state == "unknown"

    # Internet gone: outage starts and is ongoing
    mock_router(aioclient_mock, health=_health("error"))
    await _poll(hass, entry)
    assert hass.states.get(INTERNET).state == "off"
    assert hass.states.get(OUTAGES).state == "1"
    last = hass.states.get(LAST_OUTAGE)
    assert last.state == "2026-11-02T10:00:00+00:00"
    assert last.attributes["ongoing"] is True
    assert last.attributes["end"] is None

    # Still down: same outage, duration only once it is over
    freezer.tick(timedelta(seconds=30))
    await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "1"
    assert hass.states.get(LAST_OUTAGE).attributes["duration_seconds"] is None

    # Back after 90 s
    freezer.tick(timedelta(seconds=60))
    mock_router(aioclient_mock)
    await _poll(hass, entry)
    last = hass.states.get(LAST_OUTAGE)
    assert last.attributes["ongoing"] is False
    assert last.attributes["duration_seconds"] == 90
    assert last.attributes["end"] == "2026-11-02T10:01:30+00:00"

    # A second outage the same day
    mock_router(aioclient_mock, health=_health("error"))
    await _poll(hass, entry)
    mock_router(aioclient_mock)
    await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "2"

    # Survives a restart of HA
    await _restart(hass, entry)
    assert len(hass_storage[f"{DOMAIN}.{entry.entry_id}.history"]["data"]["outages"]) == 2
    assert hass.states.get(OUTAGES).state == "2"
    assert hass.states.get(LAST_OUTAGE).attributes["ongoing"] is False

    # Next day: the counter starts at 0, the last outage stays; an outage after two days drops the old ones
    freezer.move_to("2026-11-03 10:00:00+00:00")
    await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "0"
    assert hass.states.get(LAST_OUTAGE).state.startswith("2026-11-02")
    freezer.move_to("2026-11-05 10:00:00+00:00")
    mock_router(aioclient_mock, health=_health("error"))
    await _poll(hass, entry)
    assert len(entry.runtime_data.data.outages) == 1


async def test_outage_over_local_midnight(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    """ "Today" is the local day; an outage running past midnight also counts on the new day."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-11-02 22:50:00+00:00")  # 23:50 in Berlin
    mock_router(aioclient_mock)
    entry = await _setup(hass)
    mock_router(aioclient_mock, health=_health("error"))
    await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "1"
    freezer.move_to("2026-11-02 23:10:00+00:00")  # 00:10 on 3 Nov in Berlin, same day in UTC
    await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "1"
    mock_router(aioclient_mock)
    await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "1"
    freezer.move_to("2026-11-03 23:30:00+00:00")  # 00:30 on 4 Nov in Berlin
    await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "0"


async def test_no_seed_without_known_clients(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """rest/user failing at the first start must not leave offline known devices out of the seed."""
    mock_router(aioclient_mock, known_clients=500)
    entry = await _setup(hass)
    assert entry.runtime_data._known_macs is None
    # A round without rest/user does not seed either
    mock_router(aioclient_mock)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data._known_macs is None
    await _poll(hass, entry)
    assert entry.runtime_data._known_macs == {PHONE, TV, "aa:bb:cc:10:00:03"}
    assert hass.states.get(EVENT).state == "unknown"


async def test_internet_status_missing_is_no_outage(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    health = fixture("health")
    health["data"] = [h for h in health["data"] if h["subsystem"] != "www"]
    mock_router(aioclient_mock, health=health)
    entry = await _setup(hass)
    await _poll(hass, entry)
    assert hass.states.get(OUTAGES).state == "0"


async def test_restored_outage_still_ongoing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage: dict[str, Any]
) -> None:
    """An outage running while HA was stopped ends with the first "ok" after the start."""
    hass_storage[f"{DOMAIN}.restored.history"] = {
        "version": 1,
        "key": f"{DOMAIN}.restored.history",
        "data": {
            "known_macs": [PHONE],
            "outages": [
                {"start": "2026-10-30T08:00:00", "end": "2026-10-30T08:05:00"},
                {"start": "2026-10-31T08:00:00+00:00", "end": None},
                {},
                "broken",
                {"start": "2026-13-01T00:00:00"},
            ],
        },
    }
    mock_router(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="restored",
        unique_id=GW,
        title="Dream Router 7",
        data={"host": "192.0.2.1", "username": "u", "password": "p", "site": "default"},
        options={CONF_NEW_DEVICES_WIRED: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    last = hass.states.get(LAST_OUTAGE)
    assert last.state == "2026-10-31T08:00:00+00:00"
    assert last.attributes["ongoing"] is False
    # Known MACs came from the store: the TV joined while HA was stopped and is reported right at the start
    event = hass.states.get(EVENT)
    assert event.attributes["mac"] == TV
    assert event.attributes["wired"] is True
    assert TV in entry.runtime_data._known_macs
    # A stored time without offset counts as UTC
    assert entry.runtime_data.data.outages[0].start.isoformat() == "2026-10-30T08:00:00+00:00"


async def test_latency_and_vpn_attributes(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    latency = hass.states.get("sensor.dream_router_7_internet_latency")
    assert latency.attributes["average"] == 7
    vpn = hass.states.get("sensor.dream_router_7_vpn_clients")
    assert float(vpn.state) == 1
    assert vpn.attributes["enabled"] is True
    assert vpn.attributes["received_bytes"] == 123456
    assert vpn.attributes["sent_bytes"] == 654321
    assert latency.attributes["period_seconds"] == 86400


async def test_unseeded_store_stays_unseeded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage: dict[str, Any]
) -> None:
    """An outage saved before the first seed must not make the next start report every client."""
    mock_router(aioclient_mock, known_clients=500, health=_health("error"))
    entry = await _setup(hass)
    await _restart(hass, entry)
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}.history"]["data"]["known_macs"] is None
    assert entry.runtime_data._known_macs is None
    assert hass.states.get(EVENT).state == "unknown"


async def test_store_of_wrong_shape(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage: dict[str, Any]
) -> None:
    for data in (["a"], {"known_macs": "x", "outages": 5}):
        hass_storage[f"{DOMAIN}.shape.history"] = {"version": 1, "key": f"{DOMAIN}.shape.history", "data": data}
        mock_router(aioclient_mock)
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id="shape",
            unique_id=GW,
            title="Dream Router 7",
            data={"host": "192.0.2.1", "username": "u", "password": "p", "site": "default"},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(OUTAGES).state == "0"
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
