# Changelog

## Unreleased

- Changed: The update entities name their software: "UniFi OS" for the router's firmware and "UniFi Network" for the Network app. Home Assistant's update list shows it next to the version, so the router's firmware update is no longer mistaken for the update of this integration, which has nearly the same name.

## 26.11.4 – 05.11.2026

- Fixed: Install on the Dream Router firmware update did nothing when the update came from UniFi OS only. The Network app does not always offer the router's own firmware and silently ignored the install command, so no error showed up. The router's firmware now installs through UniFi OS in that case.

## 26.11.3 – 04.11.2026

- Changed: The WiFi password entity is write-only. It sets a new password but no longer shows the current one as its state, so the password is not visible to every Home Assistant user and is not written into the history (Marketplace review, hacs/default#13238). To share the network, use the QR code image entity.
- Fixed: Configure opens even when the router was unreachable at startup or setup failed. It then offers the stored client selection only, so options like Verify certificate can be changed to get the setup working.

## 26.11.2 – 03.11.2026

- Fixed: The integration now logs in to UniFi OS on its own HTTP session without cookies instead of Home Assistant's shared one. With the router set up by host name (for example `unifi.local`), the login cookie landed in the shared session, so adding, re-authenticating or reconfiguring with another account could silently swap the session of the running integration. Sessions used while setting up are released afterwards.

## 26.11.1 – 02.11.2026

- New: event "New device connected" fires the first time a device with an unknown MAC address joins, with name, hostname, vendor, WiFi name, IP, network and whether the address is private (randomized). Devices already known at the first start do not fire, the list of seen addresses survives restarts. WiFi only by default; an option also includes devices connected by cable.
- New: sensors "Internet outages today" and "Last internet outage" (start, end and duration) from the router's internet status. Outages survive restarts.
- New: "Internet latency" has the 24 hour average of the router's monitors as an attribute, "VPN clients" the totals of the VPN servers (inactive, received and sent bytes).
- Fixed: the WiFi QR code image is now built outside Home Assistant's event loop, also when it is rebuilt after the WiFi name or password changed, so building it no longer briefly blocks Home Assistant.

## 26.10.4 – 31.10.2026

- Fixed: with Home Assistant 2026.11 the presence trackers of newly chosen clients are added enabled again. Home Assistant now adds new network trackers disabled by default, but these clients are picked on purpose.
- Compatibility: ready for Home Assistant 2026.11, where client trackers use the integration's own client device and a too short WiFi password is reported as a validation error.

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
