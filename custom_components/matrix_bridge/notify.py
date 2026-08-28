"""Notify entities — one per discovered Matrix room.

Uses the modern NotifyEntity platform rather than the legacy notify service
platform, which Home Assistant is deprecating. Rich payloads (images, files,
replies, reactions) are exposed as `matrix_bridge.*` services instead, because
NotifyEntity intentionally carries only a message and a title.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MatrixBridgeConfigEntry
from .api_client import BridgeError, RoomUntrustedError
from .const import SIGNAL_ROOMS_UPDATED
from .entity import MatrixBridgeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MatrixBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a notify entity per room, and keep the set in sync."""
    coordinator = entry.runtime_data

    # Toggled in the add-on's panel, not here; reported through /api/status.
    if not coordinator.create_notify_entities:
        return

    known: set[str] = set()

    @callback
    def _sync(rooms: list[dict[str, Any]]) -> None:
        new = [room for room in rooms if room["room_id"] not in known]
        if not new:
            return
        known.update(room["room_id"] for room in new)
        async_add_entities(
            MatrixRoomNotifyEntity(coordinator, room) for room in new
        )

    _sync(coordinator.rooms)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ROOMS_UPDATED, _sync)
    )


class MatrixRoomNotifyEntity(MatrixBridgeEntity, NotifyEntity):
    """Sends a message to one Matrix room."""

    _attr_supported_features = NotifyEntityFeature.TITLE
    _attr_icon = "mdi:matrix"

    def __init__(self, coordinator, room: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._room_id = room["room_id"]
        self._attr_name = room["name"]
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self._room_id}"

    @property
    def _room(self) -> dict[str, Any] | None:
        return next(
            (r for r in self.coordinator.rooms if r["room_id"] == self._room_id), None
        )

    @property
    def available(self) -> bool:
        # Unavailable once the bot leaves the room, so stale automations surface
        # the problem instead of failing silently.
        return super().available and self._room is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self._room or {}
        return {
            "room_id": self._room_id,
            "encrypted": room.get("encrypted"),
            "member_count": room.get("member_count"),
            "unverified_device_count": room.get("unverified_device_count"),
            "trusted": room.get("trusted"),
        }

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        body = f"{title}\n{message}" if title else message
        try:
            await self.coordinator.client.send_message(self._room_id, body=body)
        except RoomUntrustedError as err:
            devices = ", ".join(
                f"{d['user_id']}/{d['device_id']}" for d in err.unverified
            )
            raise HomeAssistantError(
                f"{err} Unverified devices: {devices or 'unknown'}"
            ) from err
        except BridgeError as err:
            raise HomeAssistantError(str(err)) from err
