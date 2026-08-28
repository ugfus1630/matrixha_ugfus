"""The running bridge: the current settings, and the bot running with them.

Settings are edited in the panel while the add-on is running, so something has
to own *the current bot* and be able to replace it. That is this class. The API
holds a Bridge rather than a MatrixBot, so saving settings swaps the bot
underneath it — new account, new encryption options, new anything — without the
operator restarting the container.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from . import events, settings
from .bot import BotError, MatrixBot
from .events import EventBus
from .settings import Config

_LOGGER = logging.getLogger(__name__)

NOT_CONFIGURED = (
    "Not set up yet: open Settings below and fill in the homeserver, the bot's "
    "Matrix ID and a password."
)


class Bridge:
    """Owns the bot, and applies new settings to it in place."""

    def __init__(self, config: Config, bus: EventBus, api_token: str) -> None:
        self.config = config
        self.bus = bus
        self.api_token = api_token
        self.bot = MatrixBot(config, bus)
        self._housekeeping: asyncio.Task | None = None
        # Serialises saves against each other and against shutdown, so two
        # rapid Saves cannot leave two bots syncing the same crypto store.
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        async with self._lock:
            await self._start_bot()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_bot()

    async def _start_bot(self) -> None:
        """Bring the bot up, recording any failure instead of raising it.

        Nothing here can stop the add-on: a bad homeserver, a wrong password or
        no settings at all leave the panel and the API up, with the reason in
        `last_error` where the operator can see and fix it.
        """
        if self.config.load_error:
            self.bot.last_error = self.config.load_error
        elif not self.config.is_configured:
            self.bot.last_error = NOT_CONFIGURED
            _LOGGER.warning("%s", NOT_CONFIGURED)
        else:
            try:
                await self.bot.start()
            except BotError as err:
                _LOGGER.error("Could not connect to Matrix: %s", err)
                self.bot.last_error = str(err)
            except Exception as err:  # noqa: BLE001 - a failure here is reportable, not fatal
                _LOGGER.exception("Unexpected failure while connecting to Matrix")
                self.bot.last_error = f"Unexpected failure while connecting: {err}"
            else:
                self._housekeeping = asyncio.create_task(
                    self.bot.housekeeping(), name="housekeeping"
                )

        self.bus.publish(events.STATUS, self.status())

    async def _stop_bot(self) -> None:
        if self._housekeeping:
            self._housekeeping.cancel()
            self._housekeeping = None
        await self.bot.stop()

    # --------------------------------------------------------------- settings

    def status(self) -> dict[str, Any]:
        return self.bot.status()

    def settings_payload(self) -> dict[str, Any]:
        """Current settings plus the form definition the panel renders."""
        return {
            "settings": settings.to_dict(self.config),
            "form": settings.describe(),
            "api_token": self.api_token,
            "configured": self.config.is_configured,
        }

    async def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate and store new settings, then reconnect with them.

        Raises SettingsError if the submission is unusable, in which case
        nothing is written and the running bot is left alone.
        """
        # Validated against a copy: a rejected submission must not leave the
        # live config half-updated.
        candidate = settings.validate(raw, copy.deepcopy(self.config))

        async with self._lock:
            settings.save(candidate)
            settings.configure_logging(candidate.advanced.log_level)
            _LOGGER.info("Settings saved; reconnecting")

            await self._stop_bot()
            self.config = candidate
            self.bot = MatrixBot(candidate, self.bus)
            await self._start_bot()

        return self.settings_payload()
