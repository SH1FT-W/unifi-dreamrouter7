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
  Code-Kommentare englisch (bestehende deutsche nur bei Änderungen an der Datei mit übersetzen).
- Lokal committen ok, Push und Releases nur nach Rückfrage. Prod-HA nur auf ausdrücklichen Wunsch anfassen.
- Qualität: Platin der HA Integration Quality Scale ist die Untergrenze (`quality_scale.yaml` mitpflegen).
- Live-Tests am echten Router nur mit dem Besitzer: harmlose Schaltbefehle (LED, Gerät suchen, freier Port) ok,
  nichts, was das Internet unterbricht (WAN, Firewall, Neustart, Updates, Speedtest belastet den Anschluss).

## Version
Versionen über `tools/next-version` (globale Regeln). CHANGELOG `## YY.M.N – DD.MM.YYYY` (englisch), offene Punkte
unter `## Unreleased`.

## Schnittstelle (22.10.2026, Network 10.6.106 auf UniFi OS 5.1.33)

Pfade und Formate stehen in `api.py`; Anmeldung und Zertifikat-Pinning wie bei UNAS. Was dort nicht steht:

- v2-Objekte, `rest/portforward` und `rest/networkconf` immer vollständig zurück-PUTten (nur `enabled` geändert);
  `radio_table` und `port_overrides` ebenfalls ganz. `stat/alarm` gibt es seit Network 10 nicht mehr (404).
- `PATCH /api/system` (Display, Nachtmodus) braucht Super Admin, sonst 403.
- Status-LED der Konsole selbst: UniFi OS `ledSettings: {enabled}` in `/api/system` (PATCH wie Display). `led_override`
  der Network-App nimmt der Router für sich an, es kommt aber nie an der LED an (live 05.11.2026, UniFi OS 5.1.35;
  `cfgversion` bleibt gleich). Access Points und Switches weiter über `led_override` (U7 Lite geprüft).
- Port sperren (live 22.10. geprüft): `forward: "disabled"` setzt die Network-App still auf „all“ zurück. Wie die
  offizielle Integration `port_security_enabled: true`; die App baut den Eintrag dann um (forward disabled, Netz leer)
  und `port_security_enabled: false` gibt den Port NICHT wieder frei. Deshalb merkt sich der Coordinator den Eintrag
  vor dem Sperren (Store `udr7.<entry_id>.ports`) und schreibt ihn beim Freigeben zurück; ohne gemerkten Stand fliegt
  der Eintrag raus (Standard des Ports).
- Firmware des Routers selbst: die Network-App meldet sie oft nicht (`upgradable: false`), nur UniFi OS
  (`/api/system` firmware.latest). `devmgr upgrade` wird dann still ignoriert (live 05.11.2026, 5.1.34 → 5.1.35),
  installiert wird über `POST /api/firmware/update`.
- Erkennung: Router meldet sich weder per mDNS noch SSDP. HAs DHCP-Erkennung scannt aber die ARP-Tabelle mit
  Rückwärts-DNS (Router antwortet mit `unifi.localdomain`) → `dhcp`-Matcher im Manifest, `async_step_dhcp` bestätigt
  per `/api/system`. Eine eigene Suche in `async_setup` läuft nicht: HA lädt eine Custom-Integration ohne Eintrag
  und Matcher nicht.
- Produktbilder (`catalog.py`, Katalog von static.ui.com) sind der einzige Internetzugriff; ohne ihn nur keine Bilder.
- Live gelesen 02.11.2026, was es NICHT gibt: VPN nur als Summen in `stat/health`, nicht je Server. v2
  `wireguard/users` liefert nur konfigurierte Peers, keinen Handshake und keine Bytes; v2 `vpn/connections` →
  `{"connections": []}` (Inhalt bei aktiver Verbindung ungeprüft); `teleport/clients`, `vpn/servers`, `stat/vpn` 404.
- Live lesen: `.venv/bin/python tools/live-lesen.py [host]` (Zugang aus dem Schlüsselbund, Dienst `udr7-test`).

## Prüfen

- `.venv/bin/python -m pytest -q -p no:logging --cov=custom_components/udr7` (CI-Grenze 95 %)
- `uvx ruff@0.16.9 check . && uvx ruff@0.16.9 format --check .` (Version wie in pruefung.yml),
  `uvx mypy@2.3.1 --python-executable .venv/bin/python`
- `.venv/bin/python tools/pruefe-uebersetzungen.py`
- GitHub Actions: `pruefung.yml` (dasselbe plus hassfest), `hacs.yml`.
- `tools/next-version`, `tools/pruefe-uebersetzungen.py`, `tools/ha-venv.sh` sind byteidentische Kopien der Vorlagen.
- Logo: Produktbild von Ubiquiti (static.ui.com/fingerprint, Gerät UDR7, Variante nopadding), quadratisch mit
  transparentem Rand in `custom_components/udr7/brand/icon(@2x).png` und `docs/logo.png`.
