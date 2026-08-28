"""Config flow for the Matrix Bridge integration.

When Home Assistant runs supervised, this asks for nothing: the add-on is
located through Supervisor and its host, port and API token are read from the
add-on's own options. All configuration therefore stays in the add-on's panel.

The manual form is only a fallback, for Home Assistant Core installs or an
add-on running on a different machine.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .addon import AddonConnection, AddonNotFound, async_discover_addon
from .api_client import BridgeAuthError, BridgeClient, BridgeError
from .const import (
    CONF_ADDON_SLUG,
    CONF_TOKEN,
    CONF_USE_SUPERVISOR,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class MatrixBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a connection to the Matrix Bridge add-on."""

    VERSION = 1

    def __init__(self) -> None:
        self._addon: AddonConnection | None = None

    async def _validate(self, host: str, port: int, token: str) -> dict[str, Any]:
        client = BridgeClient(async_get_clientsession(self.hass), host, port, token)
        return await client.status()

    async def _create(self, status: dict[str, Any], data: dict[str, Any]):
        user_id = status.get("user_id") or data.get(CONF_HOST, DOMAIN)
        await self.async_set_unique_id(user_id)
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title=user_id, data=data)

    # ------------------------------------------------------------------- user

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prefer zero-input setup via Supervisor; fall back to a manual form."""
        try:
            self._addon = await async_discover_addon(self.hass)
        except AddonNotFound as err:
            _LOGGER.debug("Falling back to manual setup: %s", err)
            return await self.async_step_manual()

        return await self.async_step_addon_confirm()

    async def async_step_addon_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm using the discovered add-on. No fields to fill in."""
        assert self._addon is not None
        addon = self._addon
        errors: dict[str, str] = {}

        if not addon.running:
            return self.async_abort(reason="addon_not_running")

        if not addon.token:
            # The add-on publishes a generated token into its own options on
            # start, but that can fail or be undone — saving the add-on's
            # Configuration tab with the optional `api_token` field switched off
            # clears it, and Supervisor then serves nothing until the next
            # start. Rather than dead-ending, fall through to the manual form
            # with the discovered address filled in: the token is readable in
            # the Matrix panel, so the operator can always finish by hand.
            _LOGGER.warning(
                "Add-on %s is running but Supervisor is serving no api_token; "
                "falling back to manual entry",
                addon.slug,
            )
            return await self.async_step_manual()

        if user_input is not None:
            try:
                status = await self._validate(addon.host, addon.port, addon.token)
            except BridgeAuthError:
                # The token Supervisor is serving is not the one the add-on
                # accepts — stale, or typed in by hand and mistyped. Re-showing
                # this form cannot fix that, since it has no fields.
                _LOGGER.warning(
                    "Add-on %s rejected the token Supervisor is serving; "
                    "falling back to manual entry",
                    addon.slug,
                )
                return await self.async_step_manual()
            except BridgeError:
                errors["base"] = "cannot_connect"
            else:
                return await self._create(
                    status,
                    {
                        CONF_USE_SUPERVISOR: True,
                        CONF_ADDON_SLUG: addon.slug,
                        CONF_HOST: addon.host,
                        CONF_PORT: addon.port,
                        CONF_TOKEN: addon.token,
                    },
                )

        return self.async_show_form(
            step_id="addon_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "addon": addon.slug,
                "host": f"{addon.host}:{addon.port}",
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual entry, for unsupervised installs or a remote add-on."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                status = await self._validate(
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_TOKEN],
                )
            except BridgeAuthError:
                errors["base"] = "invalid_auth"
            except BridgeError:
                errors["base"] = "cannot_connect"
            else:
                return await self._create(
                    status, {CONF_USE_SUPERVISOR: False, **user_input}
                )

        # When discovery worked but the token did not, the host and port are
        # already known and only the token is genuinely missing.
        addon = self._addon
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOST, default=addon.host if addon else DEFAULT_HOST
                ): str,
                vol.Required(
                    CONF_PORT, default=addon.port if addon else DEFAULT_PORT
                ): int,
                vol.Required(CONF_TOKEN): str,
            }
        )
        return self.async_show_form(
            step_id="manual", data_schema=schema, errors=errors
        )

    # ----------------------------------------------------------------- reauth

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-read the token from Supervisor, or ask for it when unsupervised."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if entry.data.get(CONF_USE_SUPERVISOR):
            # Nothing to ask: the add-on issues the token itself, so just re-read
            # it. A changed token heals on reload without operator input.
            try:
                addon = await async_discover_addon(self.hass)
                await self._validate(addon.host, addon.port, addon.token)
            except (AddonNotFound, BridgeError):
                return self.async_abort(reason="reauth_failed")

            return self.async_update_reload_and_abort(
                entry,
                data={
                    **entry.data,
                    CONF_HOST: addon.host,
                    CONF_PORT: addon.port,
                    CONF_TOKEN: addon.token,
                },
            )

        if user_input is not None:
            data = {**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
            try:
                await self._validate(
                    data[CONF_HOST], data[CONF_PORT], data[CONF_TOKEN]
                )
            except BridgeAuthError:
                errors["base"] = "invalid_auth"
            except BridgeError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )
