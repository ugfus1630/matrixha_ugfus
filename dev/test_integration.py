#!/usr/bin/env python3
"""Tests for the integration's Supervisor discovery and its translations.

Runs against the installed Home Assistant, without Supervisor:

    PYTHONPATH=. python3 dev/test_integration.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.matrix_bridge.addon import AddonConnection, _extract
from custom_components.matrix_bridge.const import DEFAULT_PORT

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


def fake_info(**kwargs):
    base = {
        "slug": "local_matrix_bridge",
        "hostname": "local-matrix-bridge",
        "state": "started",
        "ingress_port": 8099,
        "options": {"api_token": "tok123"},
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_extract() -> None:
    print("\nAdd-on info extraction")
    conn = _extract(fake_info())
    check("host comes from the add-on hostname", conn.host == "local-matrix-bridge")
    check("port comes from the ingress port", conn.port == 8099)
    check("token comes from the add-on options", conn.token == "tok123")
    check("running reflects the started state", conn.running is True)

    stopped = _extract(fake_info(state="stopped"))
    check("stopped add-on is not running", stopped.running is False)

    # An add-on still on 0.2.x keeps its token in an `api` section, and only
    # moves it once it has restarted on 0.3.0.
    legacy = _extract(fake_info(options={"api": {"token": "old", "port": 9000}}))
    check("a 0.2.x token is still found", legacy.token == "old")

    # A token that has not been published yet must be reported as empty rather
    # than as the string "None", so the flow can abort with a clear reason.
    empty = _extract(fake_info(options={}))
    check("missing token yields an empty string", empty.token == "")

    no_opts = _extract(fake_info(options=None))
    check("absent options do not crash", isinstance(no_opts, AddonConnection))
    check("port falls back to ingress_port", no_opts.port == 8099)

    no_port = _extract(fake_info(options={}, ingress_port=None))
    check("port falls back to the default", no_port.port == DEFAULT_PORT)

    no_host = _extract(fake_info(hostname=None, slug="abc123_matrix_bridge"))
    check(
        "hostname falls back to the slug with hyphens",
        no_host.host == "abc123-matrix-bridge",
    )


def test_translations() -> None:
    print("\nTranslation coverage")
    component = ROOT / "custom_components" / "matrix_bridge"
    strings = json.loads((component / "strings.json").read_text())
    en = json.loads((component / "translations" / "en.json").read_text())
    source = (component / "config_flow.py").read_text()

    check("en.json matches strings.json", en == strings)

    declared_steps = set(strings["config"]["step"])
    implemented = set(re.findall(r"async def async_step_(\w+)", source))
    # `user` and `reauth` delegate to other steps and show no form of their own.
    showable = implemented - {"user", "reauth"}
    check(
        "every form step has translations",
        showable <= declared_steps,
        f"missing: {sorted(showable - declared_steps)}",
    )
    check(
        "no orphaned step translations",
        declared_steps <= implemented,
        f"orphaned: {sorted(declared_steps - implemented)}",
    )

    used_aborts = set(re.findall(r'async_abort\(reason="(\w+)"', source))
    declared_aborts = set(strings["config"]["abort"])
    check(
        "every abort reason is translated",
        used_aborts <= declared_aborts,
        f"missing: {sorted(used_aborts - declared_aborts)}",
    )

    used_errors = set(re.findall(r'errors\["base"\] = "(\w+)"', source))
    declared_errors = set(strings["config"]["error"])
    check(
        "every error code is translated",
        used_errors <= declared_errors,
        f"missing: {sorted(used_errors - declared_errors)}",
    )

    check(
        "no options-flow translations remain",
        "options" not in strings,
        "all settings should live in the add-on UI",
    )


def test_no_options_flow() -> None:
    print("\nConfiguration ownership")
    from homeassistant.config_entries import ConfigFlow

    from custom_components.matrix_bridge.config_flow import MatrixBridgeConfigFlow

    check(
        "integration exposes no options flow",
        MatrixBridgeConfigFlow.async_get_options_flow
        is ConfigFlow.async_get_options_flow,
        "an options flow would put settings outside the add-on UI",
    )

    # The settings the integration honours are edited in the add-on's panel,
    # not in the add-on's Supervisor configuration tab and not here.
    sys.path.insert(0, str(ROOT / "matrix_bridge"))
    from app.settings import FIELDS  # noqa: PLC0415

    editable = {field.path for field in FIELDS}
    for option in ("create_notify_entities", "fire_events"):
        check(
            f"{option} is editable in the add-on panel",
            f"homeassistant.{option}" in editable,
        )

    addon_config = (ROOT / "matrix_bridge" / "config.yaml").read_text()
    check(
        "no settings are left in the add-on configuration tab",
        "options:" not in addon_config,
        "settings belong in the panel, where they can be validated and applied live",
    )


def main() -> int:
    test_extract()
    test_translations()
    test_no_options_flow()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
