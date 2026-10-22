# HA-Annahmen

<!-- Vorlage aus repo-vorlagen/vorlagen/ha-annahmen.md. Liegt im Repo als docs/ha-annahmen.md.
     Der HA-Beta-Bericht (repo-vorlagen/ha-beta-bericht.md) gleicht diese Liste jeden Monat mit den
     Neuerungen der HA-Beta ab. Nur festhalten, was bei einer Änderung in HA still kaputtginge. -->

Worauf sich dieses Repo bei Home Assistant verlässt. Bei jeder Änderung am Code mitpflegen.

**Getestet mit:** HA 2026.9.3 (Tests), 2026.10 · **Mindestversion:** 2026.9.0 (`hacs.json`)

## Annahmen

| Annahme | Wo im Code | Abgesichert durch |
|---|---|---|
| `DeviceInfo` kennt `via_device_id` (Geräte-ID), nicht mehr `via_device`/`default_name` (seit 2026.9) | `entity.py` `device_info()`, `ClientEntity` | `tests/test_setup.py::test_devices` |
| Ein `ScannerEntity` legt kein eigenes Gerät an, hängt sich über `mac_address` an ein vorhandenes und benennt ein selbst angelegtes Gerät nach `hostname` | `device_tracker.py` (`hostname` = Name aus UniFi) | `test_setup.py::test_devices`, `::test_clients` |
| Ein neues `ScannerEntity` wird deaktiviert angelegt, wenn es noch kein Gerät mit seiner MAC gibt (`entity_registry_enabled_default`); deshalb legt `async_setup_entry` die Client-Geräte vor den Plattformen an | `__init__.py`, `entity.py` `client_device_info()` | `test_setup.py::test_clients` |
| `ScannerEntity.unique_id` ist die MAC (fest in HA) | `device_tracker.py` | `test_setup.py::test_clients` |
| Entitätsnamen mit `translation_placeholders` ({name}, {band}, {port}) | `entity.py`, alle Plattformen | `test_setup.py` (entity_ids) |
| `entity_id` vor dem Hinzufügen setzen übernimmt HA beim ersten Anlegen (alte IDs aus Network Plus) | `entity.py` `UdrEntity` | `test_setup.py::test_network_sensors` |
| Text-Entität prüft `native_min`/`native_max` selbst (ValueError „too short“) | `text.py` | `test_control.py::test_wlan_password` |
| `device_registry.async_get_device` ist abgekündigt; Tests nutzen `async_get_device_by_identifier`/`_by_connection` mit Eintrags-ID | Tests | `test_setup.py`, `test_features.py` |
| `RepairsFlow` mit `async_create_fix_flow(hass, issue_id, data)` | `repairs.py` | `test_features.py` |

## Fremde Integrationen

Keine; Entitäten anderer Integrationen werden nicht gelesen.

## Prüfbefehl

```sh
.venv/bin/python -m pytest -q -p no:logging
```
