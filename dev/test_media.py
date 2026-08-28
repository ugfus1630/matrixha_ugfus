#!/usr/bin/env python3
"""Unit tests for the pure helpers in app.media.

The image header parser is hand-rolled (to avoid a Pillow build dependency in
the add-on image) and the upload path allowlist is a security boundary, so both
are worth testing directly:

    PYTHONPATH=matrix_bridge python3 dev/test_media.py
"""

from __future__ import annotations

import asyncio
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "matrix_bridge"))

from app.media import (
    MediaError,
    MediaManager,
    ha_config_aliases,
    image_size,
    safe_filename,
)
from app.settings import Config, MediaConfig

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


def make_png(width: int, height: int) -> bytes:
    header = b"\x89PNG\r\n\x1a\n"
    ihdr_body = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr_body) - 4) + ihdr_body
    chunk += struct.pack(">I", zlib.crc32(ihdr_body) & 0xFFFFFFFF)
    return header + chunk


def make_gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 10


def make_jpeg(width: int, height: int) -> bytes:
    # SOI, a JFIF APP0 segment, then an SOF0 carrying the dimensions.
    out = b"\xff\xd8"
    app0_body = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    out += b"\xff\xe0" + struct.pack(">H", len(app0_body) + 2) + app0_body
    sof_body = b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9
    out += b"\xff\xc0" + struct.pack(">H", len(sof_body) + 2) + sof_body
    out += b"\xff\xd9"
    return out


def test_image_size() -> None:
    print("\nImage dimension parsing")
    check("PNG dimensions", image_size(make_png(1920, 1080)) == (1920, 1080))
    check("GIF dimensions", image_size(make_gif(320, 240)) == (320, 240))
    check("JPEG dimensions", image_size(make_jpeg(800, 600)) == (800, 600))
    check("JPEG with a preceding APP0 segment is skipped correctly",
          image_size(make_jpeg(1, 1)) == (1, 1))
    check("non-image returns None", image_size(b"not an image at all") is None)
    check(
        "truncated PNG returns None",
        image_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4) is None,
    )
    check("empty input returns None", image_size(b"") is None)


def test_safe_filename() -> None:
    print("\nFilename sanitisation")
    check("path traversal is stripped", safe_filename("../../etc/passwd") == "passwd")
    check("absolute path is stripped", safe_filename("/etc/shadow") == "shadow")
    check("unsafe characters are replaced", safe_filename("a b;c.jpg") == "a_b_c.jpg")
    check("leading dots are removed", safe_filename(".hidden") == "hidden")
    check("empty name falls back", safe_filename("") == "file")
    check("long names are truncated", len(safe_filename("x" * 500)) <= 120)


def test_upload_allowlist(tmp: Path) -> None:
    print("\nUpload path allowlist")
    allowed_dir = tmp / "allowed"
    secret_dir = tmp / "secret"
    allowed_dir.mkdir(parents=True, exist_ok=True)
    secret_dir.mkdir(parents=True, exist_ok=True)

    good = allowed_dir / "ok.png"
    good.write_bytes(make_png(4, 4))
    secret = secret_dir / "passwd"
    secret.write_text("root:x:0:0")

    # A symlink inside the allowed directory pointing outside it must not work.
    escape = allowed_dir / "escape"
    if not escape.exists():
        escape.symlink_to(secret)

    config = Config(
        homeserver="https://example.org",
        user_id="@bot:example.org",
        media=MediaConfig(allowed_upload_paths=[str(allowed_dir)]),
    )
    manager = MediaManager(client=None, config=config)

    async def run() -> None:
        media = await manager.from_path(str(good))
        check("allowed file is read", media.filename == "ok.png")
        check("mimetype is detected", media.mimetype == "image/png")

        try:
            await manager.from_path(str(secret))
            check("file outside the allowlist is refused", False, "(no error raised)")
        except MediaError:
            check("file outside the allowlist is refused", True)

        try:
            await manager.from_path(str(escape))
            check(
                "symlink escaping the allowlist is refused", False, "(no error raised)"
            )
        except MediaError:
            check("symlink escaping the allowlist is refused", True)

        try:
            await manager.from_path(str(allowed_dir / ".." / "secret" / "passwd"))
            check("relative traversal is refused", False, "(no error raised)")
        except MediaError:
            check("relative traversal is refused", True)

        # Oversize guard
        big = allowed_dir / "big.bin"
        big.write_bytes(b"\x00" * 2048)
        config.media.max_upload_mb = 0
        try:
            await manager.from_path(str(big))
            check("oversize upload is refused", False, "(no error raised)")
        except MediaError:
            check("oversize upload is refused", True)

    asyncio.run(run())


def test_ha_config_aliases() -> None:
    """Both names for Home Assistant's config directory must be interchangeable.

    Supervisor mounts it at /homeassistant; Home Assistant calls it /config in
    everything an operator writes. Accepting only one meant a path copied out
    of an automation named a directory the container does not have.
    """
    print("\nHome Assistant config directory aliasing")
    check(
        "/config gains its /homeassistant twin",
        ha_config_aliases(Path("/config/www/a.jpg"))
        == [Path("/config/www/a.jpg"), Path("/homeassistant/www/a.jpg")],
    )
    check(
        "/homeassistant gains its /config twin",
        ha_config_aliases(Path("/homeassistant/www/a.jpg"))
        == [Path("/homeassistant/www/a.jpg"), Path("/config/www/a.jpg")],
    )
    check(
        "unrelated paths are left alone",
        ha_config_aliases(Path("/media/a.jpg")) == [Path("/media/a.jpg")],
    )
    check(
        "a lookalike prefix is not rewritten",
        ha_config_aliases(Path("/configuration/a.jpg"))
        == [Path("/configuration/a.jpg")],
    )

    def allows(roots: list[str], candidate: str) -> bool:
        config = Config(
            homeserver="https://example.org",
            user_id="@bot:example.org",
            media=MediaConfig(allowed_upload_paths=roots),
        )
        manager = MediaManager(client=None, config=config)
        try:
            manager._assert_path_allowed(Path(candidate))
        except MediaError:
            return False
        return True

    check(
        "a /config root accepts a /homeassistant path",
        allows(["/config/www"], "/homeassistant/www/a.jpg"),
    )
    check(
        "a /homeassistant root accepts a /config path",
        allows(["/homeassistant/www"], "/config/www/a.jpg"),
    )
    check(
        "the new default accepts what automations write",
        allows(["/media", "/share", "/homeassistant/www"], "/config/www/snapshot.jpg"),
    )
    check(
        "aliasing does not widen the allowlist elsewhere",
        not allows(["/homeassistant/www"], "/etc/passwd"),
    )
    check(
        "aliasing does not escape the named subdirectory",
        not allows(["/homeassistant/www"], "/config/secrets.yaml"),
    )


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_image_size()
        test_safe_filename()
        test_ha_config_aliases()
        test_upload_allowlist(Path(tmp))

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
