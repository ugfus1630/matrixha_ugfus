"""Add-on entrypoint: serve the panel and the API, and keep the bridge running.

Startup deliberately cannot fail. The panel is where the bridge is configured,
so it comes up first and stays up: an unset, wrong or unreachable homeserver
shows as an error inside the panel rather than as a stopped add-on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from . import settings, supervisor
from .api import ApiServer
from .bridge import Bridge
from .events import EventBus

_LOGGER = logging.getLogger("matrix_bridge")


async def run() -> int:
    settings.configure_logging("info")
    config = settings.load()
    settings.configure_logging(config.advanced.log_level)

    # Second half of the 0.2.x import: what settings.load could not find in
    # /data/options.json may still be held by Supervisor. Both paths are
    # no-ops once settings.json exists.
    if not settings.SETTINGS_PATH.exists():
        imported = settings.import_legacy(
            await supervisor.fetch_own_options(), "the add-on's Supervisor options"
        )
        if imported is not None:
            config = imported
            settings.configure_logging(config.advanced.log_level)

    api_token, generated = settings.resolve_api_token()
    if generated:
        _LOGGER.info("Starting Matrix Bridge")
    else:
        _LOGGER.info("Starting Matrix Bridge for %s", config.user_id or "(not set up)")

    bus = EventBus()
    bridge = Bridge(config, bus, api_token)
    api = ApiServer(bridge, bus)

    # The API and the panel come up before the homeserver login is attempted,
    # so the operator can watch — and fix — a login that does not succeed.
    await api.start()

    # Publishing on every start, not only when the token is new: it is how the
    # integration discovers the add-on, and a previous attempt may have run
    # before Supervisor was ready to answer.
    if not await supervisor.publish_api_token(api_token):
        _LOGGER.warning(
            "Could not publish the API token to the add-on configuration. If "
            "the integration cannot find the add-on, enter this token "
            "manually: %s",
            api_token,
        )

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on non-POSIX platforms, which only matters for local dev.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    try:
        await bridge.start()
        await stopping.wait()
    finally:
        _LOGGER.info("Shutting down")
        await bridge.stop()
        await api.stop()

    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(0)


if __name__ == "__main__":
    main()
