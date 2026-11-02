"""agentwire's `damage-control-dev` pin must admit the workspace package's own version.

A checkout builds the engine from `packages/damage-control-dev` (uv workspace source), but a
PyPI install resolves it from PyPI against the declared range. If the workspace version drifts
outside that range, the checkout tests one engine and the wheel ships against another.
"""

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parent.parent.parent
ENGINE_INIT = REPO / "packages" / "damage-control-dev" / "src" / "damage_control" / "__init__.py"


def _pin() -> Requirement:
    # Text match, not tomllib: the project supports Python 3.10, which has no tomllib.
    pins = re.findall(
        r'^\s*"(damage-control-dev[^"]*)"', (REPO / "pyproject.toml").read_text(), re.M
    )
    assert len(pins) == 1, pins
    return Requirement(pins[0])


def test_workspace_version_is_inside_the_pin():
    match = re.search(r'^__version__ = "([^"]+)"', ENGINE_INIT.read_text(), re.M)
    assert match, "no __version__ in damage_control/__init__.py"
    version = Version(match.group(1))
    pin = _pin()
    assert pin.specifier, "damage-control-dev must be pinned to a range, not left open"
    assert version in pin.specifier, f"workspace {version} falls outside the pin {pin.specifier}"
