"""Tests for agentwire/core.py's posture defaulting (#716).

Posture used to be keyed by the old 3-way kind (orchestrator/worktree-session
-> bypass, worker -> restricted). Collapsing the kind enum to {orchestrator,
worker} made that table ambiguous: "worker" now covers BOTH a pane (agentwire
spawn, restricted) and a standalone worktree session (agentwire worktree,
bypass) — a flat lookup on kind alone would silently flip every worktree
session from bypass to permission-prompting restricted. These tests pin the
corrected 2-input rule: restricted only for a worker that is NOT isolated on
its own worktree.
"""

from argparse import Namespace

from agentwire.core import _default_posture, _resolve_posture_from_args


class TestDefaultPosture:
    def test_orchestrator_is_always_bypass(self):
        assert _default_posture("orchestrator") == "bypass"
        assert _default_posture("orchestrator", worktree_topology=True) == "bypass"
        assert _default_posture("orchestrator", worktree_topology=False) == "bypass"

    def test_worker_on_worktree_topology_is_bypass(self):
        # agentwire worktree's common case — isolated, no live-watcher, full autonomy.
        assert _default_posture("worker", worktree_topology=True) == "bypass"

    def test_worker_off_worktree_topology_is_restricted(self):
        # agentwire spawn (a pane) and a main-topology `new --kind worker` —
        # sharing a live checkout someone else may be watching.
        assert _default_posture("worker", worktree_topology=False) == "restricted"
        assert _default_posture("worker") == "restricted"

    def test_unknown_kind_defaults_bypass(self):
        assert _default_posture(None) == "bypass"
        assert _default_posture("nope") == "bypass"


class TestResolvePostureFromArgs:
    def test_pane_worker_default_matches_status_quo(self):
        # cmd_spawn's call site: _resolve_posture_from_args(args, "worker")
        # with no worktree_topology kwarg — must still resolve restricted.
        args = Namespace(posture=None, bare=False,
                          restricted=False, prompted=False)
        posture, err = _resolve_posture_from_args(args, "worker")
        assert err is None
        assert posture == "restricted"

    def test_worktree_worker_resolves_bypass(self):
        args = Namespace(posture=None, bare=False,
                          restricted=False, prompted=False)
        posture, err = _resolve_posture_from_args(args, "worker", worktree_topology=True)
        assert err is None
        assert posture == "bypass"

    def test_orchestrator_resolves_bypass_regardless_of_topology(self):
        args = Namespace(posture=None, bare=False,
                          restricted=False, prompted=False)
        for topology in (True, False):
            posture, err = _resolve_posture_from_args(args, "orchestrator", worktree_topology=topology)
            assert err is None
            assert posture == "bypass"

    def test_explicit_posture_overrides_the_default(self):
        # worktree_topology=True would otherwise default to bypass — an
        # explicit --posture prompted must win regardless.
        args = Namespace(posture="prompted", bare=False,
                          restricted=False, prompted=False)
        posture, err = _resolve_posture_from_args(args, "worker", worktree_topology=True)
        assert err is None
        assert posture == "prompted"

    def test_explicit_bare_boolean(self):
        args = Namespace(posture=None, bare=True, restricted=False, prompted=False)
        posture, err = _resolve_posture_from_args(args, "orchestrator")
        assert err is None
        assert posture == "bare"
