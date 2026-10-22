"""Lokaler Zugriff auf UniFi OS und die UniFi-Network-App eines Dream Router 7 (ohne HA, testbar).

Anmeldung wie die Web-Oberfläche: POST /api/auth/login → Cookie `TOKEN` + Header `X-Csrf-Token`, den jede
ändernde Anfrage mitschicken muss. Beides schickt der Client selbst als Header, damit er HAs gemeinsame Session
nutzen kann (ohne eigenen Cookie-Speicher). Die Network-App hat zwei Schnittstellen:

- klassisch `/proxy/network/api/s/<site>/…`, Antwort `{"meta": {"rc": "ok"}, "data": [...]}`
- v2 `/proxy/network/v2/api/site/<site>/…`, Antwort direkt (Liste oder Objekt)

Die Pfade stammen aus der Network-Oberfläche (Stand Network 10.6 auf UniFi OS 5.1, Dream Router 7).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from json import loads as json_loads
import logging
import ssl
import time
from typing import Any

import aiohttp

from .const import HTTP_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class UdrError(Exception):
    """Dream Router nicht erreichbar oder unerwartete Antwort."""


class UdrAuthError(UdrError):
    """Anmeldung abgelehnt: Benutzername oder Passwort falsch."""


class UdrPermissionError(UdrError):
    """Angemeldet, aber dem Konto fehlen die Rechte (403), z. B. nur Lesezugriff in der Network-App."""


class UdrSiteError(UdrError):
    """Die Site gibt es nicht (api.err.NoSiteContext)."""


class UdrCertificateError(UdrError):
    """Der Dream Router zeigt ein anderes Zertifikat als das bei der Einrichtung gemerkte."""

    def __init__(self, fingerprint: str) -> None:
        super().__init__("certificate changed")
        self.fingerprint = fingerprint


def format_fingerprint(fingerprint: str) -> str:
    """SHA-256 als „AB:CD:…“ wie in Browsern."""
    return ":".join(fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2)).upper()


def as_dict(value: Any) -> dict[str, Any]:
    """Verschachteltes Feld als dict; alles andere (None, Liste, Text nach Firmware-Wechsel) als leeres dict."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def num(value: Any) -> float | None:
    """Zahl aus JSON; UniFi liefert manche Werte als Text („20.2“)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    n = num(value)
    return int(n) if n is not None else None


def _ts(value: Any) -> datetime | None:
    """Unix-Zeit in Sekunden oder Millisekunden."""
    n = num(value)
    if not n:
        return None
    if n > 10**11:
        n /= 1000
    return datetime.fromtimestamp(n, tz=UTC)


# ---------------------------------------------------------------------------------------------------------------------
# Geräte (stat/device): Dream Router selbst, Access Points, Switches


@dataclass(frozen=True, slots=True)
class Port:
    idx: int
    name: str
    media: str
    up: bool
    speed: int | None
    enabled: bool
    poe_capable: bool
    poe_mode: str | None
    poe_power: float | None
    rx_rate: float | None
    tx_rate: float | None
    is_uplink: bool
    network: str


@dataclass(frozen=True, slots=True)
class Radio:
    name: str
    band: str
    channel: int | str | None
    width: int | None
    tx_power_mode: str | None
    tx_power: float | None
    clients: int | None
    utilization: float | None


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    mac: str
    name: str
    model: str
    shortname: str
    type: str
    version: str
    ip: str
    state: int
    upgradable: bool
    upgrade_to: str | None
    uptime: int | None
    cpu: float | None
    memory: float | None
    temperature: float | None
    led_override: str | None
    led_color: str | None
    led_brightness: int | None
    locating: bool
    clients: int | None
    poe_power: float | None
    overheating: bool
    ports: dict[int, Port]
    radios: dict[str, Radio]
    raw: dict[str, Any] = field(repr=False, compare=False)
    # Hardware-Kennung (hex, z. B. "a67a" = Dream Router 7) für das Produktbild aus Ubiquitis Katalog
    sysid: str = ""

    @property
    def is_gateway(self) -> bool:
        return self.type in ("udm", "ugw", "uxg")

    @property
    def online(self) -> bool:
        # state 1 = verbunden; 0 = getrennt, 4/5/… = Update, Bereitstellung, Neustart
        return self.state == 1

    @property
    def has_led_color(self) -> bool:
        """Farbe und Helligkeit gibt es nur mit LED-Ring (hw_caps Bit 2, wie die offizielle Integration prüft);
        led_override_color steht auch bei Geräten ohne Ring im Datensatz."""
        return self.led_color is not None and bool((_int(self.raw.get("hw_caps")) or 0) & HW_CAPS_LED_RING)


def parse_port(raw: dict[str, Any], overrides: dict[int, dict[str, Any]]) -> Port:
    idx = _int(raw.get("port_idx")) or 0
    override = overrides.get(idx, {})
    # Zustand meldet port_table[].enable (abgeschaltet über Port-Sicherheit ohne erlaubte MAC, siehe disable_port)
    disabled = raw.get("enable") is False
    return Port(
        idx=idx,
        name=str(raw.get("name") or f"Port {idx}"),
        media=str(raw.get("media") or ""),
        up=bool(raw.get("up")),
        speed=_int(raw.get("speed")) if raw.get("up") else None,
        enabled=not disabled,
        poe_capable=bool(raw.get("port_poe")),
        poe_mode=str(override.get("poe_mode") or raw.get("poe_mode") or "") or None,
        poe_power=num(raw.get("poe_power")),
        rx_rate=num(raw.get("rx_bytes-r")),
        tx_rate=num(raw.get("tx_bytes-r")),
        is_uplink=bool(raw.get("is_uplink")),
        network=str(raw.get("network_name") or ""),
    )


def parse_radios(raw: dict[str, Any]) -> dict[str, Radio]:
    stats = {r.get("name"): r for r in as_list(raw.get("radio_table_stats"))}
    radios: dict[str, Radio] = {}
    for config in as_list(raw.get("radio_table")):
        band = str(config.get("radio") or "")
        if not band:
            continue
        stat = stats.get(config.get("name"), {})
        channel = stat.get("channel", config.get("channel"))
        radios[band] = Radio(
            name=str(config.get("name") or ""),
            band=band,
            channel=channel if isinstance(channel, int | str) and not isinstance(channel, bool) else None,
            width=_int(config.get("ht")),
            tx_power_mode=config.get("tx_power_mode"),
            # Tatsächliche Sendeleistung (dBm) aus der Statistik, die Einstellung steht in radio_table
            tx_power=num(stat.get("tx_power")),
            clients=_int(stat.get("num_sta")),
            utilization=num(stat.get("cu_total")),
        )
    return radios


def parse_device(raw: dict[str, Any]) -> Device:
    stats = as_dict(raw.get("system-stats"))
    overrides = {_int(o.get("port_idx")) or 0: o for o in as_list(raw.get("port_overrides"))}
    temps = [num(t.get("value")) for t in as_list(raw.get("temperatures"))]
    cpu_temp = next(
        (num(t.get("value")) for t in as_list(raw.get("temperatures")) if str(t.get("type")).lower() == "cpu"),
        None,
    )
    led_color = raw.get("led_override_color")
    return Device(
        id=str(raw.get("_id") or raw.get("device_id") or ""),
        mac=str(raw.get("mac") or "").lower(),
        name=str(raw.get("name") or raw.get("model") or raw.get("mac") or ""),
        model=str(raw.get("model") or ""),
        shortname=str(raw.get("shortname") or raw.get("model") or ""),
        type=str(raw.get("type") or ""),
        version=str(raw.get("displayable_version") or raw.get("version") or ""),
        ip=str(raw.get("lan_ip") or raw.get("ip") or ""),
        state=_int(raw.get("state")) or 0,
        upgradable=bool(raw.get("upgradable")),
        upgrade_to=raw.get("upgrade_to_firmware") or None,
        uptime=_int(raw.get("uptime")),
        cpu=num(stats.get("cpu")),
        memory=num(stats.get("mem")),
        temperature=cpu_temp if cpu_temp is not None else next((t for t in temps if t is not None), None),
        led_override=raw.get("led_override"),
        led_color=led_color if isinstance(led_color, str) and led_color.startswith("#") else None,
        led_brightness=_int(raw.get("led_override_color_brightness")),
        locating=bool(raw.get("locating")),
        clients=_int(raw.get("num_sta")),
        poe_power=num(raw.get("total_used_power")),
        overheating=bool(raw.get("overheating")),
        ports={p.idx: p for p in (parse_port(r, overrides) for r in as_list(raw.get("port_table"))) if p.idx},
        radios=parse_radios(raw),
        raw=raw,
        sysid=f"{sysid:x}" if (sysid := _int(raw.get("sysid"))) else "",
    )


# ---------------------------------------------------------------------------------------------------------------------
# Clients (stat/sta: gerade verbunden; rest/user: alle bekannten mit Name, Sperre, fester IP)


@dataclass(frozen=True, slots=True)
class Client:
    mac: str
    name: str
    hostname: str
    ip: str
    wired: bool
    network: str
    essid: str
    signal: int | None
    last_seen: datetime | None
    uptime: int | None
    rx_rate: float | None
    tx_rate: float | None
    blocked: bool
    guest: bool
    uplink_mac: str
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def display_name(self) -> str:
        return self.name or self.hostname or self.mac


def parse_client(raw: dict[str, Any]) -> Client:
    wired = bool(raw.get("is_wired"))
    return Client(
        mac=str(raw.get("mac") or "").lower(),
        name=str(raw.get("name") or ""),
        hostname=str(raw.get("hostname") or ""),
        ip=str(raw.get("ip") or raw.get("last_ip") or raw.get("fixed_ip") or ""),
        wired=wired,
        network=str(raw.get("network") or raw.get("last_connection_network_name") or ""),
        essid=str(raw.get("essid") or "") if not wired else "",
        signal=_int(raw.get("signal")) if not wired else None,
        last_seen=_ts(raw.get("last_seen")),
        uptime=_int(raw.get("uptime")),
        # Aus Sicht des Clients: rx = empfangen (Download)
        rx_rate=num(raw.get("rx_bytes-r") if not wired else raw.get("wired-rx_bytes-r")),
        tx_rate=num(raw.get("tx_bytes-r") if not wired else raw.get("wired-tx_bytes-r")),
        blocked=bool(raw.get("blocked")),
        guest=bool(raw.get("is_guest")),
        uplink_mac=str(raw.get("ap_mac") or raw.get("sw_mac") or raw.get("gw_mac") or "").lower(),
        raw=raw,
    )


# ---------------------------------------------------------------------------------------------------------------------
# Einstellungen, die sich ein- und ausschalten lassen: WLANs, Firewall-Regeln, Richtlinien, Filter, VPN …


# Bereiche des Ereignisprotokolls (wie die Oberfläche unter „Systemprotokoll“)
LOG_CATEGORIES = [
    "CLIENT_DEVICES",
    "INTERNET_AND_WAN",
    "POWER",
    "SECURITY",
    "UNIFI_DEVICES",
    "SOFTWARE_UPDATES",
    "UNIFI_ETHERNET_PORTS",
    "VPN",
]

# hw_caps-Bit für einen LED-Ring mit einstellbarer Farbe (stat/device)
HW_CAPS_LED_RING = 2

# Art einer Regel → (Schnittstelle, Pfad zum Lesen); geschaltet wird je Art in UdrClient.set_enabled
RULE_KINDS = (
    "wlan",
    "firewall_policy",
    "traffic_rule",
    "traffic_route",
    "port_forward",
    "content_filter",
    "client_policy",
    "vpn_server",
    "vpn_client",
    "qos_rule",
)


@dataclass(frozen=True, slots=True)
class Rule:
    kind: str
    id: str
    name: str
    enabled: bool
    raw: dict[str, Any] = field(repr=False, compare=False)


def _rule(kind: str, raw: dict[str, Any], name_keys: tuple[str, ...], default: bool) -> Rule | None:
    rule_id = raw.get("_id") or raw.get("id")
    if not rule_id:
        return None
    name = next((str(raw[k]) for k in (*name_keys, "name", "description") if raw.get(k)), str(rule_id))
    return Rule(kind=kind, id=str(rule_id), name=name, enabled=bool(raw.get("enabled", default)), raw=raw)


def _rules(kind: str, items: Any, *name_keys: str, default: bool = False) -> dict[str, Rule]:
    """Regeln einer Art; `default` gilt, wenn `enabled` fehlt (bei VPN-Einträgen in networkconf heißt das „an“)."""
    rules = (_rule(kind, raw, name_keys, default) for raw in as_list(items))
    return {r.id: r for r in rules if r is not None}


@dataclass(frozen=True, slots=True)
class Wlan:
    id: str
    name: str
    enabled: bool
    passphrase: str | None
    security: str
    hidden: bool
    guest: bool
    network_id: str
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def qr_payload(self) -> str:
        """Inhalt des WLAN-QR-Codes (Format der Android/iOS-Kameras)."""

        def esc(value: str) -> str:
            for ch in '\\;,:"':
                value = value.replace(ch, f"\\{ch}")
            return value

        security = "nopass" if self.security == "open" or not self.passphrase else "WPA"
        password = f"P:{esc(self.passphrase)};" if self.passphrase and security != "nopass" else ""
        hidden = "H:true;" if self.hidden else ""
        return f"WIFI:T:{security};S:{esc(self.name)};{password}{hidden};"


def parse_wlan(raw: dict[str, Any]) -> Wlan:
    return Wlan(
        id=str(raw.get("_id") or ""),
        name=str(raw.get("name") or ""),
        enabled=bool(raw.get("enabled")),
        passphrase=raw.get("x_passphrase") or None,
        security=str(raw.get("security") or ""),
        hidden=bool(raw.get("hide_ssid")),
        guest=bool(raw.get("is_guest")),
        network_id=str(raw.get("networkconf_id") or ""),
        raw=raw,
    )


# ---------------------------------------------------------------------------------------------------------------------
# Gesamtstand


@dataclass(slots=True)
class UdrData:
    # Bereich ("wlan", "wan", "www", "lan", "vpn") → Rohdaten aus stat/health
    health: dict[str, dict[str, Any]]
    devices: dict[str, Device] = field(default_factory=dict)
    clients: dict[str, Client] = field(default_factory=dict)
    known_clients: dict[str, Client] = field(default_factory=dict)
    wlans: dict[str, Wlan] = field(default_factory=dict)
    rules: dict[str, dict[str, Rule]] = field(default_factory=dict)
    # Einstellungen des Gateways (rest/setting, key "usg"): UPnP u. a.
    gateway_settings: dict[str, Any] = field(default_factory=dict)
    # Kritische Ereignisse der letzten 24 Stunden (None = Protokoll nicht lesbar)
    critical_events: list[dict[str, Any]] | None = None
    # Langsamer Teil
    sysinfo: dict[str, Any] = field(default_factory=dict)
    os_firmware: dict[str, Any] = field(default_factory=dict)
    # Display (lcmSettings: enabled, color "RRGGBB", brightness 1–100), Nachtmodus (onMinute/offMinute), hasBezel
    os_settings: dict[str, Any] = field(default_factory=dict)
    network_app: dict[str, Any] = field(default_factory=dict)
    # Speedtest per Button/Automatik gestartet (Zeitpunkt), Ergebnis steht noch aus
    speedtest_started_at: datetime | None = None

    def get(self, subsystem: str, key: str) -> Any:
        return self.health.get(subsystem, {}).get(key)

    @property
    def gateway(self) -> Device | None:
        return next((d for d in self.devices.values() if d.is_gateway), None)

    @property
    def gateway_raw(self) -> dict[str, Any]:
        gateway = self.gateway
        return gateway.raw if gateway else {}

    @property
    def speedtest_status(self) -> dict[str, Any]:
        """speedtest-status des Gateways; status_summary: 1 = läuft, 2 = fertig (0 = nie gelaufen)."""
        return as_dict(self.gateway_raw.get("speedtest-status"))

    @property
    def speedtest_running(self) -> bool:
        """Selbst gestartet und noch ohne Ergebnis, oder der Router meldet einen laufenden Test.

        Maßgeblich ist stat/health („Idle“ = keiner läuft): speedtest-status im Gerätedatensatz bleibt nach einem
        Neustart des Routers als Platzhalter (lauter Nullen, rundate 0) auf status_summary 1 stehen."""
        if self.speedtest_started_at is not None:
            return True
        status = self.get("www", "speedtest_status")
        if isinstance(status, str) and status:
            return status.lower() != "idle"
        gateway = self.speedtest_status
        return gateway.get("status_summary") == 1 and bool(gateway.get("rundate"))

    def active_wan(self) -> tuple[str, dict[str, Any]] | None:
        """(Name wie in UniFi, z. B. „WAN1 (SFP+ 1)“, Port-Daten) des aktiven WAN-Anschlusses."""
        gateway = self.gateway_raw
        status = as_dict(gateway.get("last_wan_status"))
        for number in range(1, 5):
            port = gateway.get(f"wan{number}")
            group = "WAN" if number == 1 else f"WAN{number}"
            if not isinstance(port, dict) or not port or (status and status.get(group) != "online"):
                continue
            media = port.get("media") or port.get("type") or ""
            label = f"WAN{number} ({media} {port.get('num_port', number)})" if media else f"WAN{number}"
            return label, port
        return None

    def geo(self) -> dict[str, Any]:
        """Provider und Standort des aktiven Anschlusses."""
        gateway = self.gateway_raw
        for key in ("geo_info", "active_geo_info", "last_geo_info"):
            info = as_dict(gateway.get(key)).get("WAN")
            if isinstance(info, dict) and info:
                return info
        return {}


# ---------------------------------------------------------------------------------------------------------------------


class UdrClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        site: str = "default",
        *,
        verify_ssl: bool = False,
        fingerprint: str | None = None,
    ) -> None:
        self._session = session
        self.host = host
        self.site = site
        # UniFi OS kommt mit selbstsigniertem Zertifikat. Standard: nur das bei der Einrichtung gemerkte Zertifikat
        # annehmen (SHA-256, „trust on first use“); mit der Option „Zertifikat prüfen“ gegen die CA-Liste.
        self.verify_ssl = verify_ssl
        self.fingerprint = None if verify_ssl else fingerprint
        self._ssl: bool | aiohttp.Fingerprint = (
            True if verify_ssl else aiohttp.Fingerprint(bytes.fromhex(fingerprint)) if fingerprint else False
        )
        self.username = username
        self._password = password
        # Header aus der Anmeldung (Cookie TOKEN + X-Csrf-Token); None = nicht angemeldet
        self._auth: dict[str, str] | None = None
        self._login_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    # --- Transport ---------------------------------------------------------------------------------------------------

    async def login(self, *, force: bool = False) -> None:
        async with self._login_lock:
            if self._auth is not None and not force:
                return  # parallele Anfrage hat sich schon angemeldet
            try:
                async with asyncio.timeout(HTTP_TIMEOUT):
                    resp = await self._session.post(
                        f"{self.base_url}/api/auth/login",
                        json={"username": self.username, "password": self._password, "rememberMe": True},
                        ssl=self._ssl,
                        allow_redirects=False,
                    )
                    async with resp:
                        # Falsches Passwort meldet UniFi OS mit 403 (nicht 401), Sperre nach Fehlversuchen mit 429
                        if resp.status in (400, 401, 403):
                            raise UdrAuthError("login rejected")
                        if resp.status != 200:
                            raise UdrError(f"login: HTTP {resp.status}")
                        auth: dict[str, str] = {}
                        if token := resp.cookies.get("TOKEN"):
                            auth["Cookie"] = f"TOKEN={token.value}"
                        if csrf := resp.headers.get("X-Csrf-Token") or resp.headers.get("X-Updated-Csrf-Token"):
                            auth["X-Csrf-Token"] = csrf
                        self._auth = auth
            except aiohttp.ServerFingerprintMismatch as err:
                raise UdrCertificateError(err.got.hex()) from err
            except (TimeoutError, aiohttp.ClientError) as err:
                raise UdrError(f"cannot connect: {err}") from err

    async def _request(self, method: str, path: str, json: Any = None, *, relogin: bool = True) -> Any:
        if self._auth is None:
            await self.login()
        # Den beim Senden benutzten Anmeldestand merken: nur er darf bei 401 verworfen werden
        used = self._auth
        headers = dict(used or {})
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                resp = await self._session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    headers=headers,
                    ssl=self._ssl,
                    # Startet der Router gerade, leitet UniFi OS auf seine Oberfläche um: „nicht erreichbar“
                    allow_redirects=False,
                )
                async with resp:
                    text = await resp.text()
                    if resp.status == 401 and relogin:
                        # Sitzung abgelaufen: einmal neu anmelden. Laufen mehrere Anfragen parallel in den 401,
                        # meldet sich nur die erste neu an; die anderen wiederholen nur (sonst sperrt UniFi OS mit 429)
                        if self._auth is used:
                            self._auth = None
                        return await self._request(method, path, json, relogin=False)
                    if "api.err.NoSiteContext" in text:
                        raise UdrSiteError(f"site {self.site} unknown")
                    if resp.status == 401:
                        # Die Anmeldung selbst klappte gerade, nur die Anfrage nicht: Network-App startet noch
                        raise UdrError(f"{path}: session not accepted (HTTP 401)")
                    if resp.status == 403:
                        raise UdrPermissionError(f"{path}: permission denied (HTTP 403)")
                    if resp.status >= 300:
                        raise UdrError(f"{path}: HTTP {resp.status} {_error_text(text)}".rstrip())
                    if (updated := resp.headers.get("X-Updated-Csrf-Token")) and self._auth is not None:
                        self._auth["X-Csrf-Token"] = updated
        except aiohttp.ServerFingerprintMismatch as err:
            raise UdrCertificateError(err.got.hex()) from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise UdrError(f"cannot connect: {err}") from err
        if not text.strip():
            return None
        try:
            return json_loads(text)
        except ValueError as err:
            raise UdrError(f"{path}: no JSON response") from err

    async def _api(self, method: str, path: str, json: Any = None) -> list[dict[str, Any]]:
        """Klassische Schnittstelle: Inhalt von `data`, Fehler aus `meta`."""
        body = await self._request(method, f"/proxy/network/api/s/{self.site}/{path}", json)
        meta = as_dict(body.get("meta")) if isinstance(body, dict) else {}
        if meta.get("rc") != "ok":
            raise UdrError(f"{path}: {meta.get('msg') or 'unexpected response'}")
        return as_list(body.get("data"))

    async def _v2(self, method: str, path: str, json: Any = None) -> Any:
        return await self._request(method, f"/proxy/network/v2/api/site/{self.site}/{path}", json)

    async def identify(self) -> tuple[str, str, str]:
        """Ohne Anmeldung: (MAC, Name, Modell) aus /api/system – bestätigt einen im Netz gefundenen Dream Router."""
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                resp = await self._session.get(f"{self.base_url}/api/system", ssl=self._ssl, allow_redirects=False)
                async with resp:
                    if resp.status != 200:
                        raise UdrError(f"/api/system: HTTP {resp.status}")
                    text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise UdrError(f"cannot connect: {err}") from err
        try:
            body = json_loads(text)
        except ValueError as err:
            raise UdrError("/api/system: no JSON response") from err
        system = as_dict(body)
        model = str(as_dict(system.get("hardware")).get("shortname") or "")
        mac = str(system.get("mac") or "").replace(":", "").lower()
        if not model.upper().startswith("UDR") or len(mac) != 12:
            raise UdrError("not a Dream Router")
        return ":".join(mac[i : i + 2] for i in range(0, 12, 2)), str(system.get("name") or model), model

    async def certificate_fingerprint(self, context: ssl.SSLContext) -> str:
        """SHA-256 des Zertifikats, das der Router gerade zeigt (ohne Prüfung gelesen, nur zum Merken)."""
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                _reader, writer = await asyncio.open_connection(self.host, 443, ssl=context)
        except (TimeoutError, OSError) as err:
            raise UdrError(f"cannot connect: {err}") from err
        try:
            ssl_object = writer.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        finally:
            writer.close()
        if not der:
            raise UdrError("no certificate presented")
        return hashlib.sha256(der).hexdigest()

    # --- Lesen -------------------------------------------------------------------------------------------------------

    async def health(self) -> dict[str, dict[str, Any]]:
        return {d["subsystem"]: d for d in await self._api("GET", "stat/health") if "subsystem" in d}

    async def devices(self) -> dict[str, Device]:
        devices = (parse_device(d) for d in await self._api("GET", "stat/device"))
        return {d.mac: d for d in devices if d.mac}

    async def clients(self) -> dict[str, Client]:
        clients = (parse_client(c) for c in await self._api("GET", "stat/sta"))
        return {c.mac: c for c in clients if c.mac}

    async def known_clients(self) -> dict[str, Client]:
        """Alle bekannten Clients mit Namen (auch gerade nicht verbundene); für Sperre und Auswahl im Optionsdialog."""
        clients = (parse_client(c) for c in await self._api("GET", "rest/user"))
        return {c.mac: c for c in clients if c.mac}

    async def wlans(self) -> dict[str, Wlan]:
        wlans = (parse_wlan(w) for w in await self._api("GET", "rest/wlanconf"))
        return {w.id: w for w in wlans if w.id}

    async def rules(self) -> dict[str, dict[str, Rule]]:
        """Alle schaltbaren Regeln; was diese Network-Version nicht kennt, bleibt leer statt abzubrechen."""
        names = ("firewall_policy", "traffic_rule", "traffic_route", "port_forward", "content_filter", "client_policy")
        results = await asyncio.gather(
            self._v2("GET", "firewall-policies"),
            self._v2("GET", "trafficrules"),
            self._v2("GET", "trafficroutes"),
            self._api("GET", "rest/portforward"),
            self._v2("GET", "content-filtering"),
            self._v2("GET", "object-oriented-network-configs"),
            self._api("GET", "rest/networkconf"),
            self._v2("GET", "qos-rules"),
            return_exceptions=True,
        )
        for name, result in zip((*names, "networkconf", "qos_rule"), results, strict=True):
            if isinstance(result, UdrAuthError | UdrCertificateError):
                raise result
            if isinstance(result, BaseException):
                _LOGGER.debug("%s not available: %s", name, result)
        firewall, traffic_rules, routes, forwards, filters, policies, networks, qos = (
            r if not isinstance(r, BaseException) else [] for r in results
        )
        networks = as_list(networks)
        return {
            # Vordefinierte Regeln (Standard-Zonenregeln) lassen sich nicht schalten
            "firewall_policy": _rules("firewall_policy", [p for p in as_list(firewall) if not p.get("predefined")]),
            "traffic_rule": _rules("traffic_rule", traffic_rules, "description"),
            "traffic_route": _rules("traffic_route", routes, "description"),
            "port_forward": _rules("port_forward", forwards),
            "content_filter": _rules("content_filter", filters),
            "client_policy": _rules("client_policy", policies),
            "vpn_server": _rules(
                "vpn_server",
                [n for n in networks if n.get("purpose") in ("remote-user-vpn", "vpn-server")],
                default=True,
            ),
            "vpn_client": _rules("vpn_client", [n for n in networks if n.get("purpose") == "vpn-client"], default=True),
            "qos_rule": _rules("qos_rule", qos),
        }

    async def gateway_settings(self) -> dict[str, Any]:
        settings = await self._api("GET", "get/setting/usg")
        return next((s for s in settings if s.get("key") == "usg"), {})

    async def critical_events(self) -> list[dict[str, Any]]:
        """Kritische Ereignisse der letzten 24 Stunden (Protokoll der Network-App, Schwere hoch und sehr hoch).

        Ersetzt die Alarme (stat/alarm gibt es seit Network 10 nicht mehr)."""
        now = int(time.time() * 1000)
        body = await self._v2(
            "POST",
            "system-log/critical",
            {
                "timestampFrom": now - 24 * 3600 * 1000,
                "timestampTo": now,
                "severities": ["HIGH", "VERY_HIGH"],
                "categories": LOG_CATEGORIES,
                "type": "GENERAL",
                "pageNumber": 0,
                "pageSize": 100,
                "searchText": "",
            },
        )
        if isinstance(body, dict):
            body = body.get("data") or body.get("logs")
        return as_list(body)

    async def sysinfo(self) -> dict[str, Any]:
        data = await self._api("GET", "stat/sysinfo")
        return data[0] if data else {}

    async def os_status(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """(Firmware von UniFi OS, Eintrag der Network-App, Display/Nachtmodus) aus /api/system."""
        system = as_dict(await self._request("GET", "/api/system"))
        firmware = as_dict(system.get("firmware"))
        network: dict[str, Any] = next(
            (c for c in as_list(as_dict(system.get("apps")).get("controllers")) if c.get("name") == "network"), {}
        )
        settings: dict[str, Any] = {k: as_dict(system[k]) for k in ("lcmSettings", "nightMode") if k in system}
        # Farbe von Display/LED nur bei Konsolen mit „Bezel“ (so entscheidet die UniFi-OS-Oberfläche), beim UDR7 nicht
        settings["hasBezel"] = bool(as_dict(system.get("features")).get("hasBezel"))
        return firmware, network, settings

    # --- Geräte ------------------------------------------------------------------------------------------------------

    async def _devmgr(self, cmd: str, **params: Any) -> None:
        await self._api("POST", "cmd/devmgr", {"cmd": cmd, **params})

    async def restart_device(self, mac: str) -> None:
        await self._devmgr("restart", mac=mac, reboot_type="soft")

    async def locate(self, mac: str, on: bool) -> None:
        await self._devmgr("set-locate" if on else "unset-locate", mac=mac)

    async def upgrade_device(self, mac: str) -> None:
        await self._devmgr("upgrade", mac=mac)

    async def power_cycle_port(self, mac: str, port: int) -> None:
        await self._devmgr("power-cycle", mac=mac, port_idx=port)

    async def run_speedtest(self) -> None:
        await self._devmgr("speedtest")

    async def update_device(self, device: Device, changes: dict[str, Any]) -> None:
        """Einstellungen eines Geräts ändern (LED, Ports, Funk); nur die geänderten Felder."""
        await self._api("PUT", f"rest/device/{device.id}", changes)

    async def set_led(
        self, device: Device, *, on: bool, color: str | None = None, brightness: int | None = None
    ) -> None:
        changes: dict[str, Any] = {"led_override": "on" if on else "off"}
        if color is not None:
            changes["led_override_color"] = color
        if brightness is not None:
            changes["led_override_color_brightness"] = brightness
        await self.update_device(device, changes)

    async def set_port(self, device: Device, port: int, **settings: Any) -> None:
        """Port-Einstellung (forward, poe_mode …) in port_overrides setzen; die Liste wird als Ganzes ersetzt,
        die übrigen Ports bleiben, wie sie sind."""
        overrides = [dict(o) for o in as_list(device.raw.get("port_overrides"))]
        current = next((o for o in overrides if _int(o.get("port_idx")) == port), None)
        if current is None:
            # Neuer Eintrag: Port-Profil aus port_table übernehmen, sonst setzt die Network-App es zurück
            table = next((p for p in as_list(device.raw.get("port_table")) if _int(p.get("port_idx")) == port), {})
            current = {"port_idx": port}
            if table.get("portconf_id"):
                current["portconf_id"] = table["portconf_id"]
            overrides.append(current)
        current.update(settings)
        await self.update_device(device, {"port_overrides": overrides})

    async def disable_port(self, device: Device, port: int) -> dict[str, Any] | None:
        """Port sperren wie die offizielle UniFi-Integration (port_security_enabled ohne erlaubte MAC).

        Die Network-App baut den Eintrag dabei um (Netz entfernt, forward "disabled"); zurück geht es nur über den
        alten Eintrag. Deshalb gibt die Methode ihn zurück (None = Port hatte keinen eigenen Eintrag)."""
        previous = next(
            (dict(o) for o in as_list(device.raw.get("port_overrides")) if _int(o.get("port_idx")) == port), None
        )
        await self.set_port(device, port, port_security_enabled=True)
        return previous

    async def enable_port(self, device: Device, port: int, previous: dict[str, Any] | None) -> None:
        """Port wieder freigeben: den gemerkten Eintrag zurückschreiben; ohne ihn den Eintrag entfernen, dann gilt
        die Standardeinstellung des Ports (alle Netze)."""
        overrides = [dict(o) for o in as_list(device.raw.get("port_overrides")) if _int(o.get("port_idx")) != port]
        if previous is not None and not previous.get("port_security_enabled"):
            overrides.append(dict(previous))
        await self.update_device(device, {"port_overrides": overrides})

    async def set_radio(self, device: Device, band: str, **settings: Any) -> None:
        """Funk-Einstellung (tx_power_mode …) eines Bands; die anderen Bänder bleiben, wie sie sind."""
        radios = [dict(r) for r in as_list(device.raw.get("radio_table"))]
        for radio in radios:
            if radio.get("radio") == band:
                radio.update(settings)
        await self.update_device(device, {"radio_table": radios})

    # --- Clients -----------------------------------------------------------------------------------------------------

    async def _stamgr(self, cmd: str, mac: str, **params: Any) -> None:
        await self._api("POST", "cmd/stamgr", {"cmd": cmd, "mac": mac.lower(), **params})

    async def block_client(self, mac: str, blocked: bool) -> None:
        await self._stamgr("block-sta" if blocked else "unblock-sta", mac)

    async def reconnect_client(self, mac: str) -> None:
        await self._stamgr("kick-sta", mac)

    async def authorize_guest(self, mac: str, minutes: int | None = None) -> None:
        await self._stamgr("authorize-guest", mac, **({"minutes": minutes} if minutes else {}))

    async def unauthorize_guest(self, mac: str) -> None:
        await self._stamgr("unauthorize-guest", mac)

    # --- Einstellungen -----------------------------------------------------------------------------------------------

    async def set_wlan(self, wlan: Wlan, **changes: Any) -> None:
        await self._api("PUT", f"rest/wlanconf/{wlan.id}", changes)

    async def set_enabled(self, rule: Rule, enabled: bool) -> None:
        """Regel ein- oder ausschalten; v2-Objekte vollständig zurückschreiben (die Oberfläche macht es auch so)."""
        body = {**rule.raw, "enabled": enabled}
        match rule.kind:
            case "port_forward":
                await self._api("PUT", f"rest/portforward/{rule.id}", body)
            case "vpn_server" | "vpn_client":
                await self._api("PUT", f"rest/networkconf/{rule.id}", body)
            case "firewall_policy":
                await self._v2("PUT", f"firewall-policies/{rule.id}", body)
            case "traffic_rule":
                await self._v2("PUT", f"trafficrules/{rule.id}", body)
            case "traffic_route":
                await self._v2("PUT", f"trafficroutes/{rule.id}", body)
            case "content_filter":
                await self._v2("PUT", f"content-filtering/{rule.id}", body)
            case "client_policy":
                await self._v2("PUT", f"object-oriented-network-config/{rule.id}", body)
            case "qos_rule":
                await self._v2("PUT", f"qos-rules/{rule.id}", body)
            case _:
                raise UdrError(f"unknown rule kind {rule.kind}")

    async def set_gateway_setting(self, settings: dict[str, Any], **changes: Any) -> None:
        await self._api("PUT", "set/setting/usg", {"_id": settings.get("_id"), "key": "usg", **changes})

    # --- UniFi OS ----------------------------------------------------------------------------------------------------

    async def update_os(self) -> None:
        await self._request("POST", "/api/firmware/update")

    async def update_network_app(self) -> None:
        await self._request("POST", "/api/controllers/network/update")

    async def set_os(self, changes: dict[str, Any]) -> None:
        """UniFi-OS-Einstellungen ändern wie die Oberfläche (PATCH /api/system, nur die geänderten Teile), z. B.
        {"lcmSettings": {"brightness": 60}} oder {"nightMode": {"onMinute": 1260, "offMinute": 480}}."""
        await self._request("PATCH", "/api/system", changes)

    async def reboot_console(self) -> None:
        await self._request("POST", "/api/system/reboot")


def _error_text(text: str) -> str:
    """Kurze Fehlermeldung aus einer Antwort (meta.msg der klassischen, message der v2-Schnittstelle)."""
    try:
        body = json_loads(text)
    except ValueError:
        return ""
    if isinstance(body, dict):
        meta = as_dict(body.get("meta"))
        return str(meta.get("msg") or body.get("message") or body.get("code") or "")
    return ""
