"""Discovering the Matrix Bridge add-on through Supervisor.

Supervisor serves an add-on's own options at ``/addons/{slug}/info``, so when
Home Assistant runs supervised the integration can read the add-on's hostname,
port and API token itself. That is what keeps every setting in the add-on's
panel: there is nothing left for the operator to re-type on this side.

Supervisor's `discovery:` mechanism is not usable here — its service list is a
closed enum that custom integrations cannot join — so this reads the add-on
info directly instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio

from .const import ADDON_SLUG, DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)


class AddonNotFound(Exception):
    """The Matrix Bridge add-on is not installed, or Supervisor is unavailable."""


@dataclass(slots=True)
class AddonConnection:
    """Everything needed to reach the add-on, sourced from its own options."""

    slug: str
    host: str
    port: int
    token: str
    state: str

    @property
    def running(self) -> bool:
        return self.state == "started"


def _extract(info) -> AddonConnection:
    options = dict(getattr(info, "options", None) or {})
    # The add-on publishes its generated token as `api_token`; 0.2.x kept it in
    # an `api` section, which an add-on that has not restarted yet still has.
    legacy = dict(options.get("api") or {})

    host = getattr(info, "hostname", None) or info.slug.replace("_", "-")
    port = getattr(info, "ingress_port", None) or legacy.get("port") or DEFAULT_PORT
    token = str(options.get("api_token") or legacy.get("token") or "")

    return AddonConnection(
        slug=info.slug,
        host=host,
        port=int(port),
        token=token,
        state=str(getattr(info, "state", "") or ""),
    )


async def async_discover_addon(hass: HomeAssistant) -> AddonConnection:
    """Locate the add-on and read its connection details.

    Raises AddonNotFound when unsupervised, when the add-on is not installed,
    or when Supervisor cannot be reached.
    """
    if not is_hassio(hass):
        raise AddonNotFound("Home Assistant is not running under Supervisor")

    # Imported lazily so the integration still loads on non-supervised installs.
    from homeassistant.components.hassio import (  # noqa: PLC0415
        SupervisorError,
        get_supervisor_client,
    )

    client = get_supervisor_client(hass)

    try:
        installed = await client.addons.list()
    except SupervisorError as err:
        raise AddonNotFound(f"Could not list add-ons: {err}") from err

    # A locally installed add-on is `local_matrix_bridge`; one installed from a
    # repository is prefixed with that repository's hash instead.
    match = next(
        (
            addon
            for addon in installed
            if addon.slug == ADDON_SLUG or addon.slug.endswith(f"_{ADDON_SLUG}")
        ),
        None,
    )
    if match is None:
        raise AddonNotFound("The Matrix Bridge add-on is not installed")

    try:
        info = await client.addons.addon_info(match.slug)
    except SupervisorError as err:
        raise AddonNotFound(f"Could not read add-on info: {err}") from err

    connection = _extract(info)
    _LOGGER.debug(
        "Discovered add-on %s at %s:%s (state=%s, token=%s)",
        connection.slug,
        connection.host,
        connection.port,
        connection.state,
        "set" if connection.token else "empty",
    )
    return connection
