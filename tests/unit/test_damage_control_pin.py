"""agentwire's `damage-control-dev` pin is the only thing tying it to the engine (#1209 M8).

The engine has one source: the published `damage-control-dev` (github.com/dotdevdotdev/damage-control-dev).
A checkout and a wheel both resolve it from PyPI against the range declared in pyproject.toml, so
the declared floor/ceiling must parse, be a real range, and admit the engine actually installed.
"""

import re
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO = Path(__file__).resolve().parent.parent.parent


def _pin(pyproject_text: str) -> Requirement:
    # Text match, not tomllib: the project supports Python 3.10, which has no tomllib.
    pins = re.findall(r'^\s*"(damage-control-dev[^"]*)"', pyproject_text, re.M)
    assert len(pins) == 1, pins
    return Requirement(pins[0])


def _bounds(spec: SpecifierSet):
    floor = [s for s in spec if s.operator == ">="]
    ceiling = [s for s in spec if s.operator == "<"]
    return floor, ceiling


def test_the_declared_pin_is_a_floor_and_a_ceiling():
    floor, ceiling = _bounds(_pin((REPO / "pyproject.toml").read_text()).specifier)
    assert len(floor) == 1 and len(ceiling) == 1, "damage-control-dev must be pinned as >=floor,<ceiling"
    assert Version(floor[0].version) < Version(ceiling[0].version)


def test_the_installed_engine_satisfies_the_pin():
    pin = _pin((REPO / "pyproject.toml").read_text())
    installed = Version(metadata.version("damage-control-dev"))
    assert installed in pin.specifier, f"installed damage-control-dev {installed} is outside the pin {pin.specifier}"


def test_mutation_the_check_rejects_an_engine_below_the_floor_or_a_missing_ceiling():
    """Must-fail control: the same predicates, fed a pin/engine pair that is wrong."""
    pin = _pin('    "damage-control-dev>=0.1.3,<0.2",')
    assert Version("0.1.2") not in pin.specifier and Version("0.2.0") not in pin.specifier
    assert Version("0.1.3") in pin.specifier
    floor, ceiling = _bounds(_pin('    "damage-control-dev>=0.1.3",').specifier)
    assert not (len(floor) == 1 and len(ceiling) == 1)
