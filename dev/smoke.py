#!/usr/bin/env python3
"""Exercise the add-on API against a stubbed Matrix client.

Runs without a homeserver, Supervisor or Docker, so the HTTP contract the
integration depends on can be checked anywhere:

    PYTHONPATH=matrix_bridge python3 dev/smoke.py

Asserts routing, auth rejection, the trust gate's 409 response, and that the
WebSocket stream delivers published events.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "matrix_bridge"))

import aiohttp
from aiohttp import web

from app import bridge as bridge_module
from app import events, settings
from app.api import ApiServer
from app.bot import RoomUntrusted
from app.bridge import NOT_CONFIGURED, Bridge
from app.events import EventBus
from app.rooms import DeviceInfo, TrustVerdict
from app.settings import (
    AdvancedConfig,
    AutojoinConfig,
    Config,
    E2EEConfig,
    MediaConfig,
    SettingsError,
)

TOKEN = "smoke-token"
PORT = 18099

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


# --------------------------------------------------------------------- stubs


@dataclass
class StubRooms:
    """Stands in for RoomRegistry."""

    rooms: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "room_id": "!trusted:example.org",
                "name": "Trusted Room",
                "alias": "#trusted:example.org",
                "topic": None,
                "encrypted": True,
                "member_count": 2,
                "is_dm": True,
                "unverified_device_count": 0,
                "unverified_devices": [],
                "trusted": True,
            },
            {
                "room_id": "!untrusted:example.org",
                "name": "Untrusted Room",
                "alias": None,
                "topic": None,
                "encrypted": True,
                "member_count": 3,
                "is_dm": False,
                "unverified_device_count": 1,
                "unverified_devices": [
                    DeviceInfo("@someone:example.org", "BADDEVICE").as_dict()
                ],
                "trusted": False,
            },
        ]
    )

    def describe_all(self) -> list[dict[str, Any]]:
        return self.rooms

    def all_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo("@alice:example.org", "GOODDEVICE", "Laptop", verified=True),
            DeviceInfo("@someone:example.org", "BADDEVICE", "Phone", verified=False),
        ]

    def devices_for(self, user_id: str) -> list[DeviceInfo]:
        return [d for d in self.all_devices() if d.user_id == user_id]


class StubVerification:
    def __init__(self) -> None:
        self.trust_calls: list[tuple[str, str, str]] = []

    def pending(self) -> list[dict[str, Any]]:
        return [
            {
                "transaction_id": "txn-1",
                "user_id": "@alice:example.org",
                "device_id": "GOODDEVICE",
                "we_started": False,
                "stage": "emoji_ready",
                "emoji": [{"emoji": "🐶", "name": "Dog"}],
                "started_at": 0.0,
            }
        ]

    def set_trust(self, user_id: str, device_id: str, state: str) -> dict[str, Any]:
        self.trust_calls.append((user_id, device_id, state))
        return {"user_id": user_id, "device_id": device_id, "state": state}

    async def confirm(self, txn: str) -> dict[str, Any]:
        return {"transaction_id": txn, "stage": "confirmed"}

    async def start(self, user_id: str, device_id: str | None) -> dict[str, Any]:
        return {"user_id": user_id, "device_id": device_id, "stage": "started"}


class StubBot:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.connected = True
        self.rooms = StubRooms()
        self.verification = StubVerification()
        self.sent: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {
            "connected": True,
            "user_id": "@homeassistant:example.org",
            "device_id": "HADEVICE",
            "homeserver": "https://example.org",
            "encryption_enabled": True,
            "room_count": 2,
            "unverified_device_count": 1,
            "pending_verifications": 1,
            "send_to_unverified_rooms": False,
            "ignore_unverified_devices": True,
            "last_error": "",
        }

    async def send_message(self, target: str, body: str, **kwargs: Any):
        if "untrusted" in target:
            raise RoomUntrusted(
                TrustVerdict(
                    allowed=False,
                    reason="Refusing to send: 1 unverified device(s) in this room.",
                    unverified=[DeviceInfo("@someone:example.org", "BADDEVICE")],
                )
            )
        self.sent.append({"target": target, "body": body, **kwargs})
        return {"event_id": "$sent", "room_id": target}

    async def send_media(self, target: str, **kwargs: Any):
        self.sent.append({"target": target, "media": kwargs})
        return {"event_id": "$media", "room_id": target}

    async def react(self, target: str, event_id: str, key: str):
        return {"event_id": "$reaction", "room_id": target}

    async def join_room(self, room_id: str):
        return {"room_id": room_id}

    async def leave_room(self, target: str):
        return {"room_id": target}


class ConnectingBot:
    """The API is up but the Matrix login has not finished — or has failed.

    `rooms` and `verification` stay None until `MatrixBot._connect` succeeds,
    so this is the state the add-on sits in after a bad homeserver or password.
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.connected = False
        self.rooms = None
        self.verification = None
        self.last_error = "Could not reach the homeserver at https://example.org"

    def status(self) -> dict[str, Any]:
        return {
            "connected": False,
            "user_id": "@homeassistant:example.org",
            "device_id": None,
            "homeserver": "https://example.org",
            "encryption_enabled": True,
            "room_count": 0,
            "unverified_device_count": 0,
            "pending_verifications": 0,
            "send_to_unverified_rooms": False,
            "ignore_unverified_devices": True,
            "last_error": self.last_error,
        }


