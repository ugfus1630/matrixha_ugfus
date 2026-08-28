"""Persistence for the Matrix session.

The nio crypto store (Megolm sessions, device keys, trust state) lives in
``/data/store`` and is managed by nio itself. This module only handles the small
session descriptor that lets the bot restore its login — and therefore its
device identity — across restarts. Keeping the same device_id is what stops the
bot from spawning a new unverified device on every add-on restart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Session:
    user_id: str
    device_id: str
    access_token: str
    homeserver: str


def load_session(
    path: Path, expected_user: str, expected_homeserver: str
) -> Session | None:
    """Return the stored session if it matches the current configuration."""
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        session = Session(
            user_id=raw["user_id"],
            device_id=raw["device_id"],
            access_token=raw["access_token"],
            homeserver=raw.get("homeserver", expected_homeserver),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as err:
        _LOGGER.warning("Ignoring unreadable session file %s: %s", path, err)
        return None

    # If the operator repoints the add-on at a different account or homeserver,
    # the old device identity and crypto store no longer apply.
    if session.user_id != expected_user or session.homeserver != expected_homeserver:
        _LOGGER.warning(
            "Stored session is for %s on %s but configuration says %s on %s; "
            "logging in fresh.",
            session.user_id,
            session.homeserver,
            expected_user,
            expected_homeserver,
        )
        return None

    return session


def save_session(path: Path, session: Session) -> None:
    """Write the session atomically so a crash mid-write cannot corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    _LOGGER.debug(
        "Saved session for %s (device %s)", session.user_id, session.device_id
    )


def clear_session(path: Path) -> None:
    """Remove a stored session, e.g. after the server rejects its token."""
    path.unlink(missing_ok=True)
