"""Einrichtung, Geräte, Entitäten und ihre Werte am nachgebauten Dream Router."""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any
from unittest.mock import patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

from custom_components.udr7 import create_client
from custom_components.udr7.api import UdrError
from custom_components.udr7.const import DOMAIN

from .conftest import AP, BASE, ENTRY_DATA, GW, PHONE, SW


def state(hass: HomeAssistant, entity_id: str) -> str:
    current = hass.states.get(entity_id)
    assert current is not None, entity_id
    return current.state


async def test_devices(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    devices = dr.async_get(hass)
    hub = devices.async_get_device_by_identifier((DOMAIN, GW), setup.entry_id)
    assert hub is not None and hub.name == "Dream Router 7" and hub.model == "UDR7" and hub.sw_version == "5.1.33"
    for mac, name in ((AP, "U7 Lite"), (SW, "USW Flex 2.5G")):
        sub = devices.async_get_device_by_identifier((DOMAIN, mac), setup.entry_id)
        assert sub is not None and sub.name == name and sub.via_device_id == hub.id
    phone = devices.async_get_device_by_connection((dr.CONNECTION_NETWORK_MAC, PHONE), setup.entry_id)
    assert phone is not None and phone.name == "Example Phone"


async def test_network_sensors(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    # IDs wie in UniFi Network Plus: Dashboards laufen weiter
    assert state(hass, "sensor.unifi_wan_download") == "2.203408"
    assert state(hass, "sensor.wifi_rx") == "0.736848"
    assert state(hass, "sensor.unifi_speedtest_download") == "263.0"
    assert state(hass, "sensor.unifi_wan_ipv4") == "203.0.113.7"
    assert state(hass, "sensor.unifi_wan_ipv6") == "2001:db8:1::7"
    assert state(hass, "sensor.unifi_active_wan_name") == "WAN1 (SFP+ 1)"
    assert state(hass, "binary_sensor.unifi_wan_internet") == "on"
    assert state(hass, "binary_sensor.unifi_active_wan_up") == "on"
    assert state(hass, "sensor.dream_router_7_clients") == "12"
    assert state(hass, "sensor.dream_router_7_guests") == "1"
    assert state(hass, "sensor.dream_router_7_internet_provider") == "Example ISP"
    events = hass.states.get("sensor.dream_router_7_critical_events_24_h")
    assert events is not None and events.state == "1"
    assert events.attributes["latest"] == "Internet connection lost"


async def test_device_entities(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    assert state(hass, "sensor.dream_router_7_cpu_temperature") == "68.4"
    assert state(hass, "sensor.dream_router_7_cpu_load") == "20.2"
    assert state(hass, "sensor.dream_router_7_2_4_ghz_clients") == "8"
    assert state(hass, "sensor.dream_router_7_6_ghz_channel") == "37"
    assert state(hass, "sensor.dream_router_7_port_1_poe_power") == "4.39"
    assert state(hass, "sensor.u7_lite_connected_clients") == "5"
    assert state(hass, "select.dream_router_7_5_ghz_transmit_power") == "high"
    # „custom“ ist keine wählbare Stufe
    assert state(hass, "select.u7_lite_5_ghz_transmit_power") == "unknown"
    assert state(hass, "binary_sensor.usw_flex_2_5g_online") == "on"
    led = hass.states.get("light.dream_router_7_status_led")
    # Kein LED-Ring (hw_caps): nur an/aus, keine Farbe
    assert led is not None and led.state == "on" and led.attributes["supported_color_modes"] == ["onoff"]
    ring = hass.states.get("light.usw_flex_2_5g_status_led")
    assert ring is not None and ring.attributes["rgb_color"] == (0, 0, 255)
    assert state(hass, "light.u7_lite_status_led") == "off"
    update = hass.states.get("update.u7_lite_firmware")
    assert update is not None and update.state == "on" and update.attributes["latest_version"] == "8.7.20"
    # Gateway: neueste UniFi-OS-Version aus /api/system
    gw_update = hass.states.get("update.dream_router_7_firmware")
    assert gw_update is not None and gw_update.attributes["latest_version"] == "5.1.40"
    app = hass.states.get("update.dream_router_7_unifi_network")
    assert app is not None and app.attributes["latest_version"] == "10.7.3"


async def test_ports(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    ents = er.async_get(hass)
    # WAN-Port (SFP+) und Uplink des Switches lassen sich nicht abschalten
    assert ents.async_get_entity_id("switch", DOMAIN, f"{GW}_port_5_enabled") is None
    assert ents.async_get_entity_id("switch", DOMAIN, f"{SW}_port_5_enabled") is None
    assert state(hass, "switch.dream_router_7_port_2") == "on"
    assert state(hass, "switch.dream_router_7_port_1_poe") == "on"
    assert state(hass, "sensor.dream_router_7_port_3_link_speed") == "0"


async def test_rules(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    assert state(hass, "switch.dream_router_7_wifi_example_wifi") == "on"
    assert state(hass, "switch.dream_router_7_wifi_example_guest") == "off"
    assert state(hass, "switch.dream_router_7_firewall_allow_example") == "on"
    assert state(hass, "switch.dream_router_7_firewall_block_example") == "off"
    # Vordefinierte Zonenregeln bekommen keinen Schalter
    assert hass.states.get("switch.dream_router_7_firewall_predefined") is None
    assert state(hass, "switch.dream_router_7_route_example_route") == "on"
    assert state(hass, "switch.dream_router_7_port_forward_example_forward") == "on"
    assert state(hass, "switch.dream_router_7_content_filter_ad_blocker") == "on"
    assert state(hass, "switch.dream_router_7_policy_internet_block") == "on"
    # VPN-Eintrag ohne „enabled“ gilt als an
    assert state(hass, "switch.dream_router_7_vpn_server_wireguard") == "on"
    assert state(hass, "switch.dream_router_7_vpn_client_example_vpn") == "off"
    assert state(hass, "switch.dream_router_7_upnp") == "off"


async def test_clients(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    assert state(hass, "device_tracker.example_phone") == "home"
    assert state(hass, "switch.example_phone_internet_access") == "on"
    assert state(hass, "sensor.example_phone_download") == "0.4"
    assert state(hass, "sensor.example_phone_wifi_signal") == "-59"
    tracker = hass.states.get("device_tracker.example_phone")
    assert tracker is not None and tracker.attributes["connected_to"] == "U7 Lite"
    # Kabel-Client: kein Neu verbinden
    assert state(hass, "button.tv_reconnect") == "unavailable"
    assert state(hass, "device_tracker.tv") == "home"


async def test_unload(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(setup.entry_id)
    await hass.async_block_till_done()


def _token(value: str) -> SimpleCookie:
    """TOKEN cookie as UniFi OS sets it on login (valid for the whole host)."""
    cookie: SimpleCookie = SimpleCookie()
    cookie["TOKEN"] = value
    cookie["TOKEN"]["path"] = "/"
    return cookie


async def test_login_cookie_stays_out_of_shared_session(hass: HomeAssistant) -> None:
    """The TOKEN cookie of a login must not land in a cookie jar that another login on the same host fills too.

    HA's shared session keeps cookies for host names (like unifi.local, not for IP addresses) and aiohttp sends
    them along with our own Cookie header: a config flow, reauth or reconfigure with another account would swap
    the session of the running entry (same finding as the HACS review of UNAS, hacs/default#13228).
    """
    data = {"host": "unifi.local", "username": "ha-user", "password": "geheim"}
    running = create_client(hass, data)
    flow = create_client(hass, {**data, "username": "other"})
    login = URL("https://unifi.local/api/auth/login")
    running._session.cookie_jar.update_cookies(_token("running-account"), login)
    flow._session.cookie_jar.update_cookies(_token("other-account"), login)

    sent = running._session.cookie_jar.filter_cookies(
        URL("https://unifi.local/proxy/network/api/s/default/stat/device")
    )
    assert "TOKEN" not in sent
    running.detach()
    flow.detach()


async def test_flow_session_released_and_closed_session_reported(hass: HomeAssistant) -> None:
    """A config-flow client releases its own session with detach() (HA turns close() into a warning only), and a
    request on a released session fails with our own error instead of aiohttp's RuntimeError."""
    client = create_client(hass, {"host": "unifi.local", "username": "u", "password": "p"}, auto_cleanup=False)
    client.detach()
    assert client._session.closed
    with pytest.raises(UdrError, match="session closed"):
        await client.login()


async def test_session_detached_while_connecting_gives_own_error(hass: HomeAssistant) -> None:
    """HA detaches the entry's session on unload; a request still connecting then fails inside aiohttp
    (AttributeError/RuntimeError). That must surface as our own error, so services raise HomeAssistantError."""
    client = create_client(hass, {"host": "unifi.local", "username": "u", "password": "p"})

    async def detach_during_connect(*_args: Any, **_kwargs: Any) -> None:
        client.detach()
        raise AttributeError("'NoneType' object has no attribute '_timeout_ceil_threshold'")

    with (
        patch.object(client._session, "post", detach_during_connect),
        pytest.raises(UdrError, match="session closed"),
    ):
        await client.login()
    # Session still open: a real bug is not hidden behind "session closed"
    other = create_client(hass, {"host": "unifi.local", "username": "u", "password": "p"})
    with patch.object(other._session, "post", side_effect=AttributeError("other bug")), pytest.raises(AttributeError):
        await other.login()
    other.detach()


async def test_entry_session_released_on_unload_and_failed_setup(
    hass: HomeAssistant, router: AiohttpClientMocker
) -> None:
    """The entry's own session goes away with the entry (unload and failed setup), not only at HA stop.

    aioclient_mock replaces HA's session factory and drops its cleanup hook; the wrapper runs that hook as HA
    does, so this checks HA's per-entry cleanup (current_entry) together with create_client(auto_cleanup=True).
    """
    sessions: list[aiohttp.ClientSession] = []
    mocked = aiohttp_client._async_create_clientsession

    def factory(hass_: HomeAssistant, *args: Any, auto_cleanup_method: Any = None, **kwargs: Any) -> Any:
        session = mocked(hass_, *args, auto_cleanup_method=auto_cleanup_method, **kwargs)
        if auto_cleanup_method is not None:
            auto_cleanup_method(hass_, session)
        # Only sessions from async_create_clientsession (ours), not HA's shared one (product pictures)
        if auto_cleanup_method is aiohttp_client._async_register_clientsession_shutdown:
            sessions.append(session)
        return session

    with patch.object(aiohttp_client, "_async_create_clientsession", factory):
        entry = MockConfigEntry(domain=DOMAIN, unique_id="session-test", data=ENTRY_DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert sessions and not any(session.closed for session in sessions)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert all(session.closed for session in sessions)

        # Failed setup (not reachable): released as well
        sessions.clear()
        router.clear_requests()
        router.post(f"{BASE}/api/auth/login", exc=aiohttp.ClientError("down"))
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert sessions and all(session.closed for session in sessions)
    assert not aiohttp_client.async_get_clientsession(hass).closed
