"""Thin async client for the Matrix Bridge add-on API."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)


class BridgeError(Exception):
    """A request to the add-on failed."""


class BridgeAuthError(BridgeError):
    """The API token was rejected."""


class RoomUntrustedError(BridgeError):
    """The add-on's trust policy refused to send into the room."""

    def __init__(self, message: str, unverified: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.unverified = unverified


class BridgeClient:
    """REST + WebSocket client for a single add-on instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str,
    ) -> None:
        self._session = session
        self._base = URL(f"http://{host}:{port}")
        self._token = token

    @property
    def ws_url(self) -> URL:
        return self._base.with_scheme("ws") / "api/ws"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = self._base / path.lstrip("/")
        try:
            async with self._session.request(
                method, url, json=payload, headers=self.headers, timeout=TIMEOUT
            ) as response:
                text = await response.text()
                body: dict[str, Any] = {}
                if text:
                    try:
                        body = await response.json(content_type=None)
                    except ValueError:
                        body = {"error": text}

                if response.status == 401:
                    raise BridgeAuthError("The add-on rejected the API token")
                if response.status == 409 and body.get("code") == "room_untrusted":
                    raise RoomUntrustedError(
                        body.get("error", "Room is not trusted"),
                        body.get("unverified_devices", []),
                    )
                if response.status >= 400:
                    raise BridgeError(
                        body.get("error") or f"HTTP {response.status} from add-on"
                    )
                return body
        except aiohttp.ClientError as err:
            raise BridgeError(f"Cannot reach the Matrix Bridge add-on: {err}") from err
        except TimeoutError as err:
            raise BridgeError("Timed out talking to the Matrix Bridge add-on") from err

    # ------------------------------------------------------------------ reads

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "api/status")

    async def rooms(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "api/rooms")
        return data.get("rooms", [])

    async def devices(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "api/devices")
        return data.get("devices", [])

    async def verifications(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "api/verifications")
        return data.get("verifications", [])

    # ----------------------------------------------------------------- writes

    async def send_message(self, room: str, **payload: Any) -> dict[str, Any]:
        return await self._request(
            "POST", f"api/rooms/{room}/message", {k: v for k, v in payload.items() if v}
        )

    async def send_media(self, room: str, **payload: Any) -> dict[str, Any]:
        return await self._request(
            "POST", f"api/rooms/{room}/media", {k: v for k, v in payload.items() if v}
        )

    async def react(self, room: str, event_id: str, key: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"api/rooms/{room}/reaction", {"event_id": event_id, "key": key}
        )

    async def join_room(self, room_id: str) -> dict[str, Any]:
        return await self._request("POST", "api/rooms/join", {"room_id": room_id})

    async def leave_room(self, room: str) -> dict[str, Any]:
        return await self._request("POST", f"api/rooms/{room}/leave")

    async def start_verification(
        self, user_id: str, device_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"user_id": user_id}
        if device_id:
            payload["device_id"] = device_id
        return await self._request("POST", "api/verifications/start", payload)

    async def verification_action(
        self, transaction_id: str, action: str, **payload: Any
    ) -> dict[str, Any]:
        return await self._request(
            "POST", f"api/verifications/{transaction_id}/{action}", payload
        )

    async def set_device_trust(
        self, user_id: str, device_id: str, state: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST", f"api/devices/{user_id}/{device_id}/{state}"
        )
