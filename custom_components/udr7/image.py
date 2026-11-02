"""QR-Code je WLAN zum Verbinden per Handykamera (z. B. auf einem Tablet-Dashboard für Gäste)."""

from __future__ import annotations

import io

from homeassistant.components.image import ImageEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util
import segno

from .api import Wlan
from .coordinator import UdrConfigEntry, UdrCoordinator
from .entity import UdrEntity, track_new_items

PARALLEL_UPDATES = 0


def qr_png(payload: str) -> bytes:
    buffer = io.BytesIO()
    segno.make(payload, error="m").save(buffer, kind="png", scale=8, border=2)
    return buffer.getvalue()


async def async_setup_entry(
    hass: HomeAssistant, entry: UdrConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entry.async_on_unload(
        track_new_items(
            coordinator,
            lambda d: d.wlans.keys(),
            lambda ids: (WlanQrCode(hass, coordinator, coordinator.data.wlans[i]) for i in ids),
            async_add_entities,
        )
    )


class WlanQrCode(UdrEntity, ImageEntity):
    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, hass: HomeAssistant, coordinator: UdrCoordinator, wlan: Wlan) -> None:
        UdrEntity.__init__(self, coordinator, f"wlan_{wlan.id}_qr_code")
        ImageEntity.__init__(self, hass)
        self._attr_translation_key = "wlan_qr_code"
        self._attr_translation_placeholders = {"name": wlan.name}
        self._id = wlan.id
        self._payload = ""
        self._image = b""

    @property
    def available(self) -> bool:
        return super().available and self._id in self.coordinator.data.wlans

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._payload_changed():
            # segno builds the PNG synchronously: in the executor, the state follows once it is done
            self.hass.async_create_task(self._async_build(self._payload), eager_start=True)
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._payload_changed():
            await self._async_build(self._payload)

    @callback
    def _payload_changed(self) -> bool:
        """Take over a new payload; only a changed name or password needs a new image."""
        wlan = self.coordinator.data.wlans.get(self._id)
        if wlan is None or wlan.qr_payload == self._payload:
            return False
        self._payload = wlan.qr_payload
        return True

    async def _async_build(self, payload: str) -> None:
        image = await self.hass.async_add_executor_job(qr_png, payload)
        if payload != self._payload:
            return  # changed again meanwhile; the newer build sets the image
        self._image = image
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._image or None
