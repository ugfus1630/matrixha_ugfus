"""HTTP + WebSocket API behind the panel and the Home Assistant integration.

Authentication
--------------
``/api/*`` requires ``Authorization: Bearer <api token>``.

Requests arriving through Home Assistant Ingress are exempt, because Supervisor
has already authenticated the user before proxying. Ingress is identified by
both the ``X-Ingress-Path`` header *and* the peer address being Supervisor
itself — the header alone is trivially spoofable, and the add-on port can be
published by the operator. Combined with ``ports`` defaulting to unpublished,
this keeps the token the only way in from the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from . import events, settings
from .bot import BotError, RoomUntrusted
from .media import MediaError
from .settings import SettingsError
from .verification import VerificationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .bot import MatrixBot
    from .bridge import Bridge
    from .events import EventBus

_LOGGER = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"

# Supervisor's fixed address on the hassio Docker network.
SUPERVISOR_IP = "172.30.32.2"


def _json_error(status: int, message: str, **extra: Any) -> web.Response:
    payload = {"error": message, **extra}
    return web.json_response(payload, status=status)


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Translate domain exceptions into meaningful HTTP responses."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except RoomUntrusted as err:
        # 409: the request was well-formed but the trust policy refuses it.
        return _json_error(
            409,
            str(err),
            code="room_untrusted",
            unverified_devices=[d.as_dict() for d in err.verdict.unverified],
        )
    except SettingsError as err:
        return _json_error(400, str(err), code="invalid_settings")
    except (MediaError, VerificationError, BotError) as err:
        return _json_error(400, str(err), code="request_failed")
    except Exception:  # pragma: no cover - unexpected
        _LOGGER.exception("Unhandled error serving %s", request.path)
        return _json_error(500, "Internal error", code="internal")


def auth_middleware(token: str):
    """Bearer auth for /api/*, bypassed for genuine Ingress traffic."""

    def _is_ingress(request: web.Request) -> bool:
        if "X-Ingress-Path" not in request.headers:
            return False
        peer = request.remote
        return peer == SUPERVISOR_IP or peer in ("127.0.0.1", "::1")

    @web.middleware
    async def middleware(request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/") or path == "/api/health":
            return await handler(request)

        if _is_ingress(request):
            return await handler(request)

        header = request.headers.get("Authorization", "")
        provided = header[7:] if header.startswith("Bearer ") else ""
        if not provided or not secrets.compare_digest(provided, token):
            # Logged because a rejected token and an unreachable add-on look
            # identical to the caller, and the silence here sent operators
            # hunting for a network fault that was not there. The token is never
            # logged; its length is enough to spot a truncated paste.
            _LOGGER.warning(
                "Rejected %s %s from %s: %s",
                request.method,
                path,
                request.remote,
                "no Bearer token"
                if not provided
                else f"token did not match ({len(provided)} chars)",
            )
            return _json_error(401, "Missing or invalid API token", code="unauthorized")

        return await handler(request)

    return middleware


class ApiServer:
    """Wraps the aiohttp application serving the API and the Ingress UI."""

    def __init__(self, bridge: Bridge, bus: EventBus) -> None:
        self._bridge = bridge
        self._bus = bus
        self._runner: web.AppRunner | None = None

    @property
    def _bot(self) -> MatrixBot:
        """The bot currently in use, which saving settings replaces."""
        return self._bridge.bot

    # ------------------------------------------------------------------ setup

    def build_app(self) -> web.Application:
        app = web.Application(
            middlewares=[error_middleware, auth_middleware(self._bridge.api_token)],
            client_max_size=64 * 1024 * 1024,
        )

        app.add_routes(
            [
                web.get("/api/health", self.health),
                web.get("/api/status", self.status),
                web.get("/api/settings", self.get_settings),
                web.post("/api/settings", self.put_settings),
                web.put("/api/settings", self.put_settings),
                web.get("/api/rooms", self.list_rooms),
                web.post("/api/rooms/{room_id}/message", self.send_message),
                web.post("/api/rooms/{room_id}/media", self.send_media),
                web.post("/api/rooms/{room_id}/reaction", self.send_reaction),
                web.post("/api/rooms/{room_id}/typing", self.set_typing),
                web.post("/api/rooms/{room_id}/read", self.mark_read),
                web.post("/api/rooms/{room_id}/leave", self.leave_room),
                web.post("/api/rooms/join", self.join_room),
                web.get("/api/devices", self.list_devices),
                web.post("/api/devices/{user_id}/{device_id}/{action}", self.set_trust),
                web.get("/api/verifications", self.list_verifications),
                web.post("/api/verifications/start", self.start_verification),
                web.post("/api/verifications/{txn}/{action}", self.verification_action),
                web.get("/api/ws", self.websocket),
            ]
        )

        if WEB_ROOT.is_dir():
            app.router.add_get("/", self.index)
            app.router.add_static("/static", WEB_ROOT, name="static")

        return app

    async def start(self) -> None:
        app = self.build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", settings.API_PORT)
        await site.start()
        _LOGGER.info("API listening on port %s", settings.API_PORT)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # --------------------------------------------------------------- handlers

    async def index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEB_ROOT / "index.html")

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "connected": self._bot.connected})

    async def status(self, request: web.Request) -> web.Response:
        return web.json_response(self._bot.status())

    async def get_settings(self, request: web.Request) -> web.Response:
        """Everything the panel needs to render the settings form.

        The stored credentials are included, exactly as the add-on
        configuration tab this replaces did: the caller is either an
        authenticated Home Assistant admin coming through Ingress, or a holder
        of the API token.
        """
        return web.json_response(self._bridge.settings_payload())

    async def put_settings(self, request: web.Request) -> web.Response:
        """Store new settings and reconnect with them.

        Answers with the stored settings and the resulting status, so the panel
        shows the outcome of the reconnect — including a login that failed —
        without a second request.
        """
        body = await self._body(request)
        payload = await self._bridge.apply(body)
        return web.json_response({**payload, "status": self._bot.status()})

    def _require_ready(self) -> None:
        """Guard the actions that need a live Matrix connection.

        Reads deliberately do not use this: while the login is still in flight
        — or has failed — they answer with empty collections so the panel and
        the integration can come up and show `last_error`, rather than getting
        an opaque failure from every endpoint at once.
        """
        if not self._bot.rooms:
            raise BotError("The bot is still connecting; try again shortly.")

    async def list_rooms(self, request: web.Request) -> web.Response:
        rooms = self._bot.rooms.describe_all() if self._bot.rooms else []
        return web.json_response({"rooms": rooms})

    async def _body(self, request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except json.JSONDecodeError as err:
            raise BotError(f"Invalid JSON body: {err}") from err
        if not isinstance(data, dict):
            raise BotError("Request body must be a JSON object")
        return data

    async def send_message(self, request: web.Request) -> web.Response:
        self._require_ready()
        body = await self._body(request)
        message = body.get("body") or body.get("message")
        if not message:
            raise BotError("'body' is required")

        result = await self._bot.send_message(
            target=request.match_info["room_id"],
            body=str(message),
            html=body.get("html"),
            reply_to=body.get("reply_to"),
            thread_root=body.get("thread_root"),
            notice=bool(body.get("notice", False)),
        )
        return web.json_response(result)

    async def send_media(self, request: web.Request) -> web.Response:
        self._require_ready()
        body = await self._body(request)
        result = await self._bot.send_media(
            target=request.match_info["room_id"],
            path=body.get("path"),
            url=body.get("url"),
            caption=body.get("caption"),
            filename=body.get("filename"),
        )
        return web.json_response(result)

    async def send_reaction(self, request: web.Request) -> web.Response:
        self._require_ready()
        body = await self._body(request)
        if not body.get("event_id") or not body.get("key"):
            raise BotError("'event_id' and 'key' are required")
        result = await self._bot.react(
            target=request.match_info["room_id"],
            event_id=body["event_id"],
            key=body["key"],
        )
        return web.json_response(result)

    async def set_typing(self, request: web.Request) -> web.Response:
        self._require_ready()
        body = await self._body(request)
        result = await self._bot.set_typing(
            request.match_info["room_id"], bool(body.get("typing", True))
        )
        return web.json_response(result)

    async def mark_read(self, request: web.Request) -> web.Response:
        self._require_ready()
        body = await self._body(request)
        if not body.get("event_id"):
            raise BotError("'event_id' is required")
        result = await self._bot.mark_read(
            request.match_info["room_id"], body["event_id"]
        )
        return web.json_response(result)

    async def join_room(self, request: web.Request) -> web.Response:
        self._require_ready()
        body = await self._body(request)
        room = body.get("room_id") or body.get("room")
        if not room:
            raise BotError("'room_id' is required")
        return web.json_response(await self._bot.join_room(str(room)))

    async def leave_room(self, request: web.Request) -> web.Response:
        self._require_ready()
        result = await self._bot.leave_room(request.match_info["room_id"])
        return web.json_response(result)

    async def list_devices(self, request: web.Request) -> web.Response:
        if not self._bot.rooms:
            return web.json_response({"devices": []})
        user_id = request.query.get("user_id")
        devices = (
            self._bot.rooms.devices_for(user_id)
            if user_id
            else self._bot.rooms.all_devices()
        )
        return web.json_response({"devices": [d.as_dict() for d in devices]})

    async def set_trust(self, request: web.Request) -> web.Response:
        self._require_ready()
        action = request.match_info["action"]
        if action not in ("verify", "unverify", "blacklist"):
            raise BotError(f"Unknown trust action {action!r}")
        result = self._bot.verification.set_trust(
            request.match_info["user_id"], request.match_info["device_id"], action
        )
        return web.json_response(result)

    async def list_verifications(self, request: web.Request) -> web.Response:
        pending = self._bot.verification.pending() if self._bot.verification else []
        return web.json_response({"verifications": pending})

    async def start_verification(self, request: web.Request) -> web.Response:
        self._require_ready()
        body = await self._body(request)
        if not body.get("user_id"):
            raise BotError("'user_id' is required")
        result = await self._bot.verification.start(
            body["user_id"], body.get("device_id")
        )
        return web.json_response(result)

    async def verification_action(self, request: web.Request) -> web.Response:
        self._require_ready()
        txn = request.match_info["txn"]
        action = request.match_info["action"]
        manager = self._bot.verification

        if action == "accept":
            result = await manager.accept(txn)
        elif action == "confirm":
            result = await manager.confirm(txn)
        elif action == "cancel":
            body = await self._body(request)
            result = await manager.cancel(txn, reject=bool(body.get("reject", True)))
        else:
            raise BotError(f"Unknown verification action {action!r}")
        return web.json_response(result)

    # -------------------------------------------------------------- websocket

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        _LOGGER.debug("WebSocket subscriber connected from %s", request.remote)

        async with self._bus.subscribe() as queue:
            # Prime the client so it has full state without a second round trip.
            await ws.send_json(
                {
                    "type": events.STATUS,
                    "data": self._bot.status(),
                }
            )
            if self._bot.rooms:
                await ws.send_json(
                    {
                        "type": events.ROOMS_CHANGED,
                        "data": {"rooms": self._bot.rooms.describe_all()},
                    }
                )

            reader = asyncio.create_task(self._drain(ws))
            try:
                while not ws.closed:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30)
                    except TimeoutError:
                        continue
                    await ws.send_json(event)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            finally:
                reader.cancel()

        _LOGGER.debug("WebSocket subscriber disconnected")
        return ws

    async def _drain(self, ws: web.WebSocketResponse) -> None:
        """Consume inbound frames so close/ping handling works correctly."""
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
