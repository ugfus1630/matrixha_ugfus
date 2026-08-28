"""Every setting the operator can change, and where it lives.

Settings are edited in the add-on's own panel — the **Matrix** item in the Home
Assistant sidebar — and stored in ``/data/settings.json``, which this module
owns. The Supervisor *Configuration* tab deliberately holds nothing: a value
there cannot be checked against the homeserver, cannot be changed without
restarting the container, and would split configuration across two screens.

The one value that still travels through Supervisor is the generated API token,
which the add-on publishes into its own options so the Home Assistant
integration can discover it without anyone typing anything. It is a credential
the add-on issues, not a setting anybody has to fill in.

``FIELDS`` is the single description of every setting: the panel builds its form
from it, :func:`validate` enforces it, and ``dev/validate.py`` checks it against
the dataclasses below, so the form cannot drift from the code.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("MATRIX_BRIDGE_DATA", "/data"))
SETTINGS_PATH = Path(
    os.environ.get("MATRIX_BRIDGE_SETTINGS", DATA_DIR / "settings.json")
)

# Supervisor owns this file. The add-on reads it once to carry a 0.2.x
# configuration over into the panel, and writes the generated API token back
# into it through the Supervisor API (see supervisor.py).
OPTIONS_PATH = Path(os.environ.get("MATRIX_BRIDGE_OPTIONS", DATA_DIR / "options.json"))

TOKEN_PATH = DATA_DIR / "api_token"

# Fixed rather than configurable: it has to match `ingress_port` in config.yaml,
# and a mismatch would silently break the panel this whole module points at.
API_PORT = 8099

USER_ID_RE = re.compile(r"^@[^:]+:.+$")

LOG_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# Attributes of Config that are runtime state rather than stored settings.
INTERNAL_FIELDS = {"load_error"}


class SettingsError(Exception):
    """Raised when submitted settings are unusable.

    The message is shown in the panel, so it names the field and the fix.
    """


# --------------------------------------------------------------------- shape


@dataclass(slots=True)
class AutojoinConfig:
    enabled: bool = True
    allowed_users: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    max_rooms: int = 50
    accept_dm_invites: bool = True
    leave_if_alone: bool = False


@dataclass(slots=True)
class E2EEConfig:
    enabled: bool = True
    send_to_unverified_rooms: bool = True
    ignore_unverified_devices: bool = True
    blacklist_unverified: bool = False
    auto_trust_own_devices: bool = True
    auto_accept_verification: bool = False
    require_verified_sender: bool = False
    verification_timeout: int = 300


@dataclass(slots=True)
class MediaConfig:
    download_incoming: bool = True
    download_path: str = "/media/matrix"
    max_download_mb: int = 25
    max_upload_mb: int = 50
    # `/homeassistant` is where Supervisor mounts Home Assistant's own
    # configuration directory for us (the `homeassistant_config` entry in
    # config.yaml). Home Assistant calls that same directory `/config`
    # everywhere the operator sees it, so `media.py` accepts either spelling —
    # but the default names the one that exists in this container.
    allowed_upload_paths: list[str] = field(
        default_factory=lambda: ["/media", "/share", "/homeassistant/www"]
    )
    retention_days: int = 30

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@dataclass(slots=True)
class HomeAssistantConfig:
    """How the bridge presents itself inside Home Assistant.

    These live here rather than in the integration's own options flow so that
    every setting the operator can change is in one place: this panel.
    """

    create_notify_entities: bool = True
    fire_events: bool = True


@dataclass(slots=True)
class AdvancedConfig:
    log_level: str = "info"
    ignore_own_messages: bool = True
    sync_timeout: int = 30000
    message_history_limit: int = 50


@dataclass(slots=True)
class Config:
    """The complete settings document, all of it optional until first saved."""

    homeserver: str = ""
    user_id: str = ""
    password: str = ""
    access_token: str = ""
    device_name: str = "Home Assistant"
    presence: str = "online"
    autojoin: AutojoinConfig = field(default_factory=AutojoinConfig)
    e2ee: E2EEConfig = field(default_factory=E2EEConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    homeassistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    advanced: AdvancedConfig = field(default_factory=AdvancedConfig)

    # Runtime state: why the stored settings could not be read, if they could
    # not be. Surfaced in the panel instead of stopping the add-on.
    load_error: str = ""

    @property
    def is_configured(self) -> bool:
        """True once there is enough to attempt a login."""
        return bool(self.homeserver and self.user_id)

    @property
    def store_path(self) -> Path:
        return DATA_DIR / "store"

    @property
    def session_path(self) -> Path:
        return DATA_DIR / "session.json"


# -------------------------------------------------------------------- fields


SECTIONS: list[dict[str, str]] = [
    {
        "key": "connection",
        "title": "Connection",
        "help": (
            "The Matrix account the bridge logs in as. Give the bot its own "
            "account — do not reuse your own."
        ),
    },
    {
        "key": "autojoin",
        "title": "Auto-join",
        "help": "Which invites the bot accepts. Empty allowlists mean anyone.",
    },
    {
        "key": "e2ee",
        "title": "Encryption",
        "help": "End-to-end encryption and the trust policy for sending.",
    },
    {
        "key": "media",
        "title": "Media",
        "help": "Where attachments are written, and which paths may be sent.",
    },
    {
        "key": "homeassistant",
        "title": "Home Assistant",
        "help": "How the bridge presents itself to Home Assistant.",
    },
    {"key": "advanced", "title": "Advanced", "help": ""},
]


@dataclass(frozen=True, slots=True)
class Field:
    """One editable setting: how to show it, and what counts as valid."""

    path: str
    section: str
    label: str
    kind: str  # text | password | select | bool | int | list
    help: str = ""
    placeholder: str = ""
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    absolute_paths: bool = False

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "section": self.section,
            "label": self.label,
            "kind": self.kind,
        }
        if self.help:
            data["help"] = self.help
        if self.placeholder:
            data["placeholder"] = self.placeholder
        if self.choices:
            data["choices"] = list(self.choices)
        if self.minimum is not None:
            data["minimum"] = self.minimum
        if self.maximum is not None:
            data["maximum"] = self.maximum
        return data


FIELDS: list[Field] = [
    # ------------------------------------------------------------ connection
    Field(
        "homeserver",
        "connection",
        "Homeserver",
        "text",
        help="Base URL of your homeserver. https:// is assumed if you leave it off.",
        placeholder="https://matrix.example.org",
    ),
    Field(
        "user_id",
        "connection",
        "Matrix ID",
        "text",
        help="The bot's full Matrix ID.",
        placeholder="@homeassistant:example.org",
    ),
    Field(
        "password",
        "connection",
        "Password",
        "password",
        help=(
            "Used for the first login only. It can be cleared afterwards — the "
            "stored session keeps working."
        ),
    ),
    Field(
        "access_token",
        "connection",
        "Access token",
        "password",
        help="Use instead of a password, if you already have one.",
    ),
    Field(
        "device_name",
        "connection",
        "Device name",
        "text",
        help="Shown to other users in device lists.",
    ),
    Field(
        "presence",
        "connection",
        "Presence",
        "select",
        choices=("online", "unavailable", "offline"),
    ),
    # -------------------------------------------------------------- autojoin
    Field("autojoin.enabled", "autojoin", "Join rooms on invite", "bool"),
    Field(
        "autojoin.allowed_users",
        "autojoin",
        "Only accept invites from",
        "list",
        help="Matrix IDs, one per line. Empty means anyone can invite the bot.",
        placeholder="@you:example.org",
    ),
    Field(
        "autojoin.allowed_domains",
        "autojoin",
        "Only accept invites from domains",
        "list",
        help="Homeserver domains, one per line. Empty means any.",
        placeholder="example.org",
    ),
    Field(
        "autojoin.max_rooms",
        "autojoin",
        "Maximum rooms",
        "int",
        minimum=1,
        maximum=1000,
        help="Refuse new invites beyond this many rooms.",
    ),
    Field("autojoin.accept_dm_invites", "autojoin", "Accept direct messages", "bool"),
    Field(
        "autojoin.leave_if_alone",
        "autojoin",
        "Leave empty rooms",
        "bool",
        help="Leave a room once the bot is the only member left.",
    ),
    # ------------------------------------------------------------------ e2ee
    Field("e2ee.enabled", "e2ee", "End-to-end encryption", "bool"),
    Field(
        "e2ee.send_to_unverified_rooms",
        "e2ee",
        "Send to rooms with unverified devices",
        "bool",
        help="Turn off to refuse sending until every device in the room is verified.",
    ),
    Field(
        "e2ee.ignore_unverified_devices",
        "e2ee",
        "Encrypt for unverified devices",
        "bool",
        help="Off means unverified devices cannot read what the bot sends.",
    ),
    Field(
        "e2ee.blacklist_unverified",
        "e2ee",
        "Blacklist unverified devices",
        "bool",
    ),
    Field(
        "e2ee.auto_trust_own_devices", "e2ee", "Trust your own other devices", "bool"
    ),
    Field(
        "e2ee.auto_accept_verification",
        "e2ee",
        "Auto-accept verification requests",
        "bool",
        help="The emoji still have to be confirmed here or in your client.",
    ),
    Field(
        "e2ee.require_verified_sender",
        "e2ee",
        "Only act on messages from verified devices",
        "bool",
    ),
    Field(
        "e2ee.verification_timeout",
        "e2ee",
        "Verification timeout (seconds)",
        "int",
        minimum=30,
        maximum=3600,
    ),
    # ----------------------------------------------------------------- media
    Field("media.download_incoming", "media", "Download incoming attachments", "bool"),
    Field(
        "media.download_path",
        "media",
        "Download folder",
        "text",
        absolute_paths=True,
        placeholder="/media/matrix",
    ),
    Field(
        "media.max_download_mb", "media", "Maximum download (MB)", "int",
        minimum=1, maximum=500,
    ),
    Field(
        "media.max_upload_mb", "media", "Maximum upload (MB)", "int",
        minimum=1, maximum=500,
    ),
    Field(
        "media.allowed_upload_paths",
        "media",
        "Paths the bot may send from",
        "list",
        absolute_paths=True,
        help="One per line. Files outside these folders are refused.",
        placeholder="/media",
    ),
    Field(
        "media.retention_days",
        "media",
        "Delete downloads after (days)",
        "int",
        minimum=0,
        maximum=3650,
        help="0 keeps them forever.",
    ),
    # --------------------------------------------------------- homeassistant
    Field(
        "homeassistant.create_notify_entities",
        "homeassistant",
        "Create notify entities per room",
        "bool",
    ),
    Field(
        "homeassistant.fire_events",
        "homeassistant",
        "Fire matrix_bridge events",
        "bool",
        help="Needed by automations that trigger on incoming messages.",
    ),
    # --------------------------------------------------------------- advanced
    Field(
        "advanced.log_level",
        "advanced",
        "Log level",
        "select",
        choices=("trace", "debug", "info", "warning", "error"),
    ),
    Field(
        "advanced.ignore_own_messages",
        "advanced",
        "Ignore the bot's own messages",
        "bool",
    ),
    Field(
        "advanced.sync_timeout",
        "advanced",
        "Sync timeout (ms)",
        "int",
        minimum=5000,
        maximum=120000,
    ),
    Field(
        "advanced.message_history_limit",
        "advanced",
        "Messages fetched per sync",
        "int",
        minimum=0,
        maximum=1000,
    ),
]

FIELDS_BY_PATH = {f.path: f for f in FIELDS}

SECRET_KINDS = {"password"}


def describe() -> dict[str, Any]:
    """The form definition the panel renders."""
    return {
        "sections": SECTIONS,
        "fields": [f.as_dict() for f in FIELDS],
    }


# ------------------------------------------------------------------- access


def _get(config: Config, path: str) -> Any:
    target: Any = config
    for part in path.split("."):
        target = getattr(target, part)
    return target


def _set(config: Config, path: str, value: Any) -> None:
    parts = path.split(".")
    target: Any = config
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


def to_dict(config: Config) -> dict[str, Any]:
    """The stored settings as JSON, nested exactly as they are on disk."""
    data: dict[str, Any] = {}
    for f in FIELDS:
        parts = f.path.split(".")
        target = data
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        value = _get(config, f.path)
        target[parts[-1]] = list(value) if isinstance(value, list) else value
    return data


# --------------------------------------------------------------- validation


def _as_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false", "on", "off"):
        return value.lower() in ("true", "on")
    raise SettingsError(f"{label} must be true or false")


def _as_int(value: Any, f: Field) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as err:
        raise SettingsError(f"{f.label} must be a whole number") from err
    if f.minimum is not None and number < f.minimum:
        raise SettingsError(f"{f.label} must be at least {f.minimum}")
    if f.maximum is not None and number > f.maximum:
        raise SettingsError(f"{f.label} must be at most {f.maximum}")
    return number


def _as_list(value: Any, f: Field) -> list[str]:
    if isinstance(value, str):
        # The panel sends a textarea, one entry per line.
        items = [line.strip() for line in value.replace(",", "\n").splitlines()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        raise SettingsError(f"{f.label} must be a list")
    items = [item for item in items if item]
    if f.absolute_paths:
        for item in items:
            if not item.startswith("/"):
                raise SettingsError(f"{f.label}: {item} must be an absolute path")
    return items


def _as_text(value: Any, f: Field) -> str:
    text = str(value or "").strip()
    if f.absolute_paths and text and not text.startswith("/"):
        raise SettingsError(f"{f.label} must be an absolute path, e.g. /media/matrix")
    return text


def _coerce(f: Field, value: Any) -> Any:
    if f.kind == "bool":
        return _as_bool(value, f.label)
    if f.kind == "int":
        return _as_int(value, f)
    if f.kind == "list":
        return _as_list(value, f)
    if f.kind == "select":
        text = str(value or "").strip().lower()
        if text not in f.choices:
            raise SettingsError(f"{f.label} must be one of {', '.join(f.choices)}")
        return text
    if f.kind == "password":
        # Not stripped: a password may legitimately start or end with a space.
        return str(value or "")
    return _as_text(value, f)


def _flatten(raw: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat |= _flatten(value, f"{path}.")
        else:
            flat[path] = value
    return flat


def validate(raw: dict[str, Any], base: Config | None = None) -> Config:
    """Merge submitted settings onto ``base`` (or the defaults) and check them.

    Keys that are absent keep their current value, so the panel can submit a
    single section, and keys from a future version are ignored rather than
    rejected.
    """
    config = base or Config()
    submitted = _flatten(raw)

    unknown = sorted(set(submitted) - set(FIELDS_BY_PATH))
    if unknown:
        _LOGGER.warning("Ignoring unknown settings: %s", ", ".join(unknown))

    for path, value in submitted.items():
        f = FIELDS_BY_PATH.get(path)
        if f is None:
            continue
        _set(config, path, _coerce(f, value))

    homeserver = config.homeserver.rstrip("/")
    if homeserver and not homeserver.startswith(("http://", "https://")):
        homeserver = f"https://{homeserver}"
    config.homeserver = homeserver

    if config.user_id and not USER_ID_RE.match(config.user_id):
        raise SettingsError(
            "Matrix ID must look like @homeassistant:example.org, "
            f"got {config.user_id!r}"
        )

    config.load_error = ""
    return config


# -------------------------------------------------------------- persistence


def import_legacy(raw: Any, source: str, path: Path | None = None) -> Config | None:
    """Carry a 0.2.x configuration over from the Supervisor options form.

    Returns the imported settings, already saved, or None when there is nothing
    worth importing. Callers try more than one source: Supervisor drops options
    that a new schema no longer declares, and whether it does so before the
    add-on first reads ``/data/options.json`` is not something to rely on.
    """
    if not isinstance(raw, dict) or not raw.get("homeserver"):
        return None

    raw = dict(raw)
    raw.pop("api", None)  # the port is fixed now, and the token has its own file
    raw.pop("api_token", None)

    try:
        config = validate(raw)
    except SettingsError as err:
        _LOGGER.warning("Could not import the previous configuration: %s", err)
        return None

    save(config, path)
    _LOGGER.info(
        "Imported the previous configuration from %s into %s. Settings are "
        "edited in the Matrix panel from now on; the add-on's configuration tab "
        "is no longer used.",
        source,
        SETTINGS_PATH,
    )
    return config


def _legacy_options(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load(path: Path | None = None) -> Config:
    """Read the stored settings, falling back to defaults.

    Never raises: an add-on that refuses to start is an add-on whose settings
    cannot be fixed, because the panel that edits them is the thing that did
    not start.
    """
    path = path or SETTINGS_PATH

    raw: dict[str, Any] | None = None
    error = ""
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            error = f"Could not read {path}: {err}. Using defaults until you save."
            _LOGGER.error("%s", error)
        else:
            if isinstance(loaded, dict):
                raw = loaded
            else:
                error = f"{path} does not contain a settings object; using defaults."
                _LOGGER.error("%s", error)
    else:
        imported = import_legacy(
            _legacy_options(OPTIONS_PATH), str(OPTIONS_PATH), path
        )
        if imported is not None:
            return imported

    if raw is None:
        config = Config()
        config.load_error = error
        return config

    try:
        config = validate(raw)
    except SettingsError as err:
        # Stored settings that no longer validate (a hand-edited file, or a
        # constraint that tightened between versions) must not lock the panel
        # out; report them and start from the defaults.
        config = Config()
        config.load_error = f"Stored settings are invalid: {err}"
        _LOGGER.error("%s", config.load_error)
    return config


def save(config: Config, path: Path | None = None) -> None:
    """Write the settings atomically, readable only by the add-on."""
    path = path or SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_dict(config), indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    _LOGGER.debug("Saved settings to %s", path)


def resolve_api_token() -> tuple[str, bool]:
    """Return (token, freshly_generated), generating one on first start."""
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token, False

    token = secrets.token_urlsafe(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    _LOGGER.info(
        "Generated an API token and saved it to %s. It is published to the "
        "add-on's own options so the Home Assistant integration can find it.",
        TOKEN_PATH,
    )
    return token, True


def configure_logging(level_name: str) -> None:
    """Apply the configured log level, keeping nio's own chatter one step quieter."""
    level = LOG_LEVELS.get(level_name.lower(), logging.INFO)
    logging.getLogger().setLevel(level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if level_name.lower() != "trace":
        logging.getLogger("nio").setLevel(max(level, logging.INFO))
        logging.getLogger("peewee").setLevel(logging.WARNING)
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def leaf_paths() -> set[str]:
    """Every stored setting, as dotted paths — used by dev/validate.py."""
    paths: set[str] = set()
    for f in dataclass_fields(Config):
        if f.name in INTERNAL_FIELDS:
            continue
        value = getattr(Config(), f.name)
        if hasattr(value, "__dataclass_fields__"):
            paths |= {f"{f.name}.{sub}" for sub in value.__dataclass_fields__}
        else:
            paths.add(f.name)
    return paths
