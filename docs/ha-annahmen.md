# HA-Annahmen

<!-- Vorlage aus repo-vorlagen/vorlagen/ha-annahmen.md. Liegt im Repo als docs/ha-annahmen.md.
     Der HA-Beta-Bericht (repo-vorlagen/ha-beta-bericht.md) gleicht diese Liste jeden Monat mit den
     Neuerungen der HA-Beta ab. Nur festhalten, was bei einer Änderung in HA still kaputtginge. -->

Worauf sich dieses Repo bei Home Assistant verlässt. Bei jeder Änderung am Code mitpflegen.

**Getestet mit:** HA 2026.9.3 (Tests), 2026.10, 2026.11.0b1 (Tests) · **Mindestversion:** 2026.9.0 (`hacs.json`)

## Annahmen

| Annahme | Wo im Code | Abgesichert durch |
|---|---|---|
| `DeviceInfo` kennt `via_device_id` (Geräte-ID), nicht mehr `via_device`/`default_name` (seit 2026.9) | `entity.py` `device_info()`, `ClientEntity` | `tests/test_setup.py::test_devices` |
| Bis HA 2026.10 legt ein `ScannerEntity` kein eigenes Gerät an (`device_info` ist dort `@final` und `None`), hängt sich über `mac_address` an ein vorhandenes und benennt ein selbst angelegtes Gerät nach `hostname`. Ab 2026.11 (Core #188883) ist `device_info` nicht mehr final: HA nimmt das `_attr_device_info` aus `ClientEntity` (`client_device_info()`, MAC als Verbindung, Name aus UniFi) | `device_tracker.py` (`hostname` = Name aus UniFi), `entity.py` `client_device_info()` | `test_setup.py::test_devices`, `::test_clients` |
| Bis HA 2026.10 wird ein neues `ScannerEntity` deaktiviert angelegt, wenn es noch kein Gerät mit seiner MAC gibt (Property `entity_registry_enabled_default`, überschreibt `_attr_…`); deshalb legt `async_setup_entry` die Client-Geräte vor den Plattformen an (bleibt bis Mindestversion 2026.11). Ab 2026.11 (Core #188883) ist `_attr_entity_registry_enabled_default = False` auf `ScannerEntity`; `ClientTracker` setzt es auf `True`, weil Clients bewusst gewählt werden | `__init__.py`, `device_tracker.py` `ClientTracker`, `entity.py` `client_device_info()` | `test_setup.py::test_clients` |
| `ScannerEntity.unique_id` ist die MAC (fest in HA) | `device_tracker.py` | `test_setup.py::test_clients` |
| Entitätsnamen mit `translation_placeholders` ({name}, {band}, {port}) | `entity.py`, alle Plattformen | `test_setup.py` (entity_ids) |
| `entity_id` vor dem Hinzufügen setzen übernimmt HA beim ersten Anlegen (alte IDs aus Network Plus) | `entity.py` `UdrEntity` | `test_setup.py::test_network_sensors` |
| Text-Entität prüft `native_min`/`native_max` selbst („too short“; bis 2026.10 `ValueError`, ab 2026.11 `ServiceValidationError`, Core #186311) | `text.py` | `test_control.py::test_wlan_password` (akzeptiert beide) |
| `device_registry.async_get_device` ist abgekündigt; Tests nutzen `async_get_device_by_identifier`/`_by_connection` mit Eintrags-ID | Tests | `test_setup.py`, `test_features.py` |
| `RepairsFlow` mit `async_create_fix_flow(hass, issue_id, data)` | `repairs.py` | `test_features.py` |
| `ImageEntity`: ein neues `_attr_image_last_updated` plus `async_write_ha_state` ändert den Zustand (neues Bild für Dashboards), `async_image` liefert die Bytes; das Bild entsteht im Executor (`hass.async_add_executor_job`) | `image.py` `WlanQrCode` | `test_features.py::test_wlan_qr_code_rebuilt_in_executor` |
| `EventEntity`: `_trigger_event(typ, attribute)` plus `async_write_ha_state` schreibt ein Ereignis (Zustand = Zeitpunkt, Attribute `event_type` + eigene); der Typ muss in `_attr_event_types` stehen, Übersetzung unter `entity.event.<key>.state_attributes.event_type.state`; nach einem Neustart stellt HA das letzte Ereignis wieder her, ohne neu zu feuern | `event.py` `NewDeviceEvent` | `test_history.py::test_new_device_event`, `::test_new_wired_device_with_option` |
| Ein `DataUpdateCoordinator` mit `config_entry` ruft beim Entladen `async_shutdown` auf; dort schreibt der Coordinator die gesehenen MACs und Ausfälle sofort (`Store`), damit ein Neuladen nach geänderten Optionen nicht den älteren Stand liest | `coordinator.py` `async_shutdown`, `_history_store` | `test_history.py::test_new_device_event`, `::test_internet_outages` |
| Sensor mit `state_class` `total_increasing`: fällt der Wert (0 um Mitternacht), wertet die Statistik das als neuen Zählbeginn | `sensor.py` `internet_outages_today` | keiner (Tests prüfen nur den Zustand 0, nicht die Statistik) |
| HAs gemeinsame Session hat einen normalen Cookie-Speicher: das `TOKEN` einer Anmeldung bliebe dort für Hostnamen (nicht für IP-Adressen) und ginge mit jeder Anfrage mit, eine zweite Anmeldung am selben Host tauschte die Sitzung des laufenden Eintrags. Darum je Client eine eigene Session (`async_create_clientsession`) mit `aiohttp.DummyCookieJar`. Im Setup angelegt, löst HA sie beim Entladen des Eintrags selbst (`current_entry`, auch nach fehlgeschlagenem Setup); im Config-Flow mit `auto_cleanup=False` und danach `detach()`, denn HA ersetzt `close()` dieser Sessions durch eine Warnung. Eine Anfrage auf einer gelösten Session (auch mitten im Verbindungsaufbau) endet als eigener Fehler statt aiohttps `RuntimeError`/`AttributeError` (wie HACS-Review hacs/default#13228 bei UNAS) | `__init__.py` `create_client`, `api.py` `detach`/`_check_open`, `config_flow.py` | `tests/test_setup.py::test_login_cookie_stays_out_of_shared_session`, `::test_flow_session_released_and_closed_session_reported`, `::test_session_detached_while_connecting_gives_own_error`, `tests/test_config_flow.py::test_user_flow_verify_ssl` (Flow `auto_cleanup=False`, Setup `True`), `::test_entry_session_released_on_unload_and_failed_setup` (führt HAs Aufräum-Haken selbst aus, den `aioclient_mock` sonst verwirft). Grenzfall: scheitert das Entladen der Plattformen (FAILED_UNLOAD), arbeitet HA die Entlade-Haken nicht ab, die Session bleibt dann bis zum Stopp |
| Die Update-Liste in HA (Einstellungen › Updates) zeigt oben den Gerätenamen, darunter `Bereich · title latest_version` (Frontend `ha-config-updates.ts`); Casora nimmt `title` als Namen. Darum `title` „UniFi OS“ (Router-Firmware) und „UniFi Network“, sonst sähe das Router-Update aus wie das gleichnamige Update der Integration | `update.py` `FirmwareUpdate.title`, `NetworkAppUpdate` | `tests/test_control.py::test_update_titles` (nur das Attribut, die Anzeige liegt im Frontend) |
| Nach einem Befehl erscheint der neue Zustand sofort: `async_command(applied=…)` schreibt die vom Router angenommene Änderung in `coordinator.data` und ruft `async_update_listeners`. Grund: `async_request_refresh` hat einen Debouncer (sofort, danach 10 s Sperrzeit, `REQUEST_REFRESH_DEFAULT_COOLDOWN`), eine volle Runde dauert mehrere Sekunden, und der Schalter im Frontend springt nach 2 s ohne neuen Zustand zurück (ein zweites Tippen schickte dann den Gegenbefehl, 05.11.2026 an der Status-LED) | `coordinator.py` `async_command`, `light.py` `LedLight._applied`, `_set_console_led`, `DisplayLight._set` | `tests/test_control.py::test_led_state_follows_command`, `::test_display_state_follows_command` (Debouncer-Sperrzeit; das Zurückspringen im Frontend selbst ist nicht getestet) |

## Fremde Integrationen

Keine; Entitäten anderer Integrationen werden nicht gelesen.

## Prüfbefehl

```sh
.venv/bin/python -m pytest -q -p no:logging
```
