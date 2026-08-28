"""Interactive SAS (emoji) device verification.

Both directions of the to-device SAS flow are supported:

  inbound   KeyVerificationStart -> accept -> KeyVerificationKey -> emoji shown
            -> operator confirms -> KeyVerificationMac -> device verified
  outbound  start() -> KeyVerificationAccept -> KeyVerificationKey -> emoji
            -> operator confirms -> ...

Note on the nio API: ``create_key_verification``, ``verify_device``,
``blacklist_device`` and ``unverify_device`` are plain synchronous methods that
return a value, while ``accept_key_verification``, ``confirm_short_auth_string``,
``cancel_key_verification`` and ``to_device`` are coroutines. Mixing these up is
the classic way to break this flow.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nio import (
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    ToDeviceError,
)

from . import events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nio import AsyncClient

    from .events import EventBus
    from .settings import Config

_LOGGER = logging.getLogger(__name__)

SAS_SUPPORTED = "emoji"


class VerificationError(Exception):
    """Raised when a verification action cannot be carried out."""


@dataclass(slots=True)
class PendingVerification:
    transaction_id: str
    user_id: str
    device_id: str
    we_started: bool
    stage: str = "requested"
    emoji: list[tuple[str, str]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "we_started": self.we_started,
            "stage": self.stage,
            "emoji": [{"emoji": e, "name": n} for e, n in self.emoji],
            "started_at": self.started_at,
        }


class VerificationManager:
    """Tracks in-flight SAS verifications and exposes trust management."""

    def __init__(self, client: AsyncClient, config: Config, bus: EventBus) -> None:
        self._client = client
        self._config = config
        self._bus = bus
        self._pending: dict[str, PendingVerification] = {}

    # ----------------------------------------------------------------- helpers

    def pending(self) -> list[dict[str, Any]]:
        return [p.as_dict() for p in self._pending.values()]

    def _publish(self, pending: PendingVerification) -> None:
        self._bus.publish(events.VERIFICATION, pending.as_dict())

    def _device(self, user_id: str, device_id: str):
        store = getattr(self._client, "device_store", None)
        device = store[user_id][device_id] if store else None
        if device is None:
            raise VerificationError(f"Unknown device {device_id} for {user_id}")
        return device

    def _sas(self, transaction_id: str):
        sas = self._client.key_verifications.get(transaction_id)
        if sas is None:
            raise VerificationError(f"No active verification {transaction_id}")
        return sas

    async def _send(self, message) -> None:
        response = await self._client.to_device(message)
        if isinstance(response, ToDeviceError):
            raise VerificationError(f"Failed to send verification message: {response}")

    # ------------------------------------------------------------------ actions

    async def start(self, user_id: str, device_id: str | None = None) -> dict[str, Any]:
        """Begin verifying another device (usually the operator's own client)."""
        if not self._config.e2ee.enabled:
            raise VerificationError("End-to-end encryption is disabled")

        if device_id is None:
            store = getattr(self._client, "device_store", None)
            candidates = list(store.active_user_devices(user_id)) if store else []
            unverified = [d for d in candidates if not d.verified]
            if len(unverified) != 1:
                raise VerificationError(
                    f"Specify a device_id: {user_id} has {len(unverified)} "
                    "unverified devices"
                )
            device = unverified[0]
        else:
            device = self._device(user_id, device_id)

        # Synchronous: returns the ToDeviceMessage to send, does not send it.
        message = self._client.create_key_verification(device)
        await self._send(message)

        transaction_id = message.content["transaction_id"]
        pending = PendingVerification(
            transaction_id=transaction_id,
            user_id=user_id,
            device_id=device.id,
            we_started=True,
            stage="started",
        )
        self._pending[transaction_id] = pending
        self._publish(pending)
        _LOGGER.info("Started verification with %s/%s", user_id, device.id)
        return pending.as_dict()

    async def accept(self, transaction_id: str) -> dict[str, Any]:
        """Accept an inbound verification request."""
        self._sas(transaction_id)
        response = await self._client.accept_key_verification(transaction_id)
        if isinstance(response, ToDeviceError):
            raise VerificationError(f"Could not accept verification: {response}")

        pending = self._pending.get(transaction_id)
        if pending:
            pending.stage = "accepted"
            self._publish(pending)

        # Our public key must follow the acceptance before emoji can be derived.
        sas = self._sas(transaction_id)
        await self._send(sas.share_key())
        return pending.as_dict() if pending else {"transaction_id": transaction_id}

    async def confirm(self, transaction_id: str) -> dict[str, Any]:
        """Operator has confirmed the emoji match on both devices."""
        self._sas(transaction_id)
        response = await self._client.confirm_short_auth_string(transaction_id)
        if isinstance(response, ToDeviceError):
            raise VerificationError(f"Could not confirm verification: {response}")

        pending = self._pending.get(transaction_id)
        if pending:
            pending.stage = "confirmed"
            self._publish(pending)
        return pending.as_dict() if pending else {"transaction_id": transaction_id}

    async def cancel(self, transaction_id: str, reject: bool = True) -> dict[str, Any]:
        """Cancel a verification, optionally signalling an emoji mismatch."""
        response = await self._client.cancel_key_verification(
            transaction_id, reject=reject
        )
        if isinstance(response, ToDeviceError):
            raise VerificationError(f"Could not cancel verification: {response}")

        pending = self._pending.pop(transaction_id, None)
        if pending:
            pending.stage = "rejected" if reject else "cancelled"
            self._publish(pending)
        return {"transaction_id": transaction_id, "cancelled": True}

    def set_trust(self, user_id: str, device_id: str, state: str) -> dict[str, Any]:
        """Directly set a device's trust state without the SAS dance."""
        device = self._device(user_id, device_id)

        # These three are synchronous in nio despite the async client.
        if state == "verify":
            self._client.verify_device(device)
        elif state == "unverify":
            self._client.unverify_device(device)
        elif state == "blacklist":
            self._client.blacklist_device(device)
        else:
            raise VerificationError(
                f"Unknown trust state {state!r}; expected verify, unverify or blacklist"
            )

        _LOGGER.info("Set %s/%s trust to %s", user_id, device_id, state)
        self._bus.publish(
            events.VERIFICATION,
            {"user_id": user_id, "device_id": device_id, "stage": state},
        )
        return {"user_id": user_id, "device_id": device_id, "state": state}

    def expire(self) -> None:
        """Drop verifications the operator never answered."""
        timeout = self._config.e2ee.verification_timeout
        now = time.time()
        for transaction_id, pending in list(self._pending.items()):
            if now - pending.started_at > timeout:
                _LOGGER.info("Verification %s timed out", transaction_id)
                pending.stage = "timed_out"
                self._publish(pending)
                self._pending.pop(transaction_id, None)

    # ---------------------------------------------------------------- callbacks

    async def handle_to_device(self, event) -> None:
        """Drive the SAS state machine from inbound to-device events."""
        try:
            await self._handle_to_device(event)
        except VerificationError as err:
            _LOGGER.error("Verification error: %s", err)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("Unexpected error handling verification event")

    async def _handle_to_device(self, event) -> None:
        transaction_id = getattr(event, "transaction_id", None)
        if not transaction_id:
            return

        if isinstance(event, KeyVerificationStart):
            if SAS_SUPPORTED not in event.short_authentication_string:
                await self.cancel(transaction_id, reject=False)
                _LOGGER.warning(
                    "Rejecting verification from %s: emoji SAS not offered",
                    event.sender,
                )
                return

            pending = PendingVerification(
                transaction_id=transaction_id,
                user_id=event.sender,
                device_id=event.from_device,
                we_started=False,
                stage="requested",
            )
            self._pending[transaction_id] = pending
            self._publish(pending)
            _LOGGER.info(
                "Verification requested by %s/%s", event.sender, event.from_device
            )

            if self._config.e2ee.auto_accept_verification:
                await self.accept(transaction_id)

        elif isinstance(event, KeyVerificationKey):
            sas = self._sas(transaction_id)
            pending = self._pending.get(transaction_id)
            if pending is None:
                return
            pending.emoji = sas.get_emoji()
            pending.stage = "emoji_ready"
            self._publish(pending)
            _LOGGER.info(
                "Verification %s emoji: %s",
                transaction_id,
                " ".join(e for e, _ in pending.emoji),
            )

        elif isinstance(event, KeyVerificationMac):
            sas = self._sas(transaction_id)
            pending = self._pending.get(transaction_id)
            try:
                message = sas.get_mac()
            except Exception as err:  # nio raises if the SAS is not yet verified
                _LOGGER.debug("MAC not ready for %s: %s", transaction_id, err)
                return

            await self._send(message)
            if pending:
                pending.stage = "verified"
                self._publish(pending)
                self._pending.pop(transaction_id, None)
                _LOGGER.info(
                    "Verified %s/%s", pending.user_id, pending.device_id
                )
            self._bus.publish(events.ROOMS_CHANGED, {})

        elif isinstance(event, KeyVerificationCancel):
            pending = self._pending.pop(transaction_id, None)
            if pending:
                pending.stage = "cancelled"
                self._publish(pending)
            _LOGGER.info(
                "Verification %s cancelled: %s",
                transaction_id,
                getattr(event, "reason", "no reason given"),
            )
