"""Konstanten der Integration UniFi Dream Router 7."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "udr7"
MANUFACTURER = "Ubiquiti"

CONF_SITE = "site"
DEFAULT_HOST = "192.168.1.1"
DEFAULT_SITE = "default"
# SHA-256 des bei der Einrichtung gemerkten Zertifikats (entry.data)
CONF_FINGERPRINT = "fingerprint"

# stat/health alle 15 s (Durchsatz wie im Dashboard der Network-App)
UPDATE_INTERVAL = timedelta(seconds=15)
# Geräte und Clients (stat/device ~150 KB, stat/sta) jede 2. Runde = alle 30 s
DEVICES_EVERY = 2
# Einstellungen (WLANs, Regeln, Filter, VPN …) jede 4. Runde = jede Minute; nach einem Befehl sofort
CONFIG_EVERY = 4
# Systeminfo, UniFi-OS-Firmware und App-Updates ändern sich selten: jede 20. Runde = alle 5 Minuten
SLOW_EVERY = 20
HTTP_TIMEOUT = 20
# Gemerkte Port-Einstellungen (.storage)
STORAGE_VERSION = 1

# So lange braucht ein Speedtest des Gateways ungefähr, danach neu abfragen
SPEEDTEST_SECONDS = 40
# Kommt bis dahin kein Ergebnis, gilt der Speedtest nicht mehr als laufend
SPEEDTEST_TIMEOUT = timedelta(minutes=3)

# Optionen
CONF_TRACKED_CLIENTS = "tracked_clients"
CONF_SPEEDTEST_MINUTES = "speedtest_minutes"
DEFAULT_SPEEDTEST_MINUTES = 60
# Erwartete Geschwindigkeit laut Vertrag (Mbit/s, 0 = keine Bewertung) und erlaubte Abweichung in %
CONF_EXPECTED_DOWNLOAD = "expected_download"
CONF_EXPECTED_UPLOAD = "expected_upload"
CONF_TOLERANCE = "tolerance"
DEFAULT_TOLERANCE = 10
# So lange gilt ein Client nach dem letzten Kontakt noch als zu Hause (wie „detection time“ der UniFi-Integration)
CONF_AWAY_SECONDS = "away_seconds"
DEFAULT_AWAY_SECONDS = 300

# Sendeleistung der Funkmodule (radio_table[].tx_power_mode); „custom“ nur anzeigen, nicht wählbar
TX_POWER_MODES = ["auto", "high", "medium", "low"]
# Funkband (radio) → Schlüssel für Namen
RADIO_BANDS = {"ng": "2g", "na": "5g", "6e": "6g"}

# Entitäten, die es in UniFi Network Plus (und davor als REST-Sensoren bzw. in „UniFi WAN“) gab: gleiche entity_id,
# damit Dashboards, Verlauf und Statistik weiterlaufen (gilt nur, solange die ID beim ersten Anlegen frei ist, also
# nachdem UniFi Network Plus entfernt wurde)
LEGACY_ENTITY_IDS = {
    "wlan_upload": "sensor.wifi_rx",
    "wlan_download": "sensor.wifi_tx",
    "lan_upload": "sensor.lan_rx",
    "lan_download": "sensor.lan_tx",
    "wan_download": "sensor.unifi_wan_download",
    "wan_upload": "sensor.unifi_wan_upload",
    "speedtest_download": "sensor.unifi_speedtest_download",
    "speedtest_upload": "sensor.unifi_speedtest_upload",
    "speedtest_ping": "sensor.unifi_speedtest_ping",
    "speedtest_last": "sensor.unifi_speedtest_last_run",
    "wan_ip": "sensor.unifi_wan_ipv4",
    "wan_ipv6": "sensor.unifi_wan_ipv6",
    "active_wan": "sensor.unifi_active_wan_name",
    "internet": "binary_sensor.unifi_wan_internet",
    "wan_up": "binary_sensor.unifi_active_wan_up",
    "speedtest_running": "binary_sensor.unifi_speedtest_in_progress",
    "speedtest": "button.unifi_run_speedtest",
    "auto_speedtest": "switch.unifi_wan_auto_speedtest",
}
