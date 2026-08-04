"""Unit tests for tmux-legal session-name derivation (#868).

tmux forbids ``.`` in session names, so every creation path maps it to ``_``.
Resolution used the project directory's name RAW, so for any project whose
directory contains a dot (``~/.claude``, ``~/projects/dotdev.dev``) teardown
derived a name no session could ever have — matched nothing, and said it had
removed one anyway. Same class as #855 on the session-name axis.
"""

import pytest

from agentwire.worktree import (
    safe_worktree_name,
    teardown_session_note,
    tmux_safe_name,
    worktree_session_name,
)


class TestTmuxSafeName:
    def test_dots_become_underscores(self):
        assert tmux_safe_name(".claude-fix") == "_claude-fix"

    def test_slashes_are_preserved(self):
        # `project/branch` is a legal tmux name and IS the convention cmd_new
        # builds for worktree sessions — collapsing it would rename every one.
        assert tmux_safe_name("myapp/feat") == "myapp/feat"

    def test_idempotent(self):
        once = tmux_safe_name("dotdev.dev/v2.0")
        assert tmux_safe_name(once) == once

    def test_leaves_an_already_legal_name_alone(self):
        assert tmux_safe_name("myapp-fix-bug") == "myapp-fix-bug"


class TestWorktreeSessionName:
    @pytest.mark.parametrize("project,expected", [
        (".claude", "_claude-fix-bug"),
        ("dotdev.dev", "dotdev_dev-fix-bug"),
        ("jordangarygerard.com", "jordangarygerard_com-fix-bug"),
        ("myapp", "myapp-fix-bug"),
    ])
    def test_project_half_is_sanitized_too(self, tmp_path, project, expected):
        assert worktree_session_name(tmp_path / project, "fix-bug") == expected

    @pytest.mark.parametrize("project", [".claude", "dotdev.dev", "myapp"])
    @pytest.mark.parametrize("name", ["fix-bug", "feat/ui: v2.0", "///"])
    def test_output_is_a_fixed_point_of_the_creation_sanitizer(
        self, tmp_path, project, name,
    ):
        """THE invariant #868 broke.

        ``cmd_worktree`` derives this name and hands it to ``cmd_new``, which
        runs :func:`tmux_safe_name` before creating the tmux session. If the
        derivation isn't already a fixed point of that mapping, the name we
        record and later resolve is not the name that exists.
        """
        derived = worktree_session_name(tmp_path / project, name)
        assert tmux_safe_name(derived) == derived

    def test_branch_half_sanitizer_is_unchanged(self, tmp_path):
        # safe_worktree_name still owns the branch token (it also names the
        # worktree DIRECTORY) — the dot mapping is layered on top, not merged.
        assert safe_worktree_name("feat/ui: v2.0") == "feat-ui-v2-0"
        assert worktree_session_name(tmp_path / "myapp", "feat/ui: v2.0") == "myapp-feat-ui-v2-0"


class TestTeardownSessionNote:
    def test_killed(self):
        note = teardown_session_note({"session": "myapp-fix", "killed": True})
        assert note == " (killed live session)"

    def test_pane_topology_session_deliberately_left_alone(self):
        note = teardown_session_note(
            {"session": "orchestrator", "killed": False, "session_kill_skipped": True})
        assert "left running" in note and "orchestrator" in note

    def test_no_match_is_stated_out_loud(self):
        """The #868 reporting bug: this used to render as nothing at all."""
        note = teardown_session_note(
            {"session": ".claude-fix", "killed": False, "session_kill_skipped": False})
        assert "NO live tmux session named '.claude-fix'" in note
        assert "nothing killed" in note
