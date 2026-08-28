#!/usr/bin/env python3
"""Boot a real Home Assistant against a stubbed add-on and check the entities.

    PYTHONPATH=. python3 dev/test_notify.py

The rest of the dev checks stub Home Assistant out; this one does not, because
the questions worth asking here are Home Assistant's own: what entity ids the
notify entities get, whether `notify.send_message` reaches the bridge, and what
their state is before anything has been sent.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
PORT = 18299

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


STATUS: dict[str, Any] = {
    "connected": True,
    "configured": True,
    "user_id": "@homeassistant:example.org",
    "device_id": "HADEVICE",
    "homeserver": "https://example.org",
    "encryption_enabled": True,
    "room_count": 2,
    "unverified_device_count": 0,
    "pending_verifications": 0,
    "send_to_unverified_rooms": True,
    "ignore_unverified_devices": True,
    "last_error": "",
    "create_notify_entities": True,
    "fire_events": True,
}

ROOMS: list[dict[str, Any]] = [
    {
        "room_id": "!alerts:example.org",
        "name": "Alerts",
        "alias": "#alerts:example.org",
        "topic": None,
        "encrypted": True,
        "member_count": 2,
        "is_dm": False,
        "unverified_device_count": 0,
        "unverified_devices": [],
        "trusted": True,
    },
    {
        "room_id": "!family:example.org",
        "name": "Family Room",
        "alias": None,
        "topic": None,
        "encrypted": True,
        "member_count": 3,
        "is_dm": False,
        "unverified_device_count": 0,
        "unverified_devices": [],
        "trusted": True,
    },
]

SENT: list[dict[str, Any]] = []


async def start_stub_addon(port: int) -> web.AppRunner:
    """The add-on API, reduced to what the integration reads on setup."""

    async def send(request: web.Request) -> web.Response:
        SENT.append({"room": request.match_info["room_id"], **await request.json()})
        return web.json_response({"event_id": "$sent", "room_id": "!x"})

    async def ws(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"type": "status", "data": STATUS})
        await socket.send_json({"type": "rooms_changed", "data": {"rooms": ROOMS}})
        async for _ in socket:
            pass
        return socket

    app = web.Application()
    app.add_routes(
        [
            web.get("/api/status", lambda r: web.json_response(STATUS)),
            web.get("/api/rooms", lambda r: web.json_response({"rooms": ROOMS})),
            web.get("/api/devices", lambda r: web.json_response({"devices": []})),
            web.get(
                "/api/verifications",
                lambda r: web.json_response({"verifications": []}),
            ),
            web.post("/api/rooms/{room_id}/message", send),
            web.get("/api/ws", ws),
        ]
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


async def start_hass(port: int):
    """A minimal Home Assistant with the integration installed."""
    from homeassistant import auth, config_entries, loader
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import category_registry as cr
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import floor_registry as fr
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers import label_registry as lr
    from homeassistant.helpers import restore_state as rs
    from homeassistant.setup import async_setup_component

    config_dir = Path(tempfile.mkdtemp(prefix="matrix-bridge-notify-"))
    (config_dir / "custom_components").mkdir()
    shutil.copytree(
        ROOT / "custom_components" / "matrix_bridge",
        config_dir / "custom_components" / "matrix_bridge",
    )
    (config_dir / "configuration.yaml").write_text("")

    hass = HomeAssistant(str(config_dir))
    hass.config.config_dir = str(config_dir)
    loader.async_setup(hass)
    await asyncio.gather(
        ar.async_load(hass),
        cr.async_load(hass),
        dr.async_load(hass),
        er.async_load(hass),
        fr.async_load(hass),
        ir.async_load(hass),
        lr.async_load(hass),
        rs.async_load(hass),
    )
    hass.auth = await auth.auth_manager_from_config(hass, [], [])
    hass.config_entries = config_entries.ConfigEntries(hass, {"config": {}})
    await hass.config_entries.async_initialize()
    await hass.async_start()

    for domain in ("homeassistant", "network", "sensor", "binary_sensor"):
        await async_setup_component(hass, domain, {})

    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain="matrix_bridge",
        title="Matrix Bridge (@homeassistant:example.org)",
        data={
            "host": "127.0.0.1",
            "port": port,
            "token": "test-token",
            "use_supervisor": False,
        },
        source="user",
        options={},
        unique_id="@homeassistant:example.org",
        discovery_keys={},
        subentries_data=(),
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return hass, entry, config_dir


async def main() -> int:
    addon = await start_stub_addon(PORT)
    hass, entry, config_dir = await start_hass(PORT)

    try:
        from homeassistant.config_entries import ConfigEntryState

        print("\nSetup")
        check("the config entry loads", entry.state is ConfigEntryState.LOADED,
              f"state={entry.state}")

        print("\nNotify entities")
        entity_ids = sorted(
            state.entity_id for state in hass.states.async_all("notify")
        )
        check("one notify entity per room", len(entity_ids) == 2, f"{entity_ids}")

        # The Matrix ID must stay out of the entity id: it is the difference
        # between something findable in the action picker and something nobody
        # can type. See the device page for which account the bridge uses.
        check(
            "entity ids are short and findable",
            entity_ids
            == ["notify.matrix_bridge_alerts", "notify.matrix_bridge_family_room"],
            f"{entity_ids}",
        )

        alerts = hass.states.get("notify.matrix_bridge_alerts")
        check("the room name is the entity name",
              alerts.attributes["friendly_name"] == "Matrix Bridge Alerts",
              alerts.attributes.get("friendly_name", ""))
        check("the room id is exposed as an attribute",
              alerts.attributes.get("room_id") == "!alerts:example.org")
        check("entities are available while the bridge is connected",
              alerts.state != "unavailable")

        # A notify entity's state is the time it last sent something, so
        # `unknown` before the first message is Home Assistant's own behaviour,
        # not a broken entity. Assert it, so nobody "fixes" it into a lie.
        check("state is unknown until something is sent", alerts.state == "unknown")

        print("\nSending")
        services = hass.services.async_services().get("notify", {})
        check("notify.send_message exists", "send_message" in services)
        check(
            "no per-room notify service is registered",
            not [name for name in services if name.startswith("matrix")],
            f"{sorted(services)}",
        )

        await hass.services.async_call(
            "notify",
            "send_message",
            {
                "entity_id": "notify.matrix_bridge_alerts",
                "message": "hello",
                "title": "Test",
            },
            blocking=True,
        )
        await hass.async_block_till_done()

        check("the message reaches the add-on", len(SENT) == 1, f"{SENT}")
        check("it goes to the right room",
              SENT and SENT[0]["room"] == "!alerts:example.org")
        check("the title is prepended to the body",
              SENT and SENT[0]["body"] == "Test\nhello", f"{SENT}")

        after = hass.states.get("notify.matrix_bridge_alerts").state
        check("the state becomes the last-sent timestamp",
              after.startswith("20") and after != "unknown", after)

        print("\nOther entities")
        others = sorted(
            state.entity_id
            for state in hass.states.async_all()
            if state.entity_id.startswith(("sensor.", "binary_sensor."))
        )
        check(
            "sensors are named after the device too",
            all(name.split(".")[1].startswith("matrix_bridge_") for name in others),
            f"{others}",
        )
    finally:
        await hass.async_stop()
        await addon.cleanup()
        shutil.rmtree(config_dir, ignore_errors=True)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
