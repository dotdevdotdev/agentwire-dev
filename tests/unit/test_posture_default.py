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

    def test_worker_also_defaults_bypass(self):
        # A worker pane no longer gets a restricted posture — same default as any
        # other spawn; damage-control is its guard.
        args = Namespace(posture=None, bare=False, prompted=False)
        posture, err = _resolve_posture_from_args(args)
        assert err is None
        assert posture == "bypass"

    def test_explicit_posture_wins(self):
        args = Namespace(posture="prompted", bare=False, prompted=False)
        posture, err = _resolve_posture_from_args(args)
        assert err is None
        assert posture == "prompted"

    def test_explicit_auto(self):
        args = Namespace(posture="auto", bare=False, prompted=False)
        posture, err = _resolve_posture_from_args(args)
        assert err is None
        assert posture == "auto"

    def test_invalid_posture_errors(self):
        args = Namespace(posture="restricted", bare=False, prompted=False)
        posture, err = _resolve_posture_from_args(args)
        assert posture is None
        assert err is not None

    def test_bare_boolean(self):
        args = Namespace(posture=None, bare=True, prompted=False)
        posture, err = _resolve_posture_from_args(args)
        assert err is None
        assert posture == "bare"

    def test_prompted_boolean(self):
        args = Namespace(posture=None, bare=False, prompted=True)
        posture, err = _resolve_posture_from_args(args)
        assert err is None
        assert posture == "prompted"
