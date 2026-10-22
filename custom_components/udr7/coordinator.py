"""Fragt den Dream Router ab: stat/health alle 15 s, Geräte und Clients alle 30 s, Einstellungen jede Minute,
Systeminfo und Updates alle 5 Minuten. Nach einem Befehl wird sofort alles neu geholt."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Device,
    UdrAuthError,
    UdrCertificateError,
    UdrClient,
    UdrData,
    UdrError,
    UdrPermissionError,
    format_fingerprint,
)
from .catalog import async_load_pictures
from .const import (
    CONF_FINGERPRINT,
    CONFIG_EVERY,
    DEVICES_EVERY,
    DOMAIN,
    SLOW_EVERY,
    SPEEDTEST_SECONDS,
    SPEEDTEST_TIMEOUT,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type UdrConfigEntry = ConfigEntry[UdrCoordinator]


def cert_issue_id(entry: ConfigEntry) -> str:
    return f"cert_changed_{entry.entry_id}"


def _speedtest_finished(old: UdrData, new: UdrData) -> bool:
    """Ist ein per Button/Automatik gestarteter Speedtest erledigt (oder aufgegeben)?

    Fertig: neuer Zeitstempel in stat/health, oder der Gateway meldet status_summary 2 mit einem Laufdatum nach dem
    Start. Aufgegeben: nach SPEEDTEST_TIMEOUT ohne Ergebnis, sonst bliebe „läuft“ für immer stehen und der
    automatische Speedtest würde nie wieder starten.
    """
    started = old.speedtest_started_at
    if started is None:
        return True
    if dt_util.utcnow() - started >= SPEEDTEST_TIMEOUT:
        return True
    if new.get("www", "speedtest_lastrun") != old.get("www", "speedtest_lastrun"):
        return True
    status = new.speedtest_status
    rundate = status.get("rundate")
    return (
        status.get("status_summary") == 2
        and isinstance(rundate, int | float)
        and not isinstance(rundate, bool)
        and rundate >= started.timestamp()
    )


class UdrCoordinator(DataUpdateCoordinator[UdrData]):
    config_entry: UdrConfigEntry

    def __init__(self, hass: HomeAssistant, entry: UdrConfigEntry, client: UdrClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.client = client
        self._round = 0
        self._cert_issue = False
        # Nach einem Befehl alle Teile in der nächsten Runde holen
        self._full_refresh = False
        # Gerät des Dream Routers (in async_setup_entry angelegt); Access Points und Switches hängen darunter
        self.device_id: str | None = None
        self._result_timer: CALLBACK_TYPE | None = None
        # Port-Einstellungen vor dem Sperren („<mac>_<port>“ → Eintrag aus port_overrides oder None), dauerhaft,
        # damit ein Port auch nach einem Neustart von HA wieder genau so freigegeben wird
        self._port_store: Store[dict[str, dict[str, Any] | None]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.ports"
        )
        self._saved_ports: dict[str, dict[str, Any] | None] = {}
        # Produktbilder je sysid (catalog.py), im Hintergrund nach dem Start geladen
        self.pictures: dict[str, str] = {}

    async def async_load_pictures(self) -> None:
        self.pictures = await async_load_pictures(self.hass)
        self.async_update_listeners()

    def picture(self, device: Device | None) -> str | None:
        return self.pictures.get(device.sysid) if device is not None and device.sysid else None

    async def async_load_storage(self) -> None:
        self._saved_ports = await self._port_store.async_load() or {}

    async def async_set_port_enabled(self, device: Device, port: int, enabled: bool) -> None:
        key = f"{device.mac}_{port}"
        current = device.ports.get(port)
        if enabled and key not in self._saved_ports and current is not None and current.enabled:
            return  # schon frei; ohne gemerkten Stand nichts überschreiben
        if enabled:
            await self.async_command(lambda: self.client.enable_port(device, port, self._saved_ports.get(key)))
            self._saved_ports.pop(key, None)
            await self._port_store.async_save(self._saved_ports)
            return

        async def disable() -> None:
            previous = await self.client.disable_port(device, port)
            # Schon gesperrt (z. B. zweimal ausgeschaltet): den älteren, freigegebenen Stand behalten
            if key not in self._saved_ports or not (previous or {}).get("port_security_enabled"):
                self._saved_ports[key] = previous

        await self.async_command(disable)
        await self._port_store.async_save(self._saved_ports)

    def _due(self, every: int) -> bool:
        return self.data is None or self._full_refresh or self._round % every == 0

    async def _async_update_data(self) -> UdrData:
        old = self.data
        try:
            data = UdrData(health=await self.client.health())
            if self._due(DEVICES_EVERY):
                data.devices = await self.client.devices()
                data.clients = await self._partial(self.client.clients, "stat/sta", {})
            else:
                data.devices, data.clients = old.devices, old.clients
            if self._due(CONFIG_EVERY):
                data.wlans = await self.client.wlans()
                data.rules = await self.client.rules()
                data.known_clients = await self._partial(self.client.known_clients, "rest/user", {})
                data.gateway_settings = await self._partial(self.client.gateway_settings, "rest/setting", {})
                data.critical_events = await self._partial(self.client.critical_events, "system-log", None)
            else:
                data.wlans, data.rules, data.known_clients = old.wlans, old.rules, old.known_clients
                data.gateway_settings, data.critical_events = old.gateway_settings, old.critical_events
            if self._due(SLOW_EVERY):
                data.sysinfo = await self._partial(self.client.sysinfo, "stat/sysinfo", {})
                os_status = await self._partial(self.client.os_status, "/api/system", None)
                if os_status is not None:
                    data.os_firmware, data.network_app, data.os_settings = os_status
                elif old is not None:
                    data.os_firmware, data.network_app, data.os_settings = (
                        old.os_firmware,
                        old.network_app,
                        old.os_settings,
                    )
            else:
                data.sysinfo, data.os_firmware, data.network_app = old.sysinfo, old.os_firmware, old.network_app
                data.os_settings = old.os_settings
            if old is not None and old.speedtest_started_at is not None and not _speedtest_finished(old, data):
                # Bis ein neues Ergebnis da ist, gilt der Speedtest als laufend
                data.speedtest_started_at = old.speedtest_started_at
        except UdrAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err
        except UdrCertificateError as err:
            self._raise_cert_issue(err)
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="cert_changed") from err
        except UdrError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="unreachable", translation_placeholders={"error": str(err)}
            ) from err
        if self._cert_issue or old is None:
            # Auch nach dem Neuladen (z. B. nachdem die Reparatur das neue Zertifikat übernommen hat)
            ir.async_delete_issue(self.hass, DOMAIN, cert_issue_id(self.config_entry))
            self._cert_issue = False
        self._full_refresh = False
        self._round += 1
        return data

    async def _partial[T](self, fetch: Callable[[], Awaitable[T]], name: str, fallback: T) -> T:
        """Nebenabfrage; scheitert sie, bleibt `fallback` stehen, statt dass alle Werte „nicht verfügbar“ werden.

        Ein abgelehnter Login und ein fremdes Zertifikat brechen weiter ab."""
        try:
            return await fetch()
        except (UdrAuthError, UdrCertificateError):
            raise
        except UdrError as err:
            _LOGGER.debug("%s failed: %s", name, err)
            return fallback

    def _raise_cert_issue(self, err: UdrCertificateError) -> None:
        """Reparatur-Hinweis mit altem und neuem Fingerabdruck; bestätigt der Nutzer, gilt das neue Zertifikat."""
        entry = self.config_entry
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            cert_issue_id(entry),
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="cert_changed",
            translation_placeholders={
                "name": entry.title,
                "old": format_fingerprint(str(entry.data.get(CONF_FINGERPRINT, ""))),
                "new": format_fingerprint(err.fingerprint),
            },
            data={"entry_id": entry.entry_id},
        )
        self._cert_issue = True

    async def async_command(self, command: Callable[[], Awaitable[Any]]) -> None:
        """Befehl an den Router schicken und danach (gebündelt) alles neu abfragen."""
        try:
            await command()
        except UdrAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
        except UdrPermissionError as err:
            # Passwort stimmt, nur die Rechte fehlen: kein Reauth
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_permission") from err
        except UdrCertificateError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="cert_changed") from err
        except UdrError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._full_refresh = True
        await self.async_request_refresh()

    async def async_start_speedtest(self) -> None:
        """Speedtest am Gateway starten; nach SPEEDTEST_SECONDS das Ergebnis holen."""
        await self.async_command(self.client.run_speedtest)
        if self.data is not None:
            self.data.speedtest_started_at = dt_util.utcnow()
            self.async_update_listeners()

        @callback
        def _fetch_result(_now: datetime) -> None:
            self._result_timer = None
            self._full_refresh = True
            self.hass.async_create_task(self.async_request_refresh())

        self._cancel_result_timer()
        self._result_timer = async_call_later(self.hass, SPEEDTEST_SECONDS, _fetch_result)

    @callback
    def _cancel_result_timer(self) -> None:
        if self._result_timer is not None:
            self._result_timer()
            self._result_timer = None

    async def async_shutdown(self) -> None:
        self._cancel_result_timer()
        await super().async_shutdown()
