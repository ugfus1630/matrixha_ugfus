"""Talking back to Supervisor.

The add-on writes its generated API token into its *own* options via
``POST /addons/self/options``, because Supervisor then serves it in
``/addons/{slug}/info`` — which is how the Home Assistant integration
configures itself without the operator typing anything.

That token is the only thing the add-on's Supervisor options hold. Every
setting is edited in the panel and stored in ``/data/settings.json``; posting
just the token also clears whatever a 0.2.x configuration left behind, once
``settings.load`` has carried it over.

Everything here is best-effort: running outside Supervisor (local development)
simply skips it, falling back to the token file.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
TIMEOUT = aiohttp.ClientTimeout(total=15)


def supervisor_token() -> str | None:
    return os.environ.get("SUPERVISOR_TOKEN")


async def fetch_own_options() -> dict[str, Any] | None:
    """Read the options Supervisor holds for this add-on.

    Used once, to import a 0.2.x configuration into the panel: Supervisor keeps
    the operator's saved options even after a schema no longer declares them,
    while ``/data/options.json`` may already have been filtered down to what the
    new schema allows.
    """
    auth = supervisor_token()
    if not auth:
        return None

    try:
        async with (
            aiohttp.ClientSession(timeout=TIMEOUT) as session,
            session.get(
                f"{SUPERVISOR_URL}/addons/self/info",
                headers={"Authorization": f"Bearer {auth}"},
            ) as response,
        ):
            if response.status != 200:
                _LOGGER.debug("Supervisor info returned HTTP %s", response.status)
                return None
            body = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Could not read the add-on's own info: %s", err)
        return None

    options = (body.get("data") or {}).get("options")
    return options if isinstance(options, dict) else None


async def publish_api_token(token: str) -> bool:
    """Write the API token into the add-on's own options.

    Returns True when Supervisor accepted the change, or when there is no
    Supervisor to tell — the token file remains the source of truth either way.
    """
    auth = supervisor_token()
    if not auth:
        _LOGGER.debug("No SUPERVISOR_TOKEN; skipping API token publication")
        return True

    try:
        async with (
            aiohttp.ClientSession(timeout=TIMEOUT) as session,
            session.post(
                f"{SUPERVISOR_URL}/addons/self/options",
                json={"options": {"api_token": token}},
                headers={"Authorization": f"Bearer {auth}"},
            ) as response,
        ):
            body = await response.text()
            if response.status != 200:
                _LOGGER.warning(
                    "Supervisor rejected the token publication (HTTP %s): %s",
                    response.status,
                    body[:200],
                )
                return False
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not reach Supervisor to publish the token: %s", err)
        return False

    _LOGGER.debug("Published the API token to the add-on's own options")
    return True
