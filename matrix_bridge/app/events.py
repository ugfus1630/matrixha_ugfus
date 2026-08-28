"""A tiny in-process pub/sub bus feeding the WebSocket subscribers.

Each subscriber gets its own bounded queue. If a subscriber stops draining (a
wedged integration, a browser tab that was suspended), its oldest events are
dropped rather than allowing unbounded memory growth or back-pressure onto the
Matrix sync loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

_LOGGER = logging.getLogger(__name__)

QUEUE_SIZE = 256

# Event types published on the bus.
STATUS = "status"
ROOMS_CHANGED = "rooms_changed"
MESSAGE = "message"
IMAGE = "image"
FILE = "file"
REACTION = "reaction"
VERIFICATION = "verification"
UNDECRYPTABLE = "undecryptable"
ERROR = "error"


class EventBus:
    """Fan out events to any number of WebSocket subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._dropped = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Publish an event to every subscriber. Never blocks, never raises."""
        envelope = {"type": event_type, "ts": time.time(), "data": data or {}}
        for queue in self._subscribers:
            while True:
                try:
                    queue.put_nowait(envelope)
                    break
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                        self._dropped += 1
                    except asyncio.QueueEmpty:  # pragma: no cover - racy edge
                        break
        if self._dropped and self._dropped % 100 == 0:
            _LOGGER.warning(
                "Dropped %s events to slow subscribers so far", self._dropped
            )

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Register a subscriber queue for the duration of the context."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        _LOGGER.debug("Subscriber added (%s total)", len(self._subscribers))
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            _LOGGER.debug("Subscriber removed (%s left)", len(self._subscribers))
