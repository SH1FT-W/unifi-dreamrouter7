# UniFi Dream Router 7 – Projektregeln

HA-Integration (`custom_components/udr7`, Domain `udr7`) für einen UniFi Dream Router 7 über UniFi OS und die
UniFi-Network-App: Werte des Network-Dashboards und Steuerung (WLAN, Firewall, Richtlinien, Filter, VPN, Ports, PoE,
LED, Funk, Clients). Access Points und Switches sind Untergeräte (via_device) wie die Platten bei UNAS. Ersetzt
UniFi Network Plus (übernimmt dessen entity_ids, `const.LEGACY_ENTITY_IDS`, nie ändern) und die offizielle
UniFi-Integration. Entwickelt und geprüft an einem UDR7 mit U7 Lite und USW Flex 2.5G.

- Repo ist **öffentlich**: nichts Persönliches ins Repo (keine Namen, echten IPs/MACs, Seriennummern, WLAN-Namen,
  Regelnamen, Heimnetz-Details, lokalen Pfade). Testdaten bleiben fiktiv (192.0.2.x, 203.0.113.7, 2001:db8::,
  aa:bb:cc:…, „Example ISP“ ASN 64500).
- README, CHANGELOG, Release-Notizen, Issue- und Commit-Texte auf Englisch, ohne Gedankenstriche im Fließtext.
  Code-Kommentare deutsch.
- Lokal committen ok, Push und Releases nur nach Rückfrage. Prod-HA nur auf ausdrücklichen Wunsch anfassen.
- Qualität: Platin der HA Integration Quality Scale ist die Untergrenze (`quality_scale.yaml` mitpflegen).
- Live-Tests am echten Router nur mit dem Besitzer: harmlose Schaltbefehle (LED, Gerät suchen, freier Port) ok,
  nichts, was das Internet unterbricht (WAN, Firewall, Neustart, Updates, Speedtest belastet den Anschluss).

## Version
Kalender-Schema **YY.M.N** ohne v, nächste Nummer aus `tools/next-version`, nie von Hand. CHANGELOG
`## YY.M.N – DD.MM.YYYY` (englisch), offene Punkte unter `## Unreleased`.

## Schnittstelle (22.10.2026, Network 10.6.106 auf UniFi OS 5.1.33)

- Anmeldung `POST /api/auth/login` wie UNAS (Cookie `TOKEN`, Header `X-Csrf-Token`, falsches Passwort 403,
  401 → einmal neu anmelden mit Login-Lock). Zertifikat-Pinning wie UNAS (`entry.data["fingerprint"]`).
- Klassisch `/proxy/network/api/s/<site>/…` → `{"meta":{"rc"}, "data":[…]}`; v2 `/proxy/network/v2/api/site/<site>/…`
  → direkt. Unbekannte Site: `api.err.NoSiteContext`.
- Lesen: `stat/health`, `stat/device` (alle Geräte), `stat/sta`, `rest/user`, `rest/wlanconf`, v2
  `firewall-policies`, `trafficrules`, `trafficroutes`, `content-filtering`, `object-oriented-network-configs`
  (Policy Engine), `qos-rules`, `rest/portforward`, `rest/networkconf` (VPN über `purpose`, fehlendes `enabled` = an),
  `get/setting/usg`, `stat/sysinfo`, v2 `POST system-log/critical` (kritische Ereignisse; `stat/alarm` gibt es seit
  Network 10 nicht mehr, 404), UniFi OS `/api/system` (öffentlich: `mac`, `hardware.shortname`; angemeldet:
  `firmware.latest`, `apps.controllers[name=network].updateAvailable`, `lcmSettings` = Display, `nightMode`).
- Schalten: v2-Objekte vollständig zurück-PUTten mit geändertem `enabled` (`firewall-policies/<id>`,
  `trafficrules/<id>`, `trafficroutes/<id>`, `content-filtering/<id>`, `object-oriented-network-config/<id>`,
  `qos-rules/<id>`); `rest/portforward/<id>`, `rest/networkconf/<id>` ebenfalls ganz; `rest/wlanconf/<id>` teilweise
  (`enabled`, `x_passphrase`); `set/setting/usg` mit `_id`, `key`; Geräte `PUT rest/device/<_id>` (led_override,
  led_override_color, led_override_color_brightness, radio_table ganz, port_overrides ganz; PoE = `poe_mode`
  auto/off, neue Einträge mit `portconf_id`); `cmd/devmgr` (restart soft,
  set-/unset-locate, upgrade, power-cycle mit port_idx, speedtest), `cmd/stamgr` (block-sta, unblock-sta, kick-sta,
  authorize-guest, unauthorize-guest), `POST /api/controllers/network/update`, UniFi OS `PATCH /api/system`
  (`lcmSettings` enabled/color "RRGGBB"/brightness, `nightMode` onMinute/offMinute; braucht Super Admin, sonst 403).
- Port sperren (live 22.10. geprüft): `forward: "disabled"` setzt die Network-App still auf „all“ zurück. Wie die
  offizielle Integration `port_security_enabled: true`; die App baut den Eintrag dann um (forward disabled, Netz leer)
  und `port_security_enabled: false` gibt den Port NICHT wieder frei. Deshalb merkt sich der Coordinator den Eintrag
  vor dem Sperren (Store `udr7.<entry_id>.ports`) und schreibt ihn beim Freigeben zurück; ohne gemerkten Stand fliegt
  der Eintrag raus (Standard des Ports). Zustand aus `port_table[].enable`.
- Erkennung: Router meldet sich weder per mDNS noch SSDP. HAs DHCP-Erkennung scannt aber die ARP-Tabelle mit
  Rückwärts-DNS (Router antwortet mit `unifi.localdomain`) → `dhcp`-Matcher im Manifest (Ubiquiti-OUIs mit Hostname
  `unifi*`), `async_step_dhcp` bestätigt per `/api/system` (öffentlich, `hardware.shortname` beginnt mit „UDR“).
  Eine eigene Suche in `async_setup` läuft nicht: HA lädt eine Custom-Integration ohne Eintrag und Matcher nicht.
- Produktbilder: `catalog.py` lädt höchstens wöchentlich `static.ui.com/fingerprint/ui/public.json` (Store
  `udr7.catalog`), Zuordnung über `stat/device[].sysid` (dezimal; hex = Katalog-`sysid`, UDR7 a67a, U7 Lite a693,
  USW Flex 2.5G ed35) → `icons/<icon.id>_257x257.png`. Einziger Internetzugriff, ohne ihn nur keine Bilder.
- Live lesen: `.venv/bin/python tools/live-lesen.py [host]` (Zugang aus dem Schlüsselbund, Dienst `udr7-test`).

## Prüfen

- `.venv/bin/python -m pytest -q -p no:logging --cov=custom_components/udr7` (≥ 95 %)
- `uvx ruff check . && uvx ruff format --check .`, `uvx mypy@2.3.1 --python-executable .venv/bin/python`
- `.venv/bin/python tools/pruefe-uebersetzungen.py`
- GitHub Actions: `pruefung.yml` (dasselbe plus hassfest), `hacs.yml`.
- `tools/next-version`, `tools/pruefe-uebersetzungen.py`, `tools/ha-venv.sh` sind byteidentische Kopien der Vorlagen.
- Logo: Produktbild von Ubiquiti (static.ui.com/fingerprint, Gerät UDR7, Variante nopadding), quadratisch mit
  transparentem Rand in `custom_components/udr7/brand/icon(@2x).png` und `docs/logo.png`.
