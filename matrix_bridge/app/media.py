"""Upload and download of pictures and files, including E2EE attachments.

Outbound files are constrained to an operator-controlled allowlist of
directories, resolved through ``Path.resolve()`` so a symlink or ``..`` cannot
be used to read arbitrary paths out of the container.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from nio import DiskDownloadResponse, MemoryDownloadResponse, UploadResponse
from nio.crypto import attachments

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nio import AsyncClient

    from .settings import Config

_LOGGER = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Home Assistant's configuration directory has two names. Supervisor mounts it
# into add-ons at /homeassistant (the `homeassistant_config` entry in
# config.yaml), but Home Assistant itself calls it /config everywhere the
# operator meets it: automation paths, `camera.snapshot` targets, and the `www`
# folder served at /local. A path copied from any of those arrives here as
# /config/... and names a directory this container does not have, so both
# spellings are accepted and mapped onto whichever one exists.
_HA_CONFIG_MOUNT = Path("/homeassistant")
_HA_CONFIG_ALIAS = Path("/config")


def _is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` sits inside ``root``. Both must already be resolved."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _swap_root(path: Path, old: Path, new: Path) -> Path | None:
    """Rewrite ``path`` from under ``old`` to under ``new``."""
    try:
        return new / path.relative_to(old)
    except ValueError:
        return None


def ha_config_aliases(path: Path) -> list[Path]:
    """Return ``path`` plus the other name Home Assistant has for it."""
    pairs = (
        (_HA_CONFIG_ALIAS, _HA_CONFIG_MOUNT),
        (_HA_CONFIG_MOUNT, _HA_CONFIG_ALIAS),
    )
    for old, new in pairs:
        swapped = _swap_root(path, old, new)
        if swapped is not None:
            return [path, swapped]
    return [path]


class MediaError(Exception):
    """Raised for any recoverable media problem; message is user-facing."""


@dataclass(slots=True)
class OutboundMedia:
    data: bytes
    filename: str
    mimetype: str

    @property
    def msgtype(self) -> str:
        if self.mimetype.startswith("image/"):
            return "m.image"
        if self.mimetype.startswith("video/"):
            return "m.video"
        if self.mimetype.startswith("audio/"):
            return "m.audio"
        return "m.file"


def safe_filename(name: str, fallback: str = "file") -> str:
    """Reduce a remote-supplied filename to something safe to write."""
    name = Path(name or "").name
    name = _UNSAFE.sub("_", name).lstrip(".")
    return name[:120] or fallback


def image_size(data: bytes) -> tuple[int, int] | None:
    """Read pixel dimensions from PNG/GIF/JPEG headers.

    Done by hand rather than pulling in Pillow, which would be the only
    dependency in the image needing a compiler on Alpine.
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            width, height = struct.unpack(">II", data[16:24])
            return int(width), int(height)

        if data[:6] in (b"GIF87a", b"GIF89a"):
            width, height = struct.unpack("<HH", data[6:10])
            return int(width), int(height)

        if data[:2] == b"\xff\xd8":
            index = 2
            while index < len(data) - 9:
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                # SOF0-SOF15, excluding the non-dimensional DHT/JPG/DAC markers.
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                    return int(width), int(height)
                (segment_length,) = struct.unpack(">H", data[index + 2 : index + 4])
                index += 2 + segment_length
    except (struct.error, IndexError):  # pragma: no cover - malformed input
        pass
    return None


