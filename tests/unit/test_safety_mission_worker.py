"""Tests for mission-worker damage-control rules in ``agentwire.safety._core``.

When ``AGENTWIRE_SESSION_NAME`` matches ``{repo}/mission-{N}-{slug}``, the
hooks enforce two extra rules on top of the standard ruleset:

1. Edit/Write must target a path inside a ``*-worktrees/mission-N-...`` dir.
2. Bash ``git push --force`` is gated by branch name (mission-* only,
   never main/master/develop).
"""


import pytest

from agentwire.safety import _core


@pytest.fixture
def mission_session(monkeypatch):
    """Pretend we're inside a mission-worker tmux session."""
    monkeypatch.setenv("AGENTWIRE_SESSION_NAME", "agentwire-dev/mission-195-foo-bar")


@pytest.fixture
def non_mission_session(monkeypatch):
    """Pretend we're inside a regular session (no mission rules apply)."""
    monkeypatch.setenv("AGENTWIRE_SESSION_NAME", "agentwire-dev/main")


@pytest.fixture
def mission_worktree(tmp_path, monkeypatch):
    """Build a fake mission worktree directory and return its path."""
    wt = tmp_path / "projects" / "agentwire-dev-worktrees" / "mission-195-foo-bar"
    wt.mkdir(parents=True)
    return wt


# --- detection helper ---------------------------------------------------------


