"""Unit tests for ``agentwire helper`` — the no-isolation worker session (#838).

The feature is composition, so these tests pin the composition: what
``cmd_helper`` hands ``cmd_new``, and the invariants that make sharing a
checkout safe (guard declared, git untouched, registry untouched, the
git-state constraint actually reaching the agent as a role).
"""

import re
import subprocess
from pathlib import Path

import pytest

from agentwire import worktree_cli, worktree_registry
from agentwire.roles import discover_role, resolve_roles
from agentwire.worktree import worktree_session_name


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one commit (so ``git worktree list`` works)."""
    root = tmp_path / "myapp"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


@pytest.fixture
def captured_new(monkeypatch):
    """Replace ``session_cli.cmd_new`` and capture the args it's handed."""
    from agentwire import session_cli

    seen = {}

    def fake_cmd_new(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(session_cli, "cmd_new", fake_cmd_new)
    return seen


def _run(repo, captured_new, **kwargs):
    import argparse
    defaults = dict(name="digest", project=str(repo), json=True, prompt=None,
                    roles=None, posture=None, model=None, env=None,
                    created_by=None, caller_session=None, no_cohort=False,
                    no_soul=False)
    defaults.update(kwargs)
    assert worktree_cli.cmd_helper(argparse.Namespace(**defaults)) == 0
    return captured_new["args"]


# --- worktree_session_name: SSOT for the flat {project}-{safe} convention ---

class TestSessionName:
    def test_flat_convention(self, tmp_path):
        assert worktree_session_name(tmp_path / "myapp", "fix-bug") == "myapp-fix-bug"

    def test_unsafe_chars_sanitized(self, tmp_path):
        assert worktree_session_name(tmp_path / "myapp", "feat/ui: v2.0") == "myapp-feat-ui-v2-0"

    def test_empty_after_sanitizing_falls_back(self, tmp_path):
        assert worktree_session_name(tmp_path / "myapp", "///") == "myapp-wt"

    def test_no_slash_so_cmd_new_cannot_read_it_as_a_branch(self, tmp_path):
        # A "project/branch" name would send cmd_new down the worktree path —
        # the exact thing this verb must never trigger.
        from agentwire.worktree import parse_session_name
        name = worktree_session_name(tmp_path / "myapp", "some/thing")
        assert parse_session_name(name)[1] is None

    @pytest.mark.parametrize("name", ["fix-bug", "feat/ui: v2.0", "///", "a b"])
    def test_matches_cmd_worktree_inlined_copy(self, tmp_path, name):
        """Byte-identical to session_cli.cmd_worktree's inlined derivation.

        Pins the equivalence so collapsing those two lines to a call to this
        function stays a safe no-op (see the #838 PR body).
        """
        project = tmp_path / "myapp"
        inlined_safe = re.sub(r"[\s/:.]+", "-", name).strip("-") or "wt"
        assert worktree_session_name(project, name) == f"{project.name}-{inlined_safe}"

    def test_dot_in_the_project_dir_is_made_tmux_legal(self, tmp_path):
        """A helper in ``~/.claude`` must get the name tmux can actually
        create — cmd_new maps '.' to '_', and a derivation that doesn't
        agree is #868's leaked session."""
        assert worktree_session_name(tmp_path / ".claude", "digest") == "_claude-digest"


# --- What cmd_helper hands cmd_new -----------------------------------------

class TestComposition:
    def test_shares_the_checkout_verbatim(self, repo, captured_new):
        assert Path(_run(repo, captured_new).path) == repo

    def test_session_named_by_the_shared_convention(self, repo, captured_new):
        assert _run(repo, captured_new).session == "myapp-digest"

    def test_role_is_worker_on_main_topology(self, repo, captured_new):
        args = _run(repo, captured_new)
        # kind=worker + worktree_topology=False selects `worker.md` (written
        # for "a standalone session on the same checkout") and enrolls in the
        # cohort as topology "main" — so `wait --children` reaps it.
        assert args.kind == "worker"
        assert args.worktree_topology is False

    def test_declares_the_shared_dir_without_force(self, repo, captured_new):
        args = _run(repo, captured_new)
        assert args.allow_shared_dir is True
        # --force would additionally kill-replace a live same-name session.
        assert args.force is False

    def test_branchless_flags_stay_none(self, repo, captured_new):
        # cmd_new errors loudly if --base/--pull-first are set without a branch.
        args = _run(repo, captured_new)
        assert args.base is None and args.pull_first is None

    def test_prompt_becomes_first_message(self, repo, captured_new):
        assert _run(repo, captured_new, prompt="go").first_message == "go"

    def test_rooting_and_cohort_left_to_cmd_new(self, repo, captured_new):
        # A helper is in the caller's own project, so cmd_new's default
        # created_by resolution parents it — no override here.
        args = _run(repo, captured_new)
        assert args.created_by is None
        assert args.no_cohort is False


class TestRoles:
    def test_shared_checkout_role_is_injected(self, repo, captured_new):
        assert _run(repo, captured_new).roles == "shared-checkout"

    def test_user_roles_stack_behind_it(self, repo, captured_new):
        assert _run(repo, captured_new, roles="react, wiki").roles == "shared-checkout,react,wiki"

    def test_user_cannot_duplicate_it(self, repo, captured_new):
        assert _run(repo, captured_new, roles="shared-checkout").roles == "shared-checkout"

    def test_resolves_after_the_non_overridable_worker_rail(self):
        # Recency weight: shared-checkout's "never mutate git state" lands
        # after worker.md's "commit your work", which is wrong here.
        assert resolve_roles(
            "worker", worktree_topology=False, cli_roles=["shared-checkout"],
        ) == ["worker", "shared-checkout"]

    def test_role_file_is_discoverable(self):
        assert discover_role("shared-checkout") is not None


class TestCheckoutResolution:
    def test_subdir_normalizes_to_the_git_root(self, repo, captured_new):
        assert Path(_run(repo, captured_new, project=str(repo / "src")).path) == repo

    def test_linked_worktree_shares_itself_not_the_main_checkout(self, repo, captured_new, tmp_path):
        """A worktree worker spawning a helper wants it in ITS files."""
        linked = tmp_path / "wt-feature"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q",
                        "-b", "feature", str(linked)], check=True)
        assert Path(_run(repo, captured_new, project=str(linked)).path) == linked

    def test_non_git_directory_is_allowed(self, tmp_path, captured_new):
        plain = tmp_path / "notes"
        plain.mkdir()
        assert Path(_run(plain, captured_new, project=str(plain)).path) == plain

    def test_missing_directory_fails(self, tmp_path, captured_new):
        import argparse
        rc = worktree_cli.cmd_helper(argparse.Namespace(
            name="x", project=str(tmp_path / "nope"), json=True))
        assert rc != 0
        assert "args" not in captured_new

    def test_name_is_required(self, repo, captured_new):
        import argparse
        assert worktree_cli.cmd_helper(argparse.Namespace(
            name=None, project=str(repo), json=True)) != 0
        assert "args" not in captured_new