class MediaManager:
    """Handles both directions of file transfer for the bot."""

    def __init__(self, client: AsyncClient, config: Config) -> None:
        self._client = client
        self._config = config

    # --------------------------------------------------------------- outbound

    def _assert_path_allowed(self, path: Path) -> Path:
        """Resolve a requested path, refusing anything outside the allowlist.

        Both spellings of Home Assistant's configuration directory are tried,
        on the requested path and on every configured root, so an operator who
        wrote one and a setting that names the other still agree.
        """
        allowed = self._config.media.allowed_upload_paths
        roots = [
            alias.resolve()
            for root in allowed
            for alias in ha_config_aliases(Path(root))
        ]
        candidates = [alias.resolve() for alias in ha_config_aliases(path)]

        permitted = [
            candidate
            for candidate in candidates
            if any(_is_within(candidate, root) for root in roots)
        ]
        if not permitted:
            raise MediaError(
                f"{candidates[0]} is outside the allowed upload paths "
                f"({', '.join(allowed)}). Add its directory to "
                "'media.allowed_upload_paths' in the add-on configuration."
            )

        # A path that exists wins, so /config/... lands on the mount actually
        # holding the file. Otherwise keep the spelling the caller used, so the
        # "No such file" that follows names what they asked for.
        return next((path for path in permitted if path.is_file()), permitted[0])

    async def from_path(self, raw_path: str) -> OutboundMedia:
        path = self._assert_path_allowed(Path(raw_path))
        if not path.is_file():
            raise MediaError(f"No such file: {path}")

        size = path.stat().st_size
        if size > self._config.media.max_upload_bytes:
            raise MediaError(
                f"{path.name} is {size / 1048576:.1f} MB, over the "
                f"{self._config.media.max_upload_mb} MB upload limit."
            )

        data = path.read_bytes()
        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return OutboundMedia(data=data, filename=path.name, mimetype=mimetype)

    async def from_url(self, url: str) -> OutboundMedia:
        limit = self._config.media.max_upload_bytes
        timeout = aiohttp.ClientTimeout(total=60)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as response,
        ):
            if response.status != 200:
                raise MediaError(f"Fetching {url} returned HTTP {response.status}")

            declared = response.content_length
            if declared and declared > limit:
                raise MediaError(
                    f"Remote file is {declared / 1048576:.1f} MB, over the "
                    f"{self._config.media.max_upload_mb} MB upload limit."
                )

            # Read with a cap so a server lying about Content-Length (or
            # omitting it) cannot exhaust memory.
            data = await response.content.read(limit + 1)
            if len(data) > limit:
                raise MediaError(
                    f"Remote file exceeds the "
                    f"{self._config.media.max_upload_mb} MB upload limit."
                )

            mimetype = (response.content_type or "application/octet-stream").split(";")[
                0
            ]
            filename = safe_filename(
                Path(str(response.url.path)).name, fallback="download"
            )

        return OutboundMedia(data=data, filename=filename, mimetype=mimetype)

    async def upload(self, media: OutboundMedia, encrypt: bool) -> dict[str, Any]:
        """Upload to the media repository and build the event content."""
        import io

        response, keys = await self._client.upload(
            data_provider=lambda *_: io.BytesIO(media.data),
            content_type=media.mimetype,
            filename=media.filename,
            encrypt=encrypt,
            filesize=len(media.data),
        )

        if not isinstance(response, UploadResponse):
            raise MediaError(f"Upload failed: {response}")

        info: dict[str, Any] = {
            "size": len(media.data),
            "mimetype": media.mimetype,
        }
        if media.mimetype.startswith("image/"):
            dimensions = image_size(media.data)
            if dimensions:
                info["w"], info["h"] = dimensions

        content: dict[str, Any] = {
            "msgtype": media.msgtype,
            "body": media.filename,
            "info": info,
        }

        if encrypt and keys:
            # In an encrypted room the decryption keys travel in `file`, and the
            # plain `url` key must be absent.
            content["file"] = {**keys, "url": response.content_uri}
        else:
            content["url"] = response.content_uri

        return content

    # ---------------------------------------------------------------- inbound

    def _target_path(self, filename: str) -> Path:
        directory = Path(self._config.media.download_path)
        directory.mkdir(parents=True, exist_ok=True)

        candidate = directory / safe_filename(filename, fallback="attachment")
        if not candidate.exists():
            return candidate

        stem, suffix = candidate.stem, candidate.suffix
        return directory / f"{stem}-{int(time.time())}{suffix}"

    async def download(self, event: Any) -> Path | None:
        """Download (and decrypt, if needed) an inbound attachment."""
        if not self._config.media.download_incoming:
            return None

        mxc = getattr(event, "url", None)
        if not mxc:
            return None

        response = await self._client.download(mxc=mxc)
        if not isinstance(response, (DiskDownloadResponse, MemoryDownloadResponse)):
            _LOGGER.warning("Could not download %s: %s", mxc, response)
            return None

        data = response.body
        if len(data) > self._config.media.max_download_bytes:
            _LOGGER.warning(
                "Skipping %s: %.1f MB exceeds the %s MB download limit",
                mxc,
                len(data) / 1048576,
                self._config.media.max_download_mb,
            )
            return None

        # Encrypted rooms deliver RoomEncrypted* events whose payload is
        # ciphertext; the keys ride along on the event itself.
        key = getattr(event, "key", None)
        if key:
            try:
                data = attachments.decrypt_attachment(
                    data,
                    key["k"],
                    event.hashes["sha256"],
                    event.iv,
                )
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.error("Failed to decrypt attachment %s: %s", mxc, err)
                return None

        filename = getattr(event, "body", None) or response.filename or "attachment"
        path = self._target_path(filename)
        path.write_bytes(data)
        _LOGGER.info("Saved inbound attachment to %s (%s bytes)", path, len(data))

        self.prune()
        return path

    def prune(self) -> None:
        """Delete downloads older than the configured retention period."""
        days = self._config.media.retention_days
        if days <= 0:
            return

        directory = Path(self._config.media.download_path)
        if not directory.is_dir():
            return

        cutoff = time.time() - days * 86400
        for entry in directory.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    _LOGGER.debug("Pruned expired download %s", entry)
            except OSError as err:  # pragma: no cover - best effort
                _LOGGER.debug("Could not prune %s: %s", entry, err)