class TestIsMissionWorkerSession:
    def test_matches_canonical(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_SESSION_NAME", "agentwire-dev/mission-195-foo-bar")
        assert _core._is_mission_worker_session() is True

    def test_matches_short_repo(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_SESSION_NAME", "x/mission-1-y")
        assert _core._is_mission_worker_session() is True

    def test_not_mission_branch(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_SESSION_NAME", "agentwire-dev/main")
        assert _core._is_mission_worker_session() is False

    def test_no_slash(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_SESSION_NAME", "mission-1-x")
        # Not a session-form name (no repo prefix), should be False
        assert _core._is_mission_worker_session() is False

    def test_unset(self, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_SESSION_NAME", raising=False)
        assert _core._is_mission_worker_session() is False


# --- check_mission_worker_path ------------------------------------------------


class TestCheckMissionWorkerPath:
    def test_inside_worktree_allowed(self, mission_session, mission_worktree):
        target = mission_worktree / "src" / "foo.py"
        target.parent.mkdir(parents=True)
        target.write_text("x")
        blocked, _reason = _core.check_mission_worker_path(str(target))
        assert blocked is False

    def test_outside_worktree_blocked(self, mission_session, tmp_path):
        outside = tmp_path / "elsewhere" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("x")
        blocked, reason = _core.check_mission_worker_path(str(outside))
        assert blocked is True
        assert "worktree" in reason

    def test_canonical_repo_blocked(self, mission_session, tmp_path):
        repo = tmp_path / "projects" / "agentwire-dev" / "agentwire" / "main.py"
        repo.parent.mkdir(parents=True)
        repo.write_text("x")
        blocked, _reason = _core.check_mission_worker_path(str(repo))
        assert blocked is True

    def test_non_mission_session_no_constraint(self, non_mission_session, tmp_path):
        # Not a mission worker → check returns (False, "") regardless of path
        random = tmp_path / "outside.txt"
        random.write_text("x")
        blocked, _reason = _core.check_mission_worker_path(str(random))
        assert blocked is False

    def test_unset_session_no_constraint(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENTWIRE_SESSION_NAME", raising=False)
        random = tmp_path / "x.txt"
        random.write_text("y")
        blocked, _reason = _core.check_mission_worker_path(str(random))
        assert blocked is False

    def test_different_mission_worktree_still_allowed(self, mission_session, tmp_path):
        # Plan says any *-worktrees/mission-* path is allowed (not just this
        # mission's). Keeps the constraint simple — the hook can't know which
        # specific worktree this session owns.
        other = (
            tmp_path
            / "projects"
            / "other-project-worktrees"
            / "mission-7-bar"
            / "thing.py"
        )
        other.parent.mkdir(parents=True)
        other.write_text("x")
        blocked, _reason = _core.check_mission_worker_path(str(other))
        assert blocked is False


# --- check_mission_worker_bash ------------------------------------------------


class TestCheckMissionWorkerBash:
    def test_force_push_main_blocked(self, mission_session):
        cmd = "git push --force origin main"
        blocked, reason = _core.check_mission_worker_bash(cmd)
        assert blocked is True
        assert "main/master/develop" in reason

    def test_force_push_master_blocked(self, mission_session):
        blocked, _r = _core.check_mission_worker_bash("git push --force origin master")
        assert blocked is True

    def test_force_with_lease_main_blocked(self, mission_session):
        blocked, _r = _core.check_mission_worker_bash(
            "git push --force-with-lease origin main"
        )
        assert blocked is True

    def test_force_push_mission_branch_allowed(self, mission_session):
        blocked, _r = _core.check_mission_worker_bash(
            "git push --force origin mission-195-foo-bar"
        )
        assert blocked is False

    def test_force_push_other_branch_blocked(self, mission_session):
        blocked, reason = _core.check_mission_worker_bash(
            "git push --force origin some-other-branch"
        )
        assert blocked is True
        assert "mission-*" in reason

    def test_non_force_push_allowed(self, mission_session):
        # Plain `git push` isn't affected by this rule
        blocked, _r = _core.check_mission_worker_bash("git push origin main")
        assert blocked is False

    def test_non_mission_session_no_constraint(self, non_mission_session):
        # In a non-mission session, even force-pushing main is permitted by
        # THIS rule. (The standard ruleset has separate protections.)
        blocked, _r = _core.check_mission_worker_bash("git push --force origin main")
        assert blocked is False


# --- integration via check_command / check_path -------------------------------


class TestCheckCommandIntegration:
    def test_force_push_main_returns_block_decision(self, mission_session):
        result = _core.check_command(
            "git push --force origin main",
            {"safety": {"enabled": True}},
        )
        assert result["decision"] == "block"
        assert result["pattern"] == "mission-worker:force-push"

    def test_safety_disabled_overrides_mission_rules(self, mission_session):
        # Kill switch wins — disabled safety means even mission-worker rules
        # are skipped. This is documented behavior.
        result = _core.check_command(
            "git push --force origin main",
            {"safety": {"enabled": False}},
        )
        assert result["decision"] == "allow"
        assert result.get("disabled") is True

    def test_escape_hatch_overrides_mission_rules(self, mission_session):
        # Escape hatch comment also wins; the worker has explicitly noted intent
        result = _core.check_command(
            "git push --force origin main  # allow: hotfix for incident X",
            {"safety": {"enabled": True}},
        )
        assert result["decision"] == "allow"
        assert result.get("escape") is True


class TestCheckPathIntegration:
    def test_outside_worktree_blocked(self, mission_session, tmp_path):
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("x")
        blocked, reason = _core.check_path(str(outside), {"safety": {"enabled": True}})
        assert blocked is True
        assert "worktree" in reason

    def test_inside_worktree_allowed(self, mission_session, mission_worktree):
        target = mission_worktree / "a.py"
        target.write_text("x")
        blocked, _r = _core.check_path(str(target), {"safety": {"enabled": True}})
        assert blocked is False

    def test_safety_disabled_skips_mission_check(self, mission_session, tmp_path):
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("x")
        blocked, _r = _core.check_path(str(outside), {"safety": {"enabled": False}})
        assert blocked is False


# --- regex sanity (catch silly mistakes in the patterns) ----------------------


def test_force_push_regex_does_not_match_non_push():
    """The regex must not over-match: `git config --force` is not a push."""
    assert not _core._FORCE_PUSH_RE.search("git config --force foo")
    assert not _core._FORCE_PUSH_RE.search("git add --force")


def test_mission_worktree_regex_does_not_match_unrelated_paths():
    assert not _core._MISSION_WORKTREE_PATH_RE.search("/Users/x/projects/foo/main.py")
    assert not _core._MISSION_WORKTREE_PATH_RE.search("/var/log/mission-1-thing.log")
    # Right form
    assert _core._MISSION_WORKTREE_PATH_RE.search(
        "/Users/x/projects/agentwire-dev-worktrees/mission-1-thing/a.py"
    )