class TestNoGitFootprint:
    """The whole premise: zero git operations, nothing to clean up."""

    def test_creates_no_worktree_and_no_branch(self, repo, captured_new):
        before_wt = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                                   capture_output=True, text=True).stdout
        before_br = subprocess.run(["git", "-C", str(repo), "branch", "--list"],
                                   capture_output=True, text=True).stdout
        _run(repo, captured_new)
        after_wt = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                                  capture_output=True, text=True).stdout
        after_br = subprocess.run(["git", "-C", str(repo), "branch", "--list"],
                                  capture_output=True, text=True).stdout
        assert before_wt == after_wt
        assert before_br == after_br

    def test_writes_nothing_to_the_worktree_registry(self, repo, captured_new,
                                                     tmp_path, monkeypatch):
        # A registry entry pointing at the repo's OWN checkout is a resource
        # that doesn't exist: --remove can't resolve it (find_git_worktree
        # never returns the main checkout) and --prune can never drop it
        # (the dir is always there). So nothing is written.
        monkeypatch.setattr(worktree_registry, "REGISTRY_DIR", tmp_path / "registry")
        _run(repo, captured_new)
        assert worktree_registry.entries(repo) == []
        assert worktree_registry.all_entries() == []

    def test_leaves_the_shared_tree_untouched(self, repo, captured_new):
        _run(repo, captured_new)
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        assert status == ""


def test_registered_on_the_cli():
    from agentwire.__main__ import build_parser
    args = build_parser().parse_args(["helper", "digest", "-p", "/tmp/x"])
    assert args.func is worktree_cli.cmd_helper
    assert args.name == "digest"
