"""Room discovery and the single trust gate every outbound message passes.

``evaluate_trust`` is deliberately the only place that decides whether the bot
is willing to talk to a room. Both the message and media send paths call it, so
the "don't talk to unverified devices" controls cannot be bypassed by adding a
new send path later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nio import AsyncClient, MatrixRoom

    from .settings import Config

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DeviceInfo:
    user_id: str
    device_id: str
    display_name: str = ""
    verified: bool = False
    blacklisted: bool = False
    ignored: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "device_id": self.device_id,
            "display_name": self.display_name,
            "verified": self.verified,
            "blacklisted": self.blacklisted,
            "ignored": self.ignored,
        }


@dataclass(slots=True)
class TrustVerdict:
    allowed: bool
    reason: str = ""
    unverified: list[DeviceInfo] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "unverified_devices": [d.as_dict() for d in self.unverified],
        }


class RoomRegistry:
    """Projects nio's room state into the shape the integration consumes."""

    def __init__(self, client: AsyncClient, config: Config) -> None:
        self._client = client
        self._config = config
        self._signature: str = ""

    # ------------------------------------------------------------------ rooms

    def room_ids(self) -> list[str]:
        return list(self._client.rooms)

    def resolve(self, target: str) -> str | None:
        """Resolve a room id, canonical alias or display name to a room id."""
        if not target:
            return None
        if target in self._client.rooms:
            return target

        lowered = target.casefold()
        for room_id, room in self._client.rooms.items():
            aliases = {
                getattr(room, "canonical_alias", None),
                *(getattr(room, "alt_aliases", None) or []),
            }
            if target in aliases:
                return room_id
            if (getattr(room, "display_name", "") or "").casefold() == lowered:
                return room_id
            if (getattr(room, "name", "") or "").casefold() == lowered:
                return room_id
        return None

    def describe(self, room: MatrixRoom) -> dict[str, Any]:
        unverified = self.unverified_devices(room)
        return {
            "room_id": room.room_id,
            "name": room.display_name or room.name or room.room_id,
            "alias": getattr(room, "canonical_alias", None),
            "topic": getattr(room, "topic", None),
            "encrypted": bool(getattr(room, "encrypted", False)),
            "member_count": len(getattr(room, "users", {}) or {}),
            "is_dm": len(getattr(room, "users", {}) or {}) == 2,
            "unverified_device_count": len(unverified),
            "unverified_devices": [d.as_dict() for d in unverified],
            "trusted": not unverified,
        }

    def describe_all(self) -> list[dict[str, Any]]:
        rooms = [self.describe(room) for room in self._client.rooms.values()]
        rooms.sort(key=lambda r: r["name"].casefold())
        return rooms

    def signature(self) -> str:
        """A cheap fingerprint used to detect room membership/trust changes."""
        parts = []
        for room_id, room in sorted(self._client.rooms.items()):
            parts.append(
                f"{room_id}:{room.display_name}:{getattr(room, 'encrypted', False)}"
                f":{len(getattr(room, 'users', {}) or {})}"
                f":{len(self.unverified_devices(room))}"
            )
        return "|".join(parts)

    def changed(self) -> bool:
        """Return True (once) when the room set or its trust state has moved."""
        signature = self.signature()
        if signature != self._signature:
            self._signature = signature
            return True
        return False

    # ----------------------------------------------------------------- devices

    def devices_for(self, user_id: str) -> list[DeviceInfo]:
        store = getattr(self._client, "device_store", None)
        if store is None:
            return []
        result = []
        for device in store.active_user_devices(user_id):
            result.append(
                DeviceInfo(
                    user_id=device.user_id,
                    device_id=device.id,
                    display_name=device.display_name or "",
                    verified=device.verified,
                    blacklisted=device.blacklisted,
                    ignored=device.ignored,
                )
            )
        return result

    def all_devices(self) -> list[DeviceInfo]:
        seen: set[str] = set()
        devices: list[DeviceInfo] = []
        for room in self._client.rooms.values():
            for user_id in getattr(room, "users", {}) or {}:
                if user_id in seen:
                    continue
                seen.add(user_id)
                devices.extend(self.devices_for(user_id))
        devices.sort(key=lambda d: (d.user_id, d.device_id))
        return devices

    def unverified_devices(self, room: MatrixRoom) -> list[DeviceInfo]:
        """Unverified devices in a room, excluding our own current device."""
        if not getattr(room, "encrypted", False) or not self._config.e2ee.enabled:
            return []

        own_device = self._client.device_id
        unverified: list[DeviceInfo] = []
        for user_id in getattr(room, "users", {}) or {}:
            for device in self.devices_for(user_id):
                if device.device_id == own_device and user_id == self._client.user_id:
                    continue
                if device.blacklisted or device.ignored:
                    continue
                if not device.verified:
                    unverified.append(device)
        return unverified

    # ------------------------------------------------------------------ trust

    def evaluate_trust(self, room: MatrixRoom) -> TrustVerdict:
        """Decide whether the bot may send into this room.

        This is the single gate behind the `send_to_unverified_rooms` control.
        """
        if not getattr(room, "encrypted", False):
            return TrustVerdict(allowed=True, reason="room_not_encrypted")

        if not self._config.e2ee.enabled:
            return TrustVerdict(
                allowed=False,
                reason=(
                    "This room is encrypted but end-to-end encryption is disabled "
                    "in the Matrix panel's Encryption settings."
                ),
            )

        unverified = self.unverified_devices(room)
        if unverified and not self._config.e2ee.send_to_unverified_rooms:
            names = ", ".join(f"{d.user_id}/{d.device_id}" for d in unverified[:5])
            more = "" if len(unverified) <= 5 else f" (+{len(unverified) - 5} more)"
            return TrustVerdict(
                allowed=False,
                reason=(
                    f"Refusing to send: {len(unverified)} unverified device(s) in "
                    f"this room ({names}{more}). Verify them, or enable "
                    "'Send to rooms with unverified devices' in the Matrix panel."
                ),
                unverified=unverified,
            )

        return TrustVerdict(allowed=True, reason="", unverified=unverified)

    # ----------------------------------------------------------------- invites

    def invite_allowed(
        self, room_id: str, inviter: str, is_direct: bool = False
    ) -> tuple[bool, str]:
        """Apply the autojoin allowlists to an incoming invite."""
        autojoin = self._config.autojoin

        if not autojoin.enabled:
            return False, "autojoin is disabled"

        if len(self._client.rooms) >= autojoin.max_rooms:
            return False, f"already in the maximum of {autojoin.max_rooms} rooms"

        if autojoin.allowed_users and inviter not in autojoin.allowed_users:
            return False, f"{inviter} is not in autojoin.allowed_users"

        if autojoin.allowed_domains:
            domain = inviter.split(":", 1)[-1]
            if domain not in autojoin.allowed_domains:
                return False, f"domain {domain} is not in autojoin.allowed_domains"

        if is_direct and not autojoin.accept_dm_invites:
            return False, "autojoin.accept_dm_invites is disabled"

        return True, ""

    def abandoned_rooms(self) -> list[str]:
        """Rooms where the bot is the only member left.

        Used by the `autojoin.leave_if_alone` control: once everyone else has
        gone, staying joined serves no purpose and keeps the room in the
        notify entity list.
        """
        if not self._config.autojoin.leave_if_alone:
            return []

        return [
            room_id
            for room_id, room in self._client.rooms.items()
            if len(getattr(room, "users", {}) or {}) <= 1
        ]
