# Changelog

## 26.10.3 – 23.10.2026

- Fixed: "Speed test in progress" no longer stays on after a finished test when the router reports the last run as "Success", so the automatic speed test runs again ([#1](https://github.com/SH1FT-W/unifi-dreamrouter7/issues/1)).
- Fixed: the status LED light is also created for devices whose LED setting was never changed (no `led_override` in the device record); it then shows the site default as on ([#2](https://github.com/SH1FT-W/unifi-dreamrouter7/issues/2)).

## 26.10.2 – 22.10.2026

- Fixed: the "Started" sensors of the router, access points and switches (and "Connected since" of clients) no longer jump back and forth by a minute every few seconds and flood the logbook. The time only changes on a real restart or reconnect.
- Fixed: the presence tracker of a chosen client could be added disabled when Home Assistant set it up before the client's device existed.

## 26.10.1 – 22.10.2026

First release.

- Dream Router 7 as the main device, access points and switches as devices under it, chosen clients with a device each.
- Network values like the UniFi Network dashboard: throughput, clients, internet quality, public IP, provider, speed test with automatic test and contract comparison (taken over from UniFi Network Plus, same entity IDs).
- Switches for WiFi networks, firewall policies, traffic rules and routes, port forwards, content filters, Policy Engine policies, VPN servers and clients, UPnP, ports, PoE, locate and client internet access.
- WiFi password as text and WiFi QR code as image, status LED as a light (color and brightness on devices with an LED ring), transmit power per band.
- The router's display as a light with its brightness and the night mode start and end.
- Critical events of the last 24 hours.
- Product pictures of every device on its firmware update and online entities.
- Restart, PoE power cycle, reconnect, firmware updates for every device and the UniFi Network app.
- Actions to block, unblock and reconnect any client and to authorize guests.
- Discovered through Home Assistant's DHCP discovery, certificate pinning with a repair, diagnostics, English and German.
