"""Tests for agentwire/core.py's posture resolution (#729).

Posture is the ONLY session axis, and every spawn defaults to the same one —
bypass — regardless of kind/topology: workers run bypass + damage-control just
like orchestrators (no tool-locking posture exists anymore). These tests pin
that flat rule plus the explicit-override precedence.
"""

from argparse import Namespace

from agentwire.core import _resolve_posture_from_args


class TestResolvePostureFromArgs:
    def test_default_is_bypass(self):
        args = Namespace(posture=None, bare=False, prompted=False)
        posture, err = _resolve_posture_from_args(args)
        assert err is None
        assert posture == "bypass"

    def test_invalid_posture_errors(self):
        args = Namespace(posture="restricted", bare=False, prompted=False)
        posture, err = _resolve_posture_from_args(args)
        assert posture is None
        assert err is not None
