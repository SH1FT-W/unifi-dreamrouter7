"""Updates: firmware per UniFi device (UniFi OS on the Dream Router) and the Network app."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Device, as_dict
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import DeviceEntity, UdrEntity, device_keys, track_new_items

PARALLEL_UPDATES = 1


def os_latest(coordinator: UdrCoordinator) -> str | None:
    """Latest UniFi OS version from /api/system, e.g. "v5.1.40+abc" → "5.1.40"."""
    latest = as_dict(coordinator.data.os_firmware.get("latest"))
    version = str(latest.get("version") or "").removeprefix("v").split("+", 1)[0]
    return version or None


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([NetworkAppUpdate(coordinator)])

    def _device_entities(macs: set[str]) -> Iterable[Entity]:
        return (FirmwareUpdate(coordinator, coordinator.data.devices[mac]) for mac in macs)

    entry.async_on_unload(track_new_items(coordinator, device_keys, _device_entities, async_add_entities))


class FirmwareUpdate(DeviceEntity, UpdateEntity):
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, coordinator: UdrCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "firmware")

    @property
    def available(self) -> bool:
        # The device is not "connected" during the update; the entity should still show the progress
        return self.coordinator.last_update_success and self.device is not None

    @property
    def entity_picture(self) -> str | None:
        return self.coordinator.picture(self.device) or super().entity_picture

    @property
    def title(self) -> str | None:
        # Software name shown next to the version (HA's update list shows the device name above it), so the router's
        # firmware is not mistaken for the integration's own update with nearly the same name
        device = self.device
        return "UniFi OS" if device is not None and device.is_gateway else None

    @property
    def installed_version(self) -> str | None:
        device = self.device
        return device.version or None if device else None

    @property
    def latest_version(self) -> str | None:
        device = self.device
        if device is None:
            return None
        if device.upgradable and device.upgrade_to:
            # "5.1.40.34200" → compare like installed_version, without the build number
            return ".".join(device.upgrade_to.split(".")[:3])
        if device.is_gateway and (latest := os_latest(self.coordinator)) is not None:
            return latest
        return self.installed_version

    @property
    def in_progress(self) -> bool:
        device = self.device
        # state 4 = update running
        return device is not None and device.state == 4

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        client = self.coordinator.client
        device = self.device
        if device is not None and device.is_gateway and not (device.upgradable and device.upgrade_to):
            # The Network app does not offer this update (devmgr "upgrade" would be silently ignored);
            # the version comes from UniFi OS, so UniFi OS installs it
            await self.coordinator.async_command(client.update_os)
            return
        await self.coordinator.async_command(lambda: client.upgrade_device(self._mac))


class NetworkAppUpdate(UdrEntity, UpdateEntity):
    _attr_supported_features = UpdateEntityFeature.INSTALL
    _attr_title = "UniFi Network"

    def __init__(self, coordinator: UdrCoordinator) -> None:
        super().__init__(coordinator, "network_app")

    @property
    def installed_version(self) -> str | None:
        data = self.coordinator.data
        return str(data.network_app.get("version") or data.sysinfo.get("version") or "") or None

    @property
    def latest_version(self) -> str | None:
        # updateAvailable is null or the new version number
        update = self.coordinator.data.network_app.get("updateAvailable")
        return str(update) if update else self.installed_version

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        await self.coordinator.async_command(self.coordinator.client.update_network_app)
