"""Befehle: was beim Schalten an den Dream Router geht."""

from __future__ import annotations

from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import GW, PHONE, calls


async def call(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)


async def test_wlan(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    await call(hass, "switch", "turn_on", "switch.dream_router_7_wifi_example_guest")
    assert calls(router, "PUT", "rest/wlanconf/wlan2") == [{"enabled": True}]


async def test_wlan_password(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    from homeassistant.helpers import entity_registry as er

    ents = er.async_get(hass)
    entity_id = ents.async_get_entity_id("text", "udr7", f"{GW}_wlan_wlan1_password")
    assert entity_id is not None
    ents.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(setup.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "text", "set_value", {ATTR_ENTITY_ID: entity_id, "value": "new-example-pass"}, blocking=True
    )
    assert calls(router, "PUT", "rest/wlanconf/wlan1") == [{"x_passphrase": "new-example-pass"}]
    with pytest.raises(ValueError, match="too short"):
        await hass.services.async_call(
            "text", "set_value", {ATTR_ENTITY_ID: entity_id, "value": "x" * 7}, blocking=True
        )


@pytest.mark.parametrize(
    ("entity_id", "method", "path", "expected"),
    [
        (
            "switch.dream_router_7_firewall_block_example",
            "PUT",
            "firewall-policies/fw2",
            {"_id": "fw2", "enabled": True},
        ),
        ("switch.dream_router_7_route_example_route", "PUT", "trafficroutes/tr1", {"_id": "tr1", "enabled": False}),
        (
            "switch.dream_router_7_port_forward_example_forward",
            "PUT",
            "rest/portforward/pf1",
            {"_id": "pf1", "enabled": False},
        ),
        (
            "switch.dream_router_7_content_filter_ad_blocker",
            "PUT",
            "content-filtering/cf1",
            {"_id": "cf1", "enabled": False},
        ),
        (
            "switch.dream_router_7_policy_internet_block",
            "PUT",
            "object-oriented-network-config/oon1",
            {"_id": "oon1", "enabled": False},
        ),
        (
            "switch.dream_router_7_vpn_server_wireguard",
            "PUT",
            "rest/networkconf/vpn1",
            {"_id": "vpn1", "enabled": False},
        ),
    ],
)
async def test_rules(
    hass: HomeAssistant,
    setup: MockConfigEntry,
    router: AiohttpClientMocker,
    entity_id: str,
    method: str,
    path: str,
    expected: dict[str, Any],
) -> None:
    on = not expected["enabled"]
    await call(hass, "switch", "turn_off" if on else "turn_on", entity_id)
    (body,) = calls(router, method, path)
    # Das ganze Objekt geht zurück, nur „enabled“ ist geändert
    assert {k: body[k] for k in expected} == expected
    assert len(body) > 2


async def test_upnp(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    await call(hass, "switch", "turn_on", "switch.dream_router_7_upnp")
    assert calls(router, "PUT", "set/setting/usg") == [{"_id": "set1", "key": "usg", "upnp_enabled": True}]


async def test_ports(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    from .conftest import fixture, mock_router

    device_url = "rest/device/gw0000000000000000000001"
    # Port 2 hat eine eigene Netzauswahl: Sperren über Port-Sicherheit, die übrigen Ports bleiben unverändert
    await call(hass, "switch", "turn_off", "switch.dream_router_7_port_2")
    (body,) = calls(router, "PUT", device_url)
    overrides = {o["port_idx"]: o for o in body["port_overrides"]}
    assert overrides[2]["port_security_enabled"] is True and overrides[2]["forward"] == "customize"
    assert overrides[1] == {"port_idx": 1, "poe_mode": "auto", "forward": "all", "name": "Port 1"}

    # Die Network-App hat den Eintrag umgebaut; Einschalten schreibt den gemerkten Eintrag zurück
    devices = fixture("devices")
    gateway = devices["data"][0]
    gateway["port_table"][1]["enable"] = False
    gateway["port_overrides"][1] = {"port_idx": 2, "port_security_enabled": True, "forward": "disabled"}
    mock_router(router, devices=devices)
    await setup.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("switch.dream_router_7_port_2").state == "off"
    await call(hass, "switch", "turn_on", "switch.dream_router_7_port_2")
    (body,) = calls(router, "PUT", device_url)
    restored = next(o for o in body["port_overrides"] if o["port_idx"] == 2)
    assert restored == {"port_idx": 2, "forward": "customize", "excluded_networkconf_ids": ["net2"], "name": "Port 2"}

    # Ohne gemerkten Stand (z. B. in der Network-App gesperrt): Eintrag weg, Standard des Ports gilt
    mock_router(router, devices=devices)
    await setup.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await call(hass, "switch", "turn_on", "switch.dream_router_7_port_2")
    (body,) = calls(router, "PUT", device_url)
    assert [o["port_idx"] for o in body["port_overrides"]] == [1]

    # Schon freier Port ohne gemerkten Stand: nichts schicken
    router.mock_calls.clear()
    await call(hass, "switch", "turn_on", "switch.dream_router_7_port_4")
    assert calls(router, "PUT", device_url) == []

    # Neuer Eintrag beim Sperren übernimmt das Port-Profil
    await call(hass, "switch", "turn_off", "switch.dream_router_7_port_4")
    (body,) = calls(router, "PUT", device_url)
    assert next(o for o in body["port_overrides"] if o["port_idx"] == 4) == {
        "port_idx": 4,
        "portconf_id": "pc1",
        "port_security_enabled": True,
    }

    router.mock_calls.clear()
    await call(hass, "switch", "turn_off", "switch.dream_router_7_port_1_poe")
    (body,) = calls(router, "PUT", device_url)
    assert next(o for o in body["port_overrides"] if o["port_idx"] == 1)["poe_mode"] == "off"
    router.mock_calls.clear()
    await call(hass, "button", "press", "button.dream_router_7_port_1_power_cycle")
    assert calls(router, "POST", "cmd/devmgr") == [{"cmd": "power-cycle", "mac": GW, "port_idx": 1}]


async def test_port_memory_survives_restart(
    hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker
) -> None:
    await call(hass, "switch", "turn_off", "switch.dream_router_7_port_2")
    await hass.config_entries.async_reload(setup.entry_id)
    await hass.async_block_till_done()
    assert setup.runtime_data._saved_ports["aa:bb:cc:00:00:01_2"]["forward"] == "customize"


async def test_led(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    # Mit LED-Ring (hier der Switch): Farbe und Helligkeit
    await call(hass, "light", "turn_on", "light.usw_flex_2_5g_status_led", rgb_color=[255, 0, 0], brightness=128)
    assert calls(router, "PUT", "rest/device/sw0000000000000000000003") == [
        {"led_override": "on", "led_override_color": "#ff0000", "led_override_color_brightness": 50}
    ]
    router.mock_calls.clear()
    # Ohne Ring (Dream Router 7): nur an/aus
    await call(hass, "light", "turn_off", "light.dream_router_7_status_led")
    await call(hass, "light", "turn_on", "light.dream_router_7_status_led")
    assert calls(router, "PUT", "rest/device/gw0000000000000000000001") == [
        {"led_override": "off"},
        {"led_override": "on"},
    ]


async def test_radio(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    await call(hass, "select", "select_option", "select.dream_router_7_2_4_ghz_transmit_power", option="low")
    (body,) = calls(router, "PUT", "rest/device/gw0000000000000000000001")
    modes = {r["radio"]: r["tx_power_mode"] for r in body["radio_table"]}
    assert modes == {"ng": "low", "na": "high", "6e": "auto"}


async def test_device_commands(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    await call(hass, "button", "press", "button.u7_lite_restart")
    await call(hass, "switch", "turn_on", "switch.u7_lite_locate")
    await call(hass, "update", "install", "update.u7_lite_firmware")
    await call(hass, "button", "press", "button.unifi_run_speedtest")
    assert calls(router, "POST", "cmd/devmgr") == [
        {"cmd": "restart", "mac": "aa:bb:cc:00:00:02", "reboot_type": "soft"},
        {"cmd": "set-locate", "mac": "aa:bb:cc:00:00:02"},
        {"cmd": "upgrade", "mac": "aa:bb:cc:00:00:02"},
        {"cmd": "speedtest"},
    ]
    await call(hass, "update", "install", "update.dream_router_7_unifi_network")
    assert len(calls(router, "POST", "/api/controllers/network/update")) == 1


async def test_clients(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    await call(hass, "switch", "turn_off", "switch.example_phone_internet_access")
    await call(hass, "button", "press", "button.example_phone_reconnect")
    await hass.services.async_call(
        "udr7", "authorize_guest", {"mac": "AA-BB-CC-10-00-09", "minutes": 60}, blocking=True
    )
    await hass.services.async_call("udr7", "unblock_client", {"mac": "aa:bb:cc:10:00:03"}, blocking=True)
    assert calls(router, "POST", "cmd/stamgr") == [
        {"cmd": "block-sta", "mac": PHONE},
        {"cmd": "kick-sta", "mac": PHONE},
        {"cmd": "authorize-guest", "mac": "aa:bb:cc:10:00:09", "minutes": 60},
        {"cmd": "unblock-sta", "mac": "aa:bb:cc:10:00:03"},
    ]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("udr7", "block_client", {"mac": "nope"}, blocking=True)


async def test_command_errors(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    from .conftest import API, mock_router

    for status, key in ((403, "no_permission"), (500, "command_failed")):
        router.clear_requests()
        router.post(f"{API}/cmd/devmgr", status=status, text="")
        mock_router(router, clear=False)
        with pytest.raises(HomeAssistantError) as err:
            await call(hass, "button", "press", "button.u7_lite_restart")
        assert err.value.translation_key == key


async def test_display_and_night_mode(hass: HomeAssistant, setup: MockConfigEntry, router: AiohttpClientMocker) -> None:
    display = hass.states.get("light.dream_router_7_display")
    # UDR7 ohne „Bezel“: Display nur an/aus und Helligkeit, keine Farbe
    assert (
        display is not None and display.state == "on" and display.attributes["supported_color_modes"] == ["brightness"]
    )
    assert display.attributes["brightness"] == 128 and display.attributes.get("rgb_color") is None
    assert hass.states.get("time.dream_router_7_night_mode_start").state == "21:00:00"
    assert hass.states.get("time.dream_router_7_night_mode_end").state == "08:00:00"
    await call(hass, "light", "turn_on", "light.dream_router_7_display", brightness=255)
    await call(hass, "light", "turn_off", "light.dream_router_7_display")
    await call(hass, "time", "set_value", "time.dream_router_7_night_mode_start", time="22:30:00")
    assert calls(router, "PATCH", "/api/system") == [
        {"lcmSettings": {"enabled": True, "brightness": 100}},
        {"lcmSettings": {"enabled": False}},
        {"nightMode": {"onMinute": 1350, "offMinute": 480}},
    ]


async def test_auto_speedtest_on_by_default(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    # Wie in UniFi Network Plus: ohne früheren Zustand an
    assert hass.states.get("switch.unifi_wan_auto_speedtest").state == "on"


async def test_display_color_with_bezel(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Konsolen mit „Bezel“ haben eine Display-Farbe."""
    from .conftest import ENTRY_DATA, GW, fixture, mock_router

    system = fixture("system")
    system["features"]["hasBezel"] = True
    mock_router(aioclient_mock, system=system)
    entry = MockConfigEntry(domain="udr7", unique_id=GW, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("light.dream_router_7_display").attributes["rgb_color"] == (1, 57, 255)
    await call(hass, "light", "turn_on", "light.dream_router_7_display", rgb_color=[255, 0, 0])
    assert calls(aioclient_mock, "PATCH", "/api/system") == [{"lcmSettings": {"enabled": True, "color": "FF0000"}}]
