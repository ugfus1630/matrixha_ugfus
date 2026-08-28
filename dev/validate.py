#!/usr/bin/env python3
"""Static consistency checks that run without Docker or a homeserver.

    python3 dev/validate.py

Checks that add-on options and schema agree, that the integration manifest,
services and translations line up, and that the bridge event names used by the
add-on and the integration are the same set.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "matrix_bridge"
COMPONENT = ROOT / "custom_components" / "matrix_bridge"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


def flatten(data: dict, prefix: str = "") -> set[str]:
    """Flatten a nested options/schema mapping into dotted keys."""
    keys: set[str] = set()
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= flatten(value, f"{path}.")
        else:
            keys.add(path)
    return keys


def validate_addon() -> None:
    print("\nAdd-on")
    config = yaml.safe_load((ADDON / "config.yaml").read_text())

    for field in ("name", "version", "slug", "description", "arch"):
        check(f"config.yaml has {field}", field in config)

    check("architectures are amd64 + aarch64",
          sorted(config["arch"]) == ["aarch64", "amd64"])
    check("ingress is enabled", config.get("ingress") is True)
    check("init is false (base image supplies s6)", config.get("init") is False)

    sys.path.insert(0, str(ADDON))
    from app import settings  # noqa: PLC0415

    check("ingress port matches the API port",
          config.get("ingress_port") == settings.API_PORT)

    # Settings live in the panel, not in the Supervisor configuration tab. The
    # generated API token is the one thing that has to pass through Supervisor,
    # because that is how the integration discovers the add-on.
    schema = flatten(config.get("schema") or {})
    check("the configuration tab holds no settings", schema == {"api_token"},
          f"unexpected add-on options: {sorted(schema - {'api_token'})}")
    check("api_token is optional (the add-on writes it itself)",
          config["schema"]["api_token"].endswith("?"))
    check("no default options are declared", "options" not in config)

    build = yaml.safe_load((ADDON / "build.yaml").read_text())
    check("build.yaml covers every declared arch",
          set(build["build_from"]) == set(config["arch"]))

    dockerfile = (ADDON / "Dockerfile").read_text()
    check("Dockerfile uses the BUILD_FROM arg", "ARG BUILD_FROM" in dockerfile)
    check("run.sh is referenced", "run.sh" in dockerfile)

    requirements = (ADDON / "requirements.txt").read_text()
    check("matrix-nio is pinned with the e2e extra",
          "matrix-nio[e2e]==" in requirements)

    # The form and the code must describe the same settings: a field missing
    # from FIELDS is uneditable, and one that no dataclass has cannot be saved.
    stored = settings.leaf_paths()
    described = {field.path for field in settings.FIELDS}
    check("every setting is editable in the panel", not (stored - described),
          f"missing from FIELDS: {sorted(stored - described)}")
    check("every form field is a real setting", not (described - stored),
          f"not stored anywhere: {sorted(described - stored)}")

    # Every setting the panel offers must actually do something. A setting that
    # nothing reads is a lie told to the operator.
    sources = "\n".join(
        path.read_text()
        for path in (ADDON / "app").rglob("*.py")
        if path.name != "settings.py"
    )
    leaf_names = {path.split(".")[-1] for path in stored}
    unused = sorted(name for name in leaf_names if name not in sources)
    check("every setting is read somewhere outside settings.py", not unused,
          f"unused: {unused}")

    # The panel is the configuration surface, so it must render every kind of
    # field the settings can contain.
    panel = (ADDON / "app" / "web" / "app.js").read_text()
    kinds = {field.kind for field in settings.FIELDS}
    unhandled = sorted(kind for kind in kinds if f'"{kind}"' not in panel)
    check("the panel renders every field kind", not unhandled,
          f"unhandled kinds: {unhandled}")


def validate_integration() -> None:
    print("\nIntegration")
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    check("domain is matrix_bridge (no clash with core `matrix`)",
          manifest["domain"] == "matrix_bridge")
    check("config_flow is declared", manifest.get("config_flow") is True)
    check("version is set (required for custom integrations)",
          bool(manifest.get("version")))
    check("iot_class is local_push", manifest.get("iot_class") == "local_push")

    services = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    strings = json.loads((COMPONENT / "strings.json").read_text())
    translations = json.loads((COMPONENT / "translations" / "en.json").read_text())

    check("strings.json documents every service",
          sorted(strings["services"]) == sorted(services),
          f"{sorted(set(services) ^ set(strings['services']))}")
    check("en.json matches strings.json", translations == strings)

    for name, definition in services.items():
        documented = set(strings["services"][name].get("fields", {}))
        declared = set((definition or {}).get("fields", {}))
        check(f"{name}: fields documented", documented == declared,
              f"{sorted(documented ^ declared)}")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_event_contract() -> None:
    print("\nAdd-on <-> integration event contract")
    sys.path.insert(0, str(ADDON))

    from app import events as addon_events  # noqa: PLC0415

    # const.py is nothing but constants, but reaching it as
    # custom_components.matrix_bridge.const would first execute the package's
    # __init__.py, which imports Home Assistant and voluptuous. These checks are
    # meant to run without either, so load the module straight from its file.
    const = _load_module("matrix_bridge_const", COMPONENT / "const.py")

    addon_types = {
        addon_events.STATUS, addon_events.ROOMS_CHANGED, addon_events.MESSAGE,
        addon_events.IMAGE, addon_events.FILE, addon_events.REACTION,
        addon_events.VERIFICATION, addon_events.UNDECRYPTABLE, addon_events.ERROR,
    }
    integration_types = {
        const.BRIDGE_STATUS, const.BRIDGE_ROOMS_CHANGED, const.BRIDGE_MESSAGE,
        const.BRIDGE_IMAGE, const.BRIDGE_FILE, const.BRIDGE_REACTION,
        const.BRIDGE_VERIFICATION, const.BRIDGE_UNDECRYPTABLE, const.BRIDGE_ERROR,
    }
    check("both sides agree on the event vocabulary",
          addon_types == integration_types,
          f"{sorted(addon_types ^ integration_types)}")
    check("every mapped bridge event is a known type",
          set(const.EVENT_MAP) <= addon_types)


def main() -> int:
    validate_addon()
    validate_integration()
    validate_event_contract()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