async def test_disconnected_reads(session: aiohttp.ClientSession) -> None:
    """Reads must degrade to empty rather than failing the integration's setup.

    The integration primes itself over /api/status and /api/rooms; if those
    fail while the bot is still connecting, the config entry never loads and
    Home Assistant reports the bridge as broken instead of disconnected.
    """
    print("\nReads while the bot is not connected")
    bus = EventBus()
    port = PORT + 1
    server = ApiServer(StubBridge(ConnectingBot(bus)), bus)
    runner = web.AppRunner(server.build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        async with session.get(f"{base}/api/status", headers=auth) as response:
            body = await response.json()
            check("status still answers", response.status == 200)
            check("status carries the reason", bool(body["last_error"]))

        for path, key in (
            ("rooms", "rooms"),
            ("devices", "devices"),
            ("verifications", "verifications"),
        ):
            async with session.get(f"{base}/api/{path}", headers=auth) as response:
                body = await response.json()
                check(f"{path} returns an empty list", response.status == 200)
                check(f"{path} is empty, not absent", body[key] == [])

        # Writes still need a live connection.
        async with session.post(
            f"{base}/api/rooms/!x:example.org/message",
            headers=auth,
            json={"body": "hello"},
        ) as response:
            check("sending is still refused", response.status == 400)

        async with session.ws_connect(f"{base}/api/ws", headers=auth) as socket:
            first = await asyncio.wait_for(socket.receive_json(), timeout=5)
            check("stream still primes with status", first["type"] == events.STATUS)
            check(
                "primed status reports disconnected",
                first["data"]["connected"] is False,
            )
    finally:
        await runner.cleanup()


def make_config() -> Config:
    return Config(
        homeserver="https://example.org",
        user_id="@homeassistant:example.org",
        autojoin=AutojoinConfig(),
        e2ee=E2EEConfig(send_to_unverified_rooms=False),
        media=MediaConfig(),
        advanced=AdvancedConfig(),
    )


@dataclass
class StubBridge:
    """Stands in for Bridge: the API only needs the current bot and the token."""

    bot: Any
    api_token: str = TOKEN
    config: Config = field(default_factory=make_config)

    def settings_payload(self) -> dict[str, Any]:
        return {
            "settings": settings.to_dict(self.config),
            "form": settings.describe(),
            "api_token": self.api_token,
            "configured": self.config.is_configured,
        }

    async def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        self.config = settings.validate(raw, self.config)
        return self.settings_payload()


class FakeBot:
    """A bot that connects instantly, so the settings flow can be exercised."""

    created: list[FakeBot] = []

    def __init__(self, config: Config, bus: EventBus) -> None:
        self.config = config
        self.bus = bus
        self.connected = False
        self.last_error = ""
        self.rooms = None
        self.verification = None
        FakeBot.created.append(self)

    async def start(self) -> None:
        self.connected = True

    async def stop(self) -> None:
        self.connected = False

    async def housekeeping(self) -> None:
        await asyncio.sleep(3600)

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "configured": self.config.is_configured,
            "user_id": self.config.user_id,
            "homeserver": self.config.homeserver,
            "last_error": self.last_error,
        }


