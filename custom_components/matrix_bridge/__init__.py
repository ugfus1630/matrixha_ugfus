"""The Matrix Bridge integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .addon import AddonNotFound, async_discover_addon
from .api_client import BridgeAuthError, BridgeClient, BridgeError, RoomUntrustedError
from .const import (
    ATTR_CAPTION,
    ATTR_DEVICE_ID,
    ATTR_EVENT_ID,
    ATTR_HTML,
    ATTR_KEY,
    ATTR_MESSAGE,
    ATTR_PATH,
    ATTR_REPLY_TO,
    ATTR_ROOM,
    ATTR_STATE,
    ATTR_THREAD_ROOT,
    ATTR_TRANSACTION_ID,
    ATTR_URL,
    ATTR_USER_ID,
    CONF_TOKEN,
    CONF_USE_SUPERVISOR,
    DOMAIN,
    SERVICE_CANCEL_VERIFICATION,
    SERVICE_CONFIRM_VERIFICATION,
    SERVICE_JOIN_ROOM,
    SERVICE_LEAVE_ROOM,
    SERVICE_REACT,
    SERVICE_SEND_FILE,
    SERVICE_SEND_IMAGE,
    SERVICE_SEND_MESSAGE,
    SERVICE_SET_DEVICE_TRUST,
    SERVICE_START_VERIFICATION,
)
from .coordinator import MatrixBridgeCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NOTIFY,
    Platform.SENSOR,
]

# A plain alias rather than a PEP 695 `type` statement: the GitLab runner is a
# shell executor, so the pipeline's python:3.13 image is ignored and the host's
# own python3 compiles this. That interpreter predates 3.12, where `type` was
# added, and this was the one line in the project that needed it.
MatrixBridgeConfigEntry = ConfigEntry[MatrixBridgeCoordinator]

_ROOM_SCHEMA = {vol.Required(ATTR_ROOM): cv.string}

SERVICE_SCHEMAS: dict[str, vol.Schema] = {
    SERVICE_SEND_MESSAGE: vol.Schema(
        {
            **_ROOM_SCHEMA,
            vol.Required(ATTR_MESSAGE): cv.string,
            vol.Optional(ATTR_HTML): cv.string,
            vol.Optional(ATTR_REPLY_TO): cv.string,
            vol.Optional(ATTR_THREAD_ROOT): cv.string,
        }
    ),
    SERVICE_SEND_IMAGE: vol.Schema(
        {
            **_ROOM_SCHEMA,
            vol.Exclusive(ATTR_PATH, "source"): cv.string,
            vol.Exclusive(ATTR_URL, "source"): cv.string,
            vol.Optional(ATTR_CAPTION): cv.string,
        }
    ),
    SERVICE_SEND_FILE: vol.Schema(
        {
            **_ROOM_SCHEMA,
            vol.Exclusive(ATTR_PATH, "source"): cv.string,
            vol.Exclusive(ATTR_URL, "source"): cv.string,
            vol.Optional(ATTR_CAPTION): cv.string,
        }
    ),
    SERVICE_REACT: vol.Schema(
        {
            **_ROOM_SCHEMA,
            vol.Required(ATTR_EVENT_ID): cv.string,
            vol.Required(ATTR_KEY): cv.string,
        }
    ),
    SERVICE_JOIN_ROOM: vol.Schema({vol.Required(ATTR_ROOM): cv.string}),
    SERVICE_LEAVE_ROOM: vol.Schema(_ROOM_SCHEMA),
    SERVICE_START_VERIFICATION: vol.Schema(
        {
            vol.Required(ATTR_USER_ID): cv.string,
            vol.Optional(ATTR_DEVICE_ID): cv.string,
        }
    ),
    SERVICE_CONFIRM_VERIFICATION: vol.Schema(
        {vol.Required(ATTR_TRANSACTION_ID): cv.string}
    ),
    SERVICE_CANCEL_VERIFICATION: vol.Schema(
        {vol.Required(ATTR_TRANSACTION_ID): cv.string}
    ),
    SERVICE_SET_DEVICE_TRUST: vol.Schema(
        {
            vol.Required(ATTR_USER_ID): cv.string,
            vol.Required(ATTR_DEVICE_ID): cv.string,
            vol.Required(ATTR_STATE): vol.In(["verify", "unverify", "blacklist"]),
        }
    ),
}


async def _async_connection_details(
    hass: HomeAssistant, entry: MatrixBridgeConfigEntry
) -> tuple[str, int, str]:
    """Resolve host/port/token, preferring the add-on's live configuration.

    Re-reading from Supervisor on every setup means changing the port or token
    in the add-on's panel is picked up by reloading the entry, with nothing to
    re-enter on the Home Assistant side.
    """
    if entry.data.get(CONF_USE_SUPERVISOR):
        try:
            addon = await async_discover_addon(hass)
        except AddonNotFound as err:
            _LOGGER.debug("Supervisor lookup failed, using stored values: %s", err)
        else:
            if addon.token:
                if (addon.host, addon.port, addon.token) != (
                    entry.data.get(CONF_HOST),
                    entry.data.get(CONF_PORT),
                    entry.data.get(CONF_TOKEN),
                ):
                    hass.config_entries.async_update_entry(
                        entry,
                        data={
                            **entry.data,
                            CONF_HOST: addon.host,
                            CONF_PORT: addon.port,
                            CONF_TOKEN: addon.token,
                        },
                    )
                return addon.host, addon.port, addon.token

    return entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_TOKEN]


async def async_setup_entry(
    hass: HomeAssistant, entry: MatrixBridgeConfigEntry
) -> bool:
    """Set up a Matrix Bridge add-on connection."""
    host, port, token = await _async_connection_details(hass, entry)
    client = BridgeClient(async_get_clientsession(hass), host, port, token)
    coordinator = MatrixBridgeCoordinator(hass, entry, client)

    try:
        await coordinator.async_start()
    except BridgeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except BridgeError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MatrixBridgeConfigEntry
) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unloaded and not hass.config_entries.async_loaded_entries(DOMAIN):
        for service in SERVICE_SCHEMAS:
            hass.services.async_remove(DOMAIN, service)

    return unloaded


async def _async_reload_entry(
    hass: HomeAssistant, entry: MatrixBridgeConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services once, shared by all entries."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE):
        return

    def _coordinator() -> MatrixBridgeCoordinator:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise BridgeError("No Matrix Bridge entry is loaded")
        return entries[0].runtime_data

    async def _call(action, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run an add-on call, translating failures into HA-visible errors."""
        try:
            return await action(*args, **kwargs)
        except RoomUntrustedError as err:
            devices = ", ".join(
                f"{d['user_id']}/{d['device_id']}" for d in err.unverified
            )
            raise HomeAssistantError(
                f"{err} Unverified devices: {devices or 'unknown'}"
            ) from err
        except BridgeError as err:
            raise HomeAssistantError(str(err)) from err

    async def send_message(call: ServiceCall) -> None:
        coordinator = _coordinator()
        await _call(
            coordinator.client.send_message,
            call.data[ATTR_ROOM],
            body=call.data[ATTR_MESSAGE],
            html=call.data.get(ATTR_HTML),
            reply_to=call.data.get(ATTR_REPLY_TO),
            thread_root=call.data.get(ATTR_THREAD_ROOT),
        )

    async def send_media(call: ServiceCall) -> None:
        coordinator = _coordinator()
        if not call.data.get(ATTR_PATH) and not call.data.get(ATTR_URL):
            raise HomeAssistantError("Provide either 'path' or 'url'")
        await _call(
            coordinator.client.send_media,
            call.data[ATTR_ROOM],
            path=call.data.get(ATTR_PATH),
            url=call.data.get(ATTR_URL),
            caption=call.data.get(ATTR_CAPTION),
        )

    async def react(call: ServiceCall) -> None:
        coordinator = _coordinator()
        await _call(
            coordinator.client.react,
            call.data[ATTR_ROOM],
            call.data[ATTR_EVENT_ID],
            call.data[ATTR_KEY],
        )

    async def join_room(call: ServiceCall) -> None:
        coordinator = _coordinator()
        await _call(coordinator.client.join_room, call.data[ATTR_ROOM])

    async def leave_room(call: ServiceCall) -> None:
        coordinator = _coordinator()
        await _call(coordinator.client.leave_room, call.data[ATTR_ROOM])

    async def start_verification(call: ServiceCall) -> None:
        coordinator = _coordinator()
        await _call(
            coordinator.client.start_verification,
            call.data[ATTR_USER_ID],
            call.data.get(ATTR_DEVICE_ID),
        )

    async def confirm_verification(call: ServiceCall) -> None:
        coordinator = _coordinator()
        await _call(
            coordinator.client.verification_action,
            call.data[ATTR_TRANSACTION_ID],
            "confirm",
        )

    async def cancel_verification(call: ServiceCall) -> None:
        coordinator = _coordinator()
        await _call(
            coordinator.client.verification_action,
            call.data[ATTR_TRANSACTION_ID],
            "cancel",
            reject=True,
        )

    async def set_device_trust(call: ServiceCall) -> None:
        coordinator = _coordinator()
        await _call(
            coordinator.client.set_device_trust,
            call.data[ATTR_USER_ID],
            call.data[ATTR_DEVICE_ID],
            call.data[ATTR_STATE],
        )

    handlers = {
        SERVICE_SEND_MESSAGE: send_message,
        SERVICE_SEND_IMAGE: send_media,
        SERVICE_SEND_FILE: send_media,
        SERVICE_REACT: react,
        SERVICE_JOIN_ROOM: join_room,
        SERVICE_LEAVE_ROOM: leave_room,
        SERVICE_START_VERIFICATION: start_verification,
        SERVICE_CONFIRM_VERIFICATION: confirm_verification,
        SERVICE_CANCEL_VERIFICATION: cancel_verification,
        SERVICE_SET_DEVICE_TRUST: set_device_trust,
    }

    for name, handler in handlers.items():
        hass.services.async_register(
            DOMAIN, name, handler, schema=SERVICE_SCHEMAS[name]
        )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (config entries only)."""
    return True
