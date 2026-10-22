"""Liest den echten Dream Router mit dem Client der Integration (nur lesen) und zeigt, was HA daraus macht.

Zugang aus dem macOS-Schlüsselbund (Dienst „udr7-test“), nichts davon landet in der Ausgabe:
    security add-generic-password -U -s udr7-test -a <benutzer> -w
Aufruf: .venv/bin/python tools/live-lesen.py [host]
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import ssl
import subprocess
import sys

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.udr7.api import UdrClient


def keychain() -> tuple[str, str]:
    info = subprocess.run(
        ["security", "find-generic-password", "-s", "udr7-test"], capture_output=True, text=True, check=True
    ).stdout
    match = re.search(r'"acct"<blob>="([^"]*)"', info)
    if not match:
        sys.exit("Kein Konto im Schlüsselbund (Dienst udr7-test)")
    password = subprocess.run(
        ["security", "find-generic-password", "-s", "udr7-test", "-w"], capture_output=True, text=True, check=True
    ).stdout.rstrip("\n")
    return match.group(1), password


async def main(host: str) -> None:
    user, password = keychain()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as session:
        client = UdrClient(session, host, user, password)
        print("Fingerabdruck:", (await client.certificate_fingerprint(context))[:16], "…")
        print("identify:", await client.identify())
        health = await client.health()
        print("health:", sorted(health))
        devices = await client.devices()
        for d in devices.values():
            print(
                f"Gerät {d.name} ({d.shortname}, {d.type}) v{d.version} online={d.online} cpu={d.cpu} mem={d.memory} "
                f"temp={d.temperature} led={d.led_override}/{d.led_color}/{d.led_brightness} upgr={d.upgradable}"
            )
            for p in d.ports.values():
                print(f"   Port {p.idx} {p.name} up={p.up} speed={p.speed} enabled={p.enabled} poe={p.poe_mode}")
            for r in d.radios.values():
                print(f"   Funk {r.band} ch={r.channel} mode={r.tx_power_mode} clients={r.clients} cu={r.utilization}")
        clients = await client.clients()
        known = await client.known_clients()
        print("Clients aktiv:", len(clients), "bekannt:", len(known))
        wlans = await client.wlans()
        print("WLANs:", [(w.name, w.enabled, w.security, bool(w.passphrase)) for w in wlans.values()])
        for kind, rules in (await client.rules()).items():
            print(f"Regeln {kind}:", [(r.name, r.enabled) for r in rules.values()])
        settings = await client.gateway_settings()
        print("usg:", {k: settings.get(k) for k in ("_id", "key", "upnp_enabled")})
        print("Alarme:", await client.alarm_count())
        sysinfo = await client.sysinfo()
        print("sysinfo:", {k: sysinfo.get(k) for k in ("version", "update_available")})
        firmware, app = await client.os_status()
        print("UniFi OS latest:", (firmware.get("latest") or {}).get("version"), "Network-App:", app)


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "192.168.1.1"))