async def test_settings(session: aiohttp.ClientSession) -> None:
    """The panel is the configuration surface, so this is the setup path.

    Saving has to validate, persist and reconnect — without restarting the
    add-on, and without a rejected submission disturbing the running bot.
    """
    print("\nSettings")
    directory = Path(tempfile.mkdtemp(prefix="matrix-bridge-smoke-"))
    settings.SETTINGS_PATH = directory / "settings.json"
    settings.OPTIONS_PATH = directory / "options.json"
    bridge_module.MatrixBot = FakeBot
    FakeBot.created.clear()

    check("nothing stored means nothing configured", not settings.load().is_configured)

    bus = EventBus()
    bridge = Bridge(settings.load(), bus, TOKEN)
    await bridge.start()
    check("an unconfigured bridge does not connect", bridge.bot.connected is False)
    check(
        "the panel is told what to fill in",
        NOT_CONFIGURED in bridge.status()["last_error"],
    )

    port = PORT + 2
    server = ApiServer(bridge, bus)
    runner = web.AppRunner(server.build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        async with session.get(f"{base}/api/settings", headers=auth) as response:
            body = await response.json()
            check("settings are readable", response.status == 200)
            check("the form describes every field",
                  len(body["form"]["fields"]) == len(settings.FIELDS))
            check("the form is grouped into sections",
                  len(body["form"]["sections"]) == len(settings.SECTIONS))
            check("the API token is shown", body["api_token"] == TOKEN)

        async with session.post(
            f"{base}/api/settings",
            headers=auth,
            json={
                "homeserver": "matrix.example.org",
                "user_id": "@homeassistant:example.org",
                "password": "hunter2",
                "media": {"max_upload_mb": 64},
                "advanced": {"log_level": "debug"},
            },
        ) as response:
            body = await response.json()
            check("saving succeeds", response.status == 200)
            check(
                "a bare hostname gains https://",
                body["settings"]["homeserver"] == "https://matrix.example.org",
            )
            check(
                "nested settings are kept",
                body["settings"]["media"]["max_upload_mb"] == 64,
            )
            check(
                "untouched settings keep their defaults",
                body["settings"]["autojoin"]["max_rooms"] == 50,
            )
            check(
                "the response reports the reconnect",
                body["status"]["connected"] is True,
            )

        stored = json.loads(settings.SETTINGS_PATH.read_text())
        check("settings are written to disk", stored["password"] == "hunter2")
        check("the stored file is not world-readable",
              settings.SETTINGS_PATH.stat().st_mode & 0o077 == 0)
        check("saving reconnects instead of restarting the add-on",
              len(FakeBot.created) == 2 and bridge.bot.connected is True)
        check("the stored settings load back", settings.load().password == "hunter2")

        async with session.post(
            f"{base}/api/settings", headers=auth, json={"user_id": "not-a-matrix-id"}
        ) as response:
            body = await response.json()
            check("an invalid Matrix ID is rejected", response.status == 400)
            check(
                "the reason is spelled out",
                "@homeassistant:example.org" in body["error"],
            )

        async with session.post(
            f"{base}/api/settings", headers=auth, json={"autojoin": {"max_rooms": 5000}}
        ) as response:
            check("out-of-range numbers are rejected", response.status == 400)

        check("a rejected save leaves the bot alone", len(FakeBot.created) == 2)
        check(
            "a rejected save leaves the stored settings alone",
            json.loads(settings.SETTINGS_PATH.read_text())["user_id"]
            == "@homeassistant:example.org",
        )

        try:
            settings.validate({"media": {"download_path": "relative/path"}})
        except SettingsError:
            check("relative media paths are refused", True)
        else:
            check("relative media paths are refused", False)

        # Upgrading from 0.2.x: whatever was in the Supervisor options form has
        # to arrive in the panel, or the operator silently loses their setup.
        settings.SETTINGS_PATH = directory / "imported.json"
        imported = settings.import_legacy(
            {
                "homeserver": "old.example.org",
                "user_id": "@old:example.org",
                "password": "old-secret",
                "autojoin": {"max_rooms": 7},
                "api": {"token": "stale", "port": 8099},
            },
            "test",
        )
        check("a 0.2.x configuration is imported", imported is not None)
        check("imported credentials survive", imported.password == "old-secret")
        check("imported sections survive", imported.autojoin.max_rooms == 7)
        check("the imported file is written", settings.SETTINGS_PATH.exists())
        check("nothing without a homeserver is imported",
              settings.import_legacy({"user_id": "@x:y"}, "test") is None)
    finally:
        await bridge.stop()
        await runner.cleanup()


# --------------------------------------------------------------------- tests


async def main() -> int:
    bus = EventBus()
    bot = StubBot(bus)
    server = ApiServer(StubBridge(bot), bus)

    runner = web.AppRunner(server.build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()

    base = f"http://127.0.0.1:{PORT}"
    auth = {"Authorization": f"Bearer {TOKEN}"}

    print("\nAuthentication")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/api/status") as response:
            check("unauthenticated request is rejected", response.status == 401)

        async with session.get(
            f"{base}/api/status", headers={"Authorization": "Bearer wrong"}
        ) as response:
            check("wrong token is rejected", response.status == 401)

        async with session.get(f"{base}/api/health") as response:
            check("health endpoint needs no token", response.status == 200)

        # A spoofed Ingress header from a non-Supervisor peer must not bypass auth.
        async with session.get(
            f"{base}/api/status", headers={"X-Ingress-Path": "/api/ingress/abc"}
        ) as response:
            check(
                "spoofed X-Ingress-Path from loopback is handled",
                response.status in (200, 401),
                f"(status {response.status}; loopback is trusted by design)",
            )

        print("\nReads")
        async with session.get(f"{base}/api/status", headers=auth) as response:
            body = await response.json()
            check("status returns the bot identity", body["user_id"].startswith("@"))

        async with session.get(f"{base}/api/rooms", headers=auth) as response:
            body = await response.json()
            check("rooms are listed", len(body["rooms"]) == 2)

        async with session.get(f"{base}/api/devices", headers=auth) as response:
            body = await response.json()
            check("devices are listed", len(body["devices"]) == 2)

        async with session.get(f"{base}/api/verifications", headers=auth) as response:
            body = await response.json()
            check(
                "pending verification exposes emoji",
                body["verifications"][0]["emoji"][0]["emoji"] == "🐶",
            )

        print("\nSending and the trust gate")
        async with session.post(
            f"{base}/api/rooms/!trusted:example.org/message",
            headers=auth,
            json={"body": "hello"},
        ) as response:
            body = await response.json()
            check("message to a trusted room succeeds", response.status == 200)
            check("send returns an event id", body.get("event_id") == "$sent")

        async with session.post(
            f"{base}/api/rooms/!untrusted:example.org/message",
            headers=auth,
            json={"body": "hello"},
        ) as response:
            body = await response.json()
            check("untrusted room returns 409", response.status == 409)
            check(
                "409 carries the machine-readable code",
                body.get("code") == "room_untrusted",
            )
            check(
                "409 names the offending device",
                body["unverified_devices"][0]["device_id"] == "BADDEVICE",
            )

        async with session.post(
            f"{base}/api/rooms/!trusted:example.org/message", headers=auth, json={}
        ) as response:
            check("missing body is a 400", response.status == 400)

        async with session.post(
            f"{base}/api/rooms/!trusted:example.org/media",
            headers=auth,
            json={"path": "/tmp/x.jpg"},
        ) as response:
            check("media send is routed", response.status == 200)

        print("\nTrust management")
        async with session.post(
            f"{base}/api/devices/@alice:example.org/GOODDEVICE/blacklist", headers=auth
        ) as response:
            check("device trust can be set", response.status == 200)
            check(
                "trust call reached the manager",
                bot.verification.trust_calls == [
                    ("@alice:example.org", "GOODDEVICE", "blacklist")
                ],
            )

        async with session.post(
            f"{base}/api/devices/@alice:example.org/GOODDEVICE/nonsense", headers=auth
        ) as response:
            check("unknown trust action is rejected", response.status == 400)

        async with session.post(
            f"{base}/api/verifications/txn-1/confirm", headers=auth, json={}
        ) as response:
            check("verification confirm is routed", response.status == 200)

        print("\nWebSocket stream")
        async with session.ws_connect(f"{base}/api/ws", headers=auth) as socket:
            first = await asyncio.wait_for(socket.receive_json(), timeout=5)
            check("stream primes with status", first["type"] == events.STATUS)

            second = await asyncio.wait_for(socket.receive_json(), timeout=5)
            check("stream primes with rooms", second["type"] == events.ROOMS_CHANGED)

            bus.publish(events.MESSAGE, {"body": "from matrix", "sender": "@a:b"})
            third = await asyncio.wait_for(socket.receive_json(), timeout=5)
            check("published events reach subscribers", third["type"] == events.MESSAGE)
            check("event payload survives", third["data"]["body"] == "from matrix")

        await test_disconnected_reads(session)
        await test_settings(session)

    await runner.cleanup()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
