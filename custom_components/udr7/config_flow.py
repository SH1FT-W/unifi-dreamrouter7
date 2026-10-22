"""Einrichtung: Adresse des Dream Routers und ein lokales UniFi-OS-Konto (kein UI-Konto mit Zwei-Faktor).

Ohne „Zertifikat prüfen“ merkt sich der Flow zuerst das Zertifikat des Routers und schickt die Zugangsdaten nur an
dieses (trust on first use); später gilt nur noch dieses Zertifikat.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
import voluptuous as vol

from . import async_read_fingerprint, create_client, verify_ssl_option
from .api import UdrAuthError, UdrCertificateError, UdrError, UdrPermissionError, UdrSiteError
from .const import (
    CONF_AWAY_SECONDS,
    CONF_EXPECTED_DOWNLOAD,
    CONF_EXPECTED_UPLOAD,
    CONF_FINGERPRINT,
    CONF_SITE,
    CONF_SPEEDTEST_MINUTES,
    CONF_TOLERANCE,
    CONF_TRACKED_CLIENTS,
    DEFAULT_AWAY_SECONDS,
    DEFAULT_HOST,
    DEFAULT_SITE,
    DEFAULT_SPEEDTEST_MINUTES,
    DEFAULT_TOLERANCE,
    DOMAIN,
)

_PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


def _split(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Zugangsdaten (entry.data) und Optionen (entry.options) aus einer Formulareingabe trennen."""
    data = dict(user_input)
    verify = bool(data.pop(CONF_VERIFY_SSL, False))
    return data, {CONF_VERIFY_SSL: verify}


async def validate(
    hass: HomeAssistant, data: dict[str, Any], *, verify_ssl: bool = False
) -> tuple[str | None, str | None, str]:
    """Anmelden und Geräte lesen → (Fehler, MAC des Gateways, Name).

    Ohne CA-Prüfung und ohne gemerktes Zertifikat liest es zuerst den Fingerabdruck und legt ihn in `data` ab."""
    if verify_ssl:
        data.pop(CONF_FINGERPRINT, None)
    elif not data.get(CONF_FINGERPRINT):
        try:
            data[CONF_FINGERPRINT] = await async_read_fingerprint(hass, data[CONF_HOST])
        except UdrError:
            return "cannot_connect", None, ""
    client = create_client(hass, data, verify_ssl=verify_ssl)
    try:
        devices = await client.devices()
    except UdrAuthError:
        return "invalid_auth", None, ""
    except UdrSiteError:
        return "unknown_site", None, ""
    except UdrPermissionError:
        return "no_permission", None, ""
    except UdrCertificateError:
        return "cert_changed", None, ""
    except UdrError:
        return "cannot_connect", None, ""
    gateway = next((d for d in devices.values() if d.is_gateway), None)
    if gateway is None:
        return "no_gateway", None, ""
    return None, format_mac(gateway.mac), gateway.name


class UdrConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host = ""
        self._name = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> UdrOptionsFlow:
        return UdrOptionsFlow()

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Von HAs DHCP-Erkennung gefunden (ARP-Tabelle: Ubiquiti-MAC, Rückwärts-DNS „unifi“); /api/system bestätigt
        ohne Anmeldung, dass es ein Dream Router ist, und liefert die MAC des Geräts (die unique_id)."""
        host = discovery_info.ip
        try:
            mac, name, _model = await create_client(
                self.hass, {CONF_HOST: host, CONF_USERNAME: "", CONF_PASSWORD: ""}
            ).identify()
        except UdrError:
            return self.async_abort(reason="not_udr")
        await self.async_set_unique_id(format_mac(mac))
        # Keine Adresse übernehmen: der Router hat eine je Netz, eingerichtet ist die, die der Nutzer gewählt hat
        self._abort_if_unique_id_configured()
        self._host, self._name = host, name
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, options = _split({CONF_HOST: self._host, CONF_SITE: DEFAULT_SITE, **user_input})
            error, mac, name = await validate(self.hass, data, verify_ssl=options[CONF_VERIFY_SSL])
            if error is None and mac == self.unique_id:
                return self.async_create_entry(title=name, data=data, options=options)
            errors["base"] = error or "wrong_device"
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): _PASSWORD,
                    vol.Optional(CONF_VERIFY_SSL, default=False): bool,
                }
            ),
            description_placeholders={"name": self._name, "host": self._host},
            errors=errors,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, options = _split(user_input)
            error, mac, name = await validate(self.hass, data, verify_ssl=options[CONF_VERIFY_SSL])
            if error is None and mac:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured(updates={CONF_HOST: data[CONF_HOST]})
                return self.async_create_entry(title=name, data=data, options=options)
            errors["base"] = error or "unknown"
        defaults = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)): str,
                    vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, vol.UNDEFINED)): str,
                    vol.Required(CONF_PASSWORD): _PASSWORD,
                    vol.Required(CONF_SITE, default=defaults.get(CONF_SITE, DEFAULT_SITE)): str,
                    vol.Optional(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, False)): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Neues Passwort, z. B. nachdem es in UniFi OS geändert wurde."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            error, mac, _name = await validate(self.hass, data, verify_ssl=verify_ssl_option(entry))
            if error is None:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                    vol.Required(CONF_PASSWORD): _PASSWORD,
                }
            ),
            description_placeholders={"name": entry.title},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Neue Adresse, anderes Konto oder andere Site."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            # Neue Adresse oder neu aufgesetzter Router: Zertifikat neu merken
            data.pop(CONF_FINGERPRINT, None)
            error, mac, _name = await validate(self.hass, data, verify_ssl=verify_ssl_option(entry))
            if error is None:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        current = user_input or entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
                    vol.Required(CONF_USERNAME, default=current.get(CONF_USERNAME, "")): str,
                    vol.Required(CONF_PASSWORD): _PASSWORD,
                    vol.Required(CONF_SITE, default=current.get(CONF_SITE, DEFAULT_SITE)): str,
                }
            ),
            errors=errors,
        )


class UdrOptionsFlow(OptionsFlow):
    """Clients für Anwesenheit/Sperre, Speedtest, gebuchte Geschwindigkeit und Zertifikatsprüfung."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            verify = bool(user_input.get(CONF_VERIFY_SSL, False))
            error = None
            if verify != verify_ssl_option(entry):
                # Prüfen an einer Kopie: ein gemerkter Fingerabdruck bleibt im Eintrag
                error, _mac, _name = await validate(self.hass, dict(entry.data), verify_ssl=verify)
            if error is None:
                return self.async_create_entry(data=user_input)
            errors["base"] = error
        options = user_input or entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TRACKED_CLIENTS, default=list(options.get(CONF_TRACKED_CLIENTS, []))
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._client_options(), multiple=True, sort=True, custom_value=True
                        )
                    ),
                    vol.Required(
                        CONF_AWAY_SECONDS, default=int(options.get(CONF_AWAY_SECONDS, DEFAULT_AWAY_SECONDS))
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=0, max=3600, step=30, unit_of_measurement="s")
                    ),
                    vol.Required(
                        CONF_SPEEDTEST_MINUTES,
                        default=int(options.get(CONF_SPEEDTEST_MINUTES, DEFAULT_SPEEDTEST_MINUTES)),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=15, max=1440, step=15, unit_of_measurement="min")
                    ),
                    vol.Optional(
                        CONF_EXPECTED_DOWNLOAD, default=float(options.get(CONF_EXPECTED_DOWNLOAD) or 0)
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=100000, unit_of_measurement="Mbit/s", mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(
                        CONF_EXPECTED_UPLOAD, default=float(options.get(CONF_EXPECTED_UPLOAD) or 0)
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=100000, unit_of_measurement="Mbit/s", mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_TOLERANCE, default=float(options.get(CONF_TOLERANCE, DEFAULT_TOLERANCE))
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=50, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(CONF_VERIFY_SSL, default=bool(options.get(CONF_VERIFY_SSL, False))): bool,
                }
            ),
            errors=errors,
        )

    def _client_options(self) -> list[selector.SelectOptionDict]:
        """Bekannte Clients mit Namen zuerst; ausgewählte bleiben in der Liste, auch wenn sie unbekannt sind."""
        data = self.config_entry.runtime_data.data
        clients = {**data.known_clients, **data.clients}
        result = [
            selector.SelectOptionDict(value=mac, label=f"{c.display_name} ({mac})")
            for mac, c in clients.items()
            if c.name or c.hostname
        ]
        listed = {o["value"] for o in result}
        result.extend(
            selector.SelectOptionDict(value=mac, label=mac)
            for mac in self.config_entry.options.get(CONF_TRACKED_CLIENTS, [])
            if mac not in listed
        )
        return result
