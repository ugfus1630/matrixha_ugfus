"""Connection lifecycle and event fan-out for the Matrix Bridge."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api_client import BridgeAuthError, BridgeClient, BridgeError
from .const import (
    BRIDGE_ERROR,
    BRIDGE_ROOMS_CHANGED,
    BRIDGE_STATUS,
    BRIDGE_VERIFICATION,
    CONF_CREATE_NOTIFY_ENTITIES,
    CONF_FIRE_EVENTS,
    DEFAULT_CREATE_NOTIFY_ENTITIES,
    DEFAULT_FIRE_EVENTS,
    DOMAIN,
    EVENT_MAP,
    SIGNAL_ROOMS_UPDATED,
    SIGNAL_STATE_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN = 5
RECONNECT_MAX = 120


class MatrixBridgeCoordinator:
    """Keeps a live WebSocket to the add-on and projects its events into HA."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: BridgeClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client

        self.connected = False
        self.status: dict[str, Any] = {}
        self.rooms: list[dict[str, Any]] = []
        self.last_message: dict[str, Any] | None = None

        self._task: asyncio.Task | None = None
        self._known_rooms: set[str] = set()

    @property
    def fire_events(self) -> bool:
        """Configured in the add-on's panel and reported by /api/status."""
        return bool(self.status.get(CONF_FIRE_EVENTS, DEFAULT_FIRE_EVENTS))

    @property
    def create_notify_entities(self) -> bool:
        """Configured in the add-on's panel and reported by /api/status."""
        return bool(
            self.status.get(CONF_CREATE_NOTIFY_ENTITIES, DEFAULT_CREATE_NOTIFY_ENTITIES)
        )

    @property
    def bot_user_id(self) -> str:
        return self.status.get("user_id") or self.entry.title

    # ------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Prime state over REST, then keep a WebSocket open."""
        await self.async_refresh()
        self._task = self.entry.async_create_background_task(
            self.hass, self._listen(), name=f"{DOMAIN}-ws"
        )

    async def async_stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def async_refresh(self) -> None:
        """Re-read status and rooms from the add-on."""
        self.status = await self.client.status()
        rooms = await self.client.rooms()
        self._apply_rooms(rooms)
        self.connected = bool(self.status.get("connected"))
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

    # ---------------------------------------------------------------- WS loop

    async def _listen(self) -> None:
        session = async_get_clientsession(self.hass)
        backoff = RECONNECT_MIN

        while True:
            try:
                async with session.ws_connect(
                    self.client.ws_url,
                    headers=self.client.headers,
                    heartbeat=30,
                ) as socket:
                    _LOGGER.debug("Connected to the Matrix Bridge event stream")
                    self.connected = True
                    backoff = RECONNECT_MIN
                    async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

                    async for message in socket:
                        if message.type is aiohttp.WSMsgType.TEXT:
                            self._handle_event(message.json())
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break

            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError, BridgeError) as err:
                _LOGGER.debug("Matrix Bridge event stream dropped: %s", err)
            except Exception:  # pragma: no cover - defensive
                _LOGGER.exception("Unexpected error on the Matrix Bridge event stream")

            # Mark unavailable and back off with jitter so several entries do
            # not stampede a restarting add-on.
            self.connected = False
            async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)
            delay = min(backoff, RECONNECT_MAX) * (0.8 + random.random() * 0.4)
            _LOGGER.debug("Reconnecting to the add-on in %.1fs", delay)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, RECONNECT_MAX)

            # A reconnect may have missed room changes while we were away.
            with contextlib.suppress(BridgeError):
                await self.async_refresh()

    # ------------------------------------------------------------- dispatching

    def _apply_rooms(self, rooms: list[dict[str, Any]]) -> None:
        self.rooms = rooms
        room_ids = {room["room_id"] for room in rooms}
        if room_ids != self._known_rooms:
            self._known_rooms = room_ids
            async_dispatcher_send(self.hass, SIGNAL_ROOMS_UPDATED, rooms)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        data = event.get("data") or {}

        if event_type == BRIDGE_STATUS:
            self.status = data
            self.connected = bool(data.get("connected"))
            async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)
            return

        if event_type == BRIDGE_ROOMS_CHANGED:
            if "rooms" in data:
                self._apply_rooms(data["rooms"])
                async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)
            else:
                self.hass.async_create_task(self._refresh_quietly())
            return

        if event_type == BRIDGE_ERROR:
            _LOGGER.warning("Matrix Bridge: %s", data.get("message"))

        if event_type in ("message", "image", "file"):
            self.last_message = data
            async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

        if event_type == BRIDGE_VERIFICATION:
            self._notify_verification(data)

        # Re-emit on the HA event bus so automations can trigger on it.
        if self.fire_events and (ha_event := EVENT_MAP.get(event_type or "")):
            self.hass.bus.async_fire(ha_event, data)

    async def _refresh_quietly(self) -> None:
        try:
            await self.async_refresh()
        except (BridgeError, BridgeAuthError) as err:
            _LOGGER.debug("Refresh after room change failed: %s", err)

    def _notify_verification(self, data: dict[str, Any]) -> None:
        """Surface emoji verification as a persistent notification."""
        stage = data.get("stage")
        transaction_id = data.get("transaction_id")
        if not transaction_id:
            return

        notification_id = f"{DOMAIN}_verify_{transaction_id}"

        if stage in ("verified", "cancelled", "rejected", "timed_out"):
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": notification_id},
                    blocking=False,
                )
            )
            return

        emoji = data.get("emoji") or []
        if not emoji:
            return

        strip = "  ".join(entry["emoji"] for entry in emoji)
        names = ", ".join(entry["name"] for entry in emoji)
        message = (
            f"**{data.get('user_id')}** · `{data.get('device_id')}`\n\n"
            f"# {strip}\n\n"
            f"{names}\n\n"
            "If these match the emoji shown in your Matrix client, confirm with:\n"
            f"```yaml\n"
            f"action: {DOMAIN}.confirm_verification\n"
            f"data:\n  transaction_id: {transaction_id}\n"
            f"```"
        )

        self.hass.async_create_task(
            self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Matrix device verification",
                    "message": message,
                    "notification_id": notification_id,
                },
                blocking=False,
            )
        )
