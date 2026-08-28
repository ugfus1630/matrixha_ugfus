"""The Matrix bot: connection, room discovery, send and receive."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    JoinError,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    LocalProtocolError,
    LoginError,
    MegolmEvent,
    ReactionEvent,
    RoomEncryptedFile,
    RoomEncryptedImage,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageNotice,
    RoomMessageText,
    RoomSendError,
    SyncResponse,
    WhoamiError,
)

from . import events
from .events import EventBus
from .media import MediaError, MediaManager
from .rooms import RoomRegistry
from .settings import Config
from .store import Session, clear_session, load_session, save_session
from .verification import VerificationManager

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nio import MatrixRoom

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN = 5
RECONNECT_MAX = 300


def _describe_error(err: BaseException) -> str:
    """Name the exception when it carries no message.

    nio is configured with max_timeouts=0, so a slow homeserver surfaces as a
    bare TimeoutError whose str() is empty -- which logged as "Sync failed ()"
    and left the panel showing no reason at all.
    """
    text = str(err).strip()
    if text:
        return text
    if isinstance(err, (TimeoutError, asyncio.TimeoutError)):
        return "the homeserver stopped responding"
    return type(err).__name__


class BotError(Exception):
    """Raised for send/join failures; the message is surfaced to the caller."""


class RoomUntrusted(BotError):
    """Raised when the trust gate refuses to send into a room."""

    def __init__(self, verdict) -> None:
        super().__init__(verdict.reason)
        self.verdict = verdict


class MatrixBot:
    """Owns the nio client and everything derived from it."""

    def __init__(self, config: Config, bus: EventBus) -> None:
        self.config = config
        self.bus = bus
        self.client: AsyncClient | None = None
        self.rooms: RoomRegistry | None = None
        self.media: MediaManager | None = None
        self.verification: VerificationManager | None = None

        self.connected = False
        self.last_error: str = ""
        self._sync_task: asyncio.Task | None = None
        self._stopping = False
        self._backoff = RECONNECT_MIN

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        try:
            await self._connect()
        except BotError:
            raise
        except (aiohttp.ClientError, TimeoutError, LocalProtocolError, OSError) as err:
            # nio is configured with max_timeouts=0, so it does not retry: a
            # homeserver that does not resolve, refuses the connection or fails
            # the TLS handshake surfaces the transport error here. Translating
            # it means the caller can report it instead of the add-on dying on
            # the single most common misconfiguration.
            raise BotError(
                f"Could not reach the homeserver at {self.config.homeserver}: {err}"
            ) from err
        self._sync_task = asyncio.create_task(self._sync_loop(), name="matrix-sync")

    async def stop(self) -> None:
        self._stopping = True
        if self._sync_task:
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task
        if self.client:
            await self.client.close()
        self.connected = False

    async def _connect(self) -> None:
        config = self.config
        config.store_path.mkdir(parents=True, exist_ok=True)

        client_config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=config.e2ee.enabled,
            max_limit_exceeded=0,
            max_timeouts=0,
        )

        session = load_session(config.session_path, config.user_id, config.homeserver)

        self.client = AsyncClient(
            homeserver=config.homeserver,
            user=config.user_id,
            device_id=session.device_id if session else "",
            store_path=str(config.store_path),
            config=client_config,
        )

        if session:
            # Synchronous in nio, despite living on the async client.
            self.client.restore_login(
                user_id=session.user_id,
                device_id=session.device_id,
                access_token=session.access_token,
            )
            _LOGGER.info(
                "Restored session for %s (device %s)",
                session.user_id,
                session.device_id,
            )
            if not await self._validate_token():
                _LOGGER.warning("Stored access token was rejected; logging in again")
                clear_session(config.session_path)
                await self.client.close()
                self.client = None
                await self._connect()
                return
        elif config.access_token:
            self.client.restore_login(
                user_id=config.user_id,
                device_id=config.device_name,
                access_token=config.access_token,
            )
            if not await self._validate_token():
                raise BotError(
                    "The configured access_token was rejected by the homeserver."
                )
            save_session(
                config.session_path,
                Session(
                    user_id=self.client.user_id,
                    device_id=self.client.device_id,
                    access_token=config.access_token,
                    homeserver=config.homeserver,
                ),
            )
        else:
            await self._login_with_password()

        if config.e2ee.enabled:
            self.client.load_store()  # synchronous

        self.rooms = RoomRegistry(self.client, config)
        self.media = MediaManager(self.client, config)
        self.verification = VerificationManager(self.client, config, self.bus)
        self._register_callbacks()

        if config.e2ee.enabled and self.client.should_upload_keys:
            await self.client.keys_upload()

        self.connected = True
        self.last_error = ""
        self.bus.publish(events.STATUS, self.status())
        _LOGGER.info(
            "Connected to %s as %s (device %s)",
            config.homeserver,
            self.client.user_id,
            self.client.device_id,
        )

    async def _login_with_password(self) -> None:
        if not self.config.password:
            raise BotError(
                "No stored session and no credentials: fill in a password (or "
                "an access token) under Settings so the bot can log in for the "
                "first time."
            )

        response = await self.client.login(
            password=self.config.password,
            device_name=self.config.device_name,
        )
        if isinstance(response, LoginError):
            raise BotError(f"Login failed: {response.message}")

        save_session(
            self.config.session_path,
            Session(
                user_id=response.user_id,
                device_id=response.device_id,
                access_token=response.access_token,
                homeserver=self.config.homeserver,
            ),
        )
        _LOGGER.info(
            "Logged in as %s (device %s)", response.user_id, response.device_id
        )

    async def _validate_token(self) -> bool:
        response = await self.client.whoami()
        if isinstance(response, WhoamiError):
            _LOGGER.debug("whoami failed: %s", response)
            return False
        return True

    # -------------------------------------------------------------- callbacks

    def _register_callbacks(self) -> None:
        client = self.client

        client.add_event_callback(self._on_text, (RoomMessageText, RoomMessageNotice))
        # Encrypted rooms deliver RoomEncrypted* rather than RoomMessage*; missing
        # these is the usual reason attachments appear to vanish.
        client.add_event_callback(
            self._on_media,
            (RoomMessageImage, RoomMessageFile, RoomEncryptedImage, RoomEncryptedFile),
        )
        client.add_response_callback(self._on_sync, SyncResponse)
        client.add_event_callback(self._on_reaction, (ReactionEvent,))
        client.add_event_callback(self._on_undecryptable, (MegolmEvent,))
        client.add_event_callback(self._on_invite, (InviteMemberEvent,))
        client.add_to_device_callback(
            self._on_to_device,
            (
                KeyVerificationStart,
                KeyVerificationKey,
                KeyVerificationMac,
                KeyVerificationCancel,
            ),
        )

    def _should_ignore(self, room: MatrixRoom, event) -> bool:
        if (
            self.config.advanced.ignore_own_messages
            and event.sender == self.client.user_id
        ):
            return True

        if self.config.e2ee.require_verified_sender and getattr(
            room, "encrypted", False
        ):
            device_id = getattr(event, "device_id", None)
            if device_id:
                store = getattr(self.client, "device_store", None)
                device = store[event.sender][device_id] if store else None
                if device is None or not device.verified:
                    _LOGGER.info(
                        "Ignoring message from unverified device %s/%s",
                        event.sender,
                        device_id,
                    )
                    return True
        return False

    def _base_payload(self, room: MatrixRoom, event) -> dict[str, Any]:
        return {
            "room_id": room.room_id,
            "room_name": room.display_name or room.name or room.room_id,
            "sender": event.sender,
            "sender_name": room.user_name(event.sender) or event.sender,
            "event_id": event.event_id,
            "timestamp": getattr(event, "server_timestamp", None),
            "encrypted": bool(getattr(room, "encrypted", False)),
        }

    async def _on_text(self, room: MatrixRoom, event) -> None:
        if self._should_ignore(room, event):
            return
        payload = self._base_payload(room, event)
        payload["body"] = event.body
        payload["notice"] = isinstance(event, RoomMessageNotice)
        self.bus.publish(events.MESSAGE, payload)

    async def _on_media(self, room: MatrixRoom, event) -> None:
        if self._should_ignore(room, event):
            return

        payload = self._base_payload(room, event)
        payload["body"] = getattr(event, "body", "")
        payload["url"] = getattr(event, "url", None)
        payload["mimetype"] = getattr(event, "mimetype", None) or (
            getattr(event, "source", {})
            .get("content", {})
            .get("info", {})
            .get("mimetype")
        )

        try:
            path = await self.media.download(event)
            payload["local_path"] = str(path) if path else None
        except Exception as err:  # pragma: no cover - network/disk failures
            _LOGGER.error("Failed to store inbound attachment: %s", err)
            payload["local_path"] = None
            payload["error"] = str(err)

        is_image = isinstance(event, (RoomMessageImage, RoomEncryptedImage))
        self.bus.publish(events.IMAGE if is_image else events.FILE, payload)

    async def _on_reaction(self, room: MatrixRoom, event) -> None:
        if self._should_ignore(room, event):
            return
        payload = self._base_payload(room, event)
        payload["key"] = getattr(event, "key", None)
        payload["reacts_to"] = getattr(event, "reacts_to", None)
        self.bus.publish(events.REACTION, payload)

    async def _on_undecryptable(self, room: MatrixRoom, event: MegolmEvent) -> None:
        _LOGGER.warning(
            "Could not decrypt an event in %s from %s (session %s)",
            room.room_id,
            event.sender,
            event.session_id,
        )
        payload = self._base_payload(room, event)
        payload["session_id"] = event.session_id
        self.bus.publish(events.UNDECRYPTABLE, payload)

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        # Only react to the invite addressed to us, not other members' state.
        if event.state_key != self.client.user_id or event.membership != "invite":
            return

        # `is_direct` on the invite marks it as a one-to-one conversation.
        is_direct = bool(
            (getattr(event, "source", {}) or {})
            .get("content", {})
            .get("is_direct", False)
        )
        allowed, reason = self.rooms.invite_allowed(
            room.room_id, event.sender, is_direct=is_direct
        )
        if not allowed:
            _LOGGER.warning(
                "Declining invite to %s from %s: %s", room.room_id, event.sender, reason
            )
            self.bus.publish(
                events.ERROR,
                {
                    "context": "invite",
                    "room_id": room.room_id,
                    "sender": event.sender,
                    "message": f"Invite declined: {reason}",
                },
            )
            return

        _LOGGER.info("Joining %s (invited by %s)", room.room_id, event.sender)
        response = await self.client.join(room.room_id)
        if isinstance(response, JoinError):
            _LOGGER.error("Failed to join %s: %s", room.room_id, response.message)
            return

        self.bus.publish(events.ROOMS_CHANGED, {"joined": room.room_id})

    async def _on_sync(self, response: SyncResponse) -> None:
        """Mark the bot healthy again after a failed sync.

        connect() runs once, so without this the bot stayed `connected = False`
        for the life of the process once a sync had failed: the panel and the
        entities showed a stale error, and housekeeping -- which skips while
        disconnected -- never ran again until the add-on was restarted.
        """
        self._backoff = RECONNECT_MIN
        if self.connected:
            return
        self.connected = True
        self.last_error = ""
        _LOGGER.info("Sync recovered; reconnected to %s", self.config.homeserver)
        self.bus.publish(events.STATUS, self.status())

    async def _on_to_device(self, event) -> None:
        await self.verification.handle_to_device(event)

    # ------------------------------------------------------------- sync loop

    def _sync_filter(self) -> dict[str, Any]:
        """Bound how much room backlog each sync pulls down."""
        return {
            "room": {
                "timeline": {"limit": self.config.advanced.message_history_limit},
            }
        }

    async def _sync_loop(self) -> None:
        self._backoff = RECONNECT_MIN
        while not self._stopping:
            try:
                await self.client.sync_forever(
                    timeout=self.config.advanced.sync_timeout,
                    full_state=True,
                    set_presence=self.config.presence,
                    sync_filter=self._sync_filter(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if self._stopping:
                    return
                self.connected = False
                self.last_error = _describe_error(err)
                _LOGGER.error(
                    "Sync failed (%s); reconnecting in %ss",
                    self.last_error,
                    self._backoff,
                )
                self.bus.publish(events.STATUS, self.status())
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, RECONNECT_MAX)
        # No `else`: sync_forever only ever exits by raising, so recovery is
        # observed in _on_sync instead.

    async def housekeeping(self) -> None:
        """Periodic upkeep: trust maintenance, room-change detection, timeouts."""
        while not self._stopping:
            try:
                await asyncio.sleep(10)
                if not self.client or not self.connected:
                    continue

                self.verification.expire()
                self._maintain_trust()
                await self._leave_abandoned_rooms()

                if self.rooms.changed():
                    self.bus.publish(
                        events.ROOMS_CHANGED, {"rooms": self.rooms.describe_all()}
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                _LOGGER.exception("Housekeeping iteration failed")

    async def _leave_abandoned_rooms(self) -> None:
        """Honour `autojoin.leave_if_alone` once everyone else has left."""
        for room_id in self.rooms.abandoned_rooms():
            _LOGGER.info("Leaving %s: no other members remain", room_id)
            await self.client.room_leave(room_id)
            self.bus.publish(events.ROOMS_CHANGED, {"left": room_id})

    def _maintain_trust(self) -> None:
        """Apply auto_trust_own_devices and blacklist_unverified."""
        if not self.config.e2ee.enabled:
            return

        if self.config.e2ee.auto_trust_own_devices:
            store = getattr(self.client, "device_store", None)
            if store is not None:
                for device in store.active_user_devices(self.client.user_id):
                    if device.id != self.client.device_id and not device.verified:
                        self.client.verify_device(device)
                        _LOGGER.info("Auto-trusted own device %s", device.id)

        if self.config.e2ee.blacklist_unverified:
            for room in self.client.rooms.values():
                for info in self.rooms.unverified_devices(room):
                    store = self.client.device_store
                    device = store[info.user_id][info.device_id]
                    if device is not None and not device.blacklisted:
                        self.client.blacklist_device(device)
                        _LOGGER.info(
                            "Blacklisted unverified device %s/%s",
                            info.user_id,
                            info.device_id,
                        )

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        client = self.client
        rooms = self.rooms.describe_all() if self.rooms else []
        unverified = sum(r["unverified_device_count"] for r in rooms)
        return {
            "connected": self.connected,
            # False until the panel has been filled in, which is a different
            # thing from a failed connection and reads differently in the UI.
            "configured": self.config.is_configured,
            "homeserver": self.config.homeserver,
            # nio leaves these empty until login completes, so fall back to the
            # configured identity: the integration derives its unique_id from
            # this, and it may well set up while the bridge is still offline.
            "user_id": getattr(client, "user_id", "") or self.config.user_id,
            "device_id": getattr(client, "device_id", "") or None,
            "encryption_enabled": self.config.e2ee.enabled,
            "room_count": len(rooms),
            "unverified_device_count": unverified,
            "pending_verifications": (
                len(self.verification.pending()) if self.verification else 0
            ),
            "send_to_unverified_rooms": self.config.e2ee.send_to_unverified_rooms,
            "ignore_unverified_devices": self.config.e2ee.ignore_unverified_devices,
            "last_error": self.last_error,
            # Presentation flags the integration honours, so that every setting
            # the operator can change lives in this panel.
            "create_notify_entities": self.config.homeassistant.create_notify_entities,
            "fire_events": self.config.homeassistant.fire_events,
        }

    # -------------------------------------------------------------- outbound

    def _require_room(self, target: str) -> MatrixRoom:
        room_id = self.rooms.resolve(target)
        if room_id is None:
            raise BotError(f"Not in any room matching {target!r}")
        return self.client.rooms[room_id]

    async def _send(self, room: MatrixRoom, content: dict[str, Any]) -> str:
        verdict = self.rooms.evaluate_trust(room)
        if not verdict.allowed:
            raise RoomUntrusted(verdict)

        try:
            response = await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=self.config.e2ee.ignore_unverified_devices,
            )
        except LocalProtocolError as err:
            # nio raises this when unverified devices are present and
            # ignore_unverified_devices is off.
            raise RoomUntrusted(self.rooms.evaluate_trust(room)) from err

        if isinstance(response, RoomSendError):
            raise BotError(f"Send failed: {response.message}")
        return response.event_id

    async def send_message(
        self,
        target: str,
        body: str,
        html: str | None = None,
        reply_to: str | None = None,
        thread_root: str | None = None,
        notice: bool = False,
    ) -> dict[str, Any]:
        room = self._require_room(target)
        content: dict[str, Any] = {
            "msgtype": "m.notice" if notice else "m.text",
            "body": body,
        }
        if html:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = html

        relates: dict[str, Any] = {}
        if reply_to:
            relates["m.in_reply_to"] = {"event_id": reply_to}
        if thread_root:
            relates["rel_type"] = "m.thread"
            relates["event_id"] = thread_root
            if reply_to:
                relates["is_falling_back"] = False
        if relates:
            content["m.relates_to"] = relates

        event_id = await self._send(room, content)
        return {"event_id": event_id, "room_id": room.room_id}

    async def send_media(
        self,
        target: str,
        path: str | None = None,
        url: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        room = self._require_room(target)

        verdict = self.rooms.evaluate_trust(room)
        if not verdict.allowed:
            raise RoomUntrusted(verdict)

        if path:
            media = await self.media.from_path(path)
        elif url:
            media = await self.media.from_url(url)
        else:
            raise MediaError("Provide either 'path' or 'url'")

        if filename:
            media.filename = filename

        content = await self.media.upload(
            media, encrypt=bool(getattr(room, "encrypted", False))
        )
        event_id = await self._send(room, content)

        result = {"event_id": event_id, "room_id": room.room_id}
        if caption:
            caption_result = await self.send_message(target, caption)
            result["caption_event_id"] = caption_result["event_id"]
        return result

    async def react(self, target: str, event_id: str, key: str) -> dict[str, Any]:
        room = self._require_room(target)
        verdict = self.rooms.evaluate_trust(room)
        if not verdict.allowed:
            raise RoomUntrusted(verdict)

        response = await self.client.room_send(
            room_id=room.room_id,
            message_type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": key,
                }
            },
            ignore_unverified_devices=self.config.e2ee.ignore_unverified_devices,
        )
        if isinstance(response, RoomSendError):
            raise BotError(f"Reaction failed: {response.message}")
        return {"event_id": response.event_id, "room_id": room.room_id}

    async def join_room(self, room_id: str) -> dict[str, Any]:
        response = await self.client.join(room_id)
        if isinstance(response, JoinError):
            raise BotError(f"Could not join {room_id}: {response.message}")
        self.bus.publish(events.ROOMS_CHANGED, {"joined": response.room_id})
        return {"room_id": response.room_id}

    async def leave_room(self, target: str) -> dict[str, Any]:
        room = self._require_room(target)
        await self.client.room_leave(room.room_id)
        self.bus.publish(events.ROOMS_CHANGED, {"left": room.room_id})
        return {"room_id": room.room_id}

    async def set_typing(self, target: str, typing: bool) -> dict[str, Any]:
        room = self._require_room(target)
        await self.client.room_typing(room.room_id, typing_state=typing)
        return {"room_id": room.room_id, "typing": typing}

    async def mark_read(self, target: str, event_id: str) -> dict[str, Any]:
        room = self._require_room(target)
        await self.client.update_receipt_marker(room.room_id, event_id)
        return {"room_id": room.room_id, "event_id": event_id}
