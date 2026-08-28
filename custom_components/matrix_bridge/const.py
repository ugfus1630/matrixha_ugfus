"""Constants for the Matrix Bridge integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "matrix_bridge"

CONF_TOKEN: Final = "token"

# Set when the entry configures itself from Supervisor rather than from values
# typed by the operator. Connection details are then re-read on every setup, so
# changing them in the add-on's panel is picked up on reload.
CONF_USE_SUPERVISOR: Final = "use_supervisor"
CONF_ADDON_SLUG: Final = "addon_slug"

ADDON_SLUG: Final = "matrix_bridge"

# Add-ons are reachable on the Supervisor network as {repo}_{slug} with
# underscores replaced by hyphens. For a locally installed add-on that is
# `local-matrix-bridge`.
DEFAULT_HOST: Final = "local-matrix-bridge"
DEFAULT_PORT: Final = 8099

# Behaviour flags. These are configured in the add-on's panel and reported by
# /api/status; the defaults only apply until the first status is received.
CONF_FIRE_EVENTS: Final = "fire_events"
CONF_CREATE_NOTIFY_ENTITIES: Final = "create_notify_entities"
DEFAULT_FIRE_EVENTS: Final = True
DEFAULT_CREATE_NOTIFY_ENTITIES: Final = True

# Home Assistant event bus event names
EVENT_MESSAGE: Final = f"{DOMAIN}_message"
EVENT_IMAGE: Final = f"{DOMAIN}_image"
EVENT_FILE: Final = f"{DOMAIN}_file"
EVENT_REACTION: Final = f"{DOMAIN}_reaction"
EVENT_VERIFICATION: Final = f"{DOMAIN}_verification"
EVENT_UNDECRYPTABLE: Final = f"{DOMAIN}_undecryptable"

# Bridge event types (must match matrix_bridge/app/events.py)
BRIDGE_STATUS: Final = "status"
BRIDGE_ROOMS_CHANGED: Final = "rooms_changed"
BRIDGE_MESSAGE: Final = "message"
BRIDGE_IMAGE: Final = "image"
BRIDGE_FILE: Final = "file"
BRIDGE_REACTION: Final = "reaction"
BRIDGE_VERIFICATION: Final = "verification"
BRIDGE_UNDECRYPTABLE: Final = "undecryptable"
BRIDGE_ERROR: Final = "error"

EVENT_MAP: Final = {
    BRIDGE_MESSAGE: EVENT_MESSAGE,
    BRIDGE_IMAGE: EVENT_IMAGE,
    BRIDGE_FILE: EVENT_FILE,
    BRIDGE_REACTION: EVENT_REACTION,
    BRIDGE_VERIFICATION: EVENT_VERIFICATION,
    BRIDGE_UNDECRYPTABLE: EVENT_UNDECRYPTABLE,
}

# Dispatcher signals
SIGNAL_ROOMS_UPDATED: Final = f"{DOMAIN}_rooms_updated"
SIGNAL_STATE_UPDATED: Final = f"{DOMAIN}_state_updated"

# Services
SERVICE_SEND_MESSAGE: Final = "send_message"
SERVICE_SEND_IMAGE: Final = "send_image"
SERVICE_SEND_FILE: Final = "send_file"
SERVICE_REACT: Final = "react"
SERVICE_JOIN_ROOM: Final = "join_room"
SERVICE_LEAVE_ROOM: Final = "leave_room"
SERVICE_START_VERIFICATION: Final = "start_verification"
SERVICE_CONFIRM_VERIFICATION: Final = "confirm_verification"
SERVICE_CANCEL_VERIFICATION: Final = "cancel_verification"
SERVICE_SET_DEVICE_TRUST: Final = "set_device_trust"

ATTR_ROOM: Final = "room"
ATTR_MESSAGE: Final = "message"
ATTR_HTML: Final = "html"
ATTR_REPLY_TO: Final = "reply_to"
ATTR_THREAD_ROOT: Final = "thread_root"
ATTR_PATH: Final = "path"
ATTR_URL: Final = "url"
ATTR_CAPTION: Final = "caption"
ATTR_EVENT_ID: Final = "event_id"
ATTR_KEY: Final = "key"
ATTR_USER_ID: Final = "user_id"
ATTR_DEVICE_ID: Final = "device_id"
ATTR_TRANSACTION_ID: Final = "transaction_id"
ATTR_STATE: Final = "state"
