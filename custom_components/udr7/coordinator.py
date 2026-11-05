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
    Client,
    Device,
    Outage,
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
    OUTAGE_KEEP,
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


def _aware(value: Any) -> datetime | None:
    """Stored timestamp; one without offset counts as UTC."""
    try:
        parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
    except ValueError:  # matches the pattern but is no real date (month 13)
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


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
        # Every client MAC ever seen (None = not seeded yet) and internet outages; survive a restart of HA
        self._history_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.history")
        self._known_macs: set[str] | None = None
        # rest/user answered in this round (a failed request also yields {}, which must not seed the known MACs)
        self._known_clients_fresh = False
        self._outages: list[Outage] = []
        # Produktbilder je sysid (catalog.py), im Hintergrund nach dem Start geladen
        self.pictures: dict[str, str] = {}

    async def async_load_pictures(self) -> None:
        self.pictures = await async_load_pictures(self.hass)
        self.async_update_listeners()

    def picture(self, device: Device | None) -> str | None:
        return self.pictures.get(device.sysid) if device is not None and device.sysid else None

    async def async_load_storage(self) -> None:
        self._saved_ports = await self._port_store.async_load() or {}
        history = await self._history_store.async_load()
        if not isinstance(history, dict):
            return
        if isinstance(macs := history.get("known_macs"), list):
            self._known_macs = {str(m) for m in macs}
        outages = history.get("outages")
        for item in outages if isinstance(outages, list) else ():
            if not isinstance(item, dict):
                continue
            start = _aware(item.get("start"))
            if start is not None:
                self._outages.append(Outage(start, _aware(item.get("end"))))

    def _history(self) -> dict[str, Any]:
        return {
            # None until seeded: an empty list would count as "seeded" after a restart and report every client
            "known_macs": sorted(self._known_macs) if self._known_macs is not None else None,
            "outages": [
                {"start": o.start.isoformat(), "end": o.end.isoformat() if o.end else None} for o in self._outages
            ],
        }

    @callback
    def _save_history(self) -> None:
        self._history_store.async_delay_save(self._history, 10)

    @callback
    def _new_clients(self, data: UdrData) -> list[Client]:
        """Clients whose MAC the integration has never seen before.

        On the very first start everything already known to UniFi (rest/user) or connected counts as seen, so there
        is no burst of events. Seeding waits for a round in which stat/sta and rest/user both answered; an empty
        client list (stat/sta failed) neither seeds nor reports anything."""
        if not data.clients:
            return []
        if self._known_macs is None:
            if not self._known_clients_fresh:
                return []
            self._known_macs = set(data.clients) | set(data.known_clients)
            self._save_history()
            return []
        new = [client for mac, client in data.clients.items() if mac not in self._known_macs]
        if new:
            self._known_macs.update(client.mac for client in new)
            self._save_history()
        return new

    @callback
    def _track_outages(self, data: UdrData) -> None:
        """Start an outage when the internet status leaves "ok" and end it when it is back."""
        status = data.get("www", "status")
        if not status:
            return
        now = dt_util.utcnow()
        ongoing = self._outages[-1] if self._outages and self._outages[-1].end is None else None
        if status != "ok" and ongoing is None:
            self._outages = [o for o in self._outages if now - (o.end or now) < OUTAGE_KEEP]
            self._outages.append(Outage(now))
            self._save_history()
        elif status == "ok" and ongoing is not None:
            self._outages[-1] = Outage(ongoing.start, now)
            self._save_history()

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
                known = await self._partial(self.client.known_clients, "rest/user", None)
                self._known_clients_fresh = known is not None
                data.known_clients = known or {}
                data.gateway_settings = await self._partial(self.client.gateway_settings, "rest/setting", {})
                data.critical_events = await self._partial(self.client.critical_events, "system-log", None)
            else:
                data.wlans, data.rules, data.known_clients = old.wlans, old.rules, old.known_clients
                self._known_clients_fresh = False
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
            data.new_clients = self._new_clients(data)
            self._track_outages(data)
            data.outages = list(self._outages)
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

    async def async_command(
        self, command: Callable[[], Awaitable[Any]], *, applied: Callable[[UdrData], None] | None = None
    ) -> None:
        """Send a command to the router, then fetch everything again (debounced).

        `applied` writes what the router just accepted into the current data right away. The refresh after a command
        is debounced (a second command within 10 s waits for the cooldown) and a full round takes several seconds;
        without it the entity kept its old state meanwhile, the frontend toggle snapped back after 2 s and a second
        tap sent the opposite command."""
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
        if applied is not None and self.data is not None:
            applied(self.data)
            self.async_update_listeners()
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
        if self._known_macs is not None or self._outages:
            # Write now instead of after the save delay: a reload (new options) reads the file right away
            await self._history_store.async_save(self._history())
