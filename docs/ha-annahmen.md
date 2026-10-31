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

## Fremde Integrationen

Keine; Entitäten anderer Integrationen werden nicht gelesen.

## Prüfbefehl

```sh
.venv/bin/python -m pytest -q -p no:logging
```
