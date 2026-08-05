"""Unit tests for tmux-legal session-name derivation (#868, #878).

tmux rewrites its address separators in session names, so every creation path
maps them to ``_``. Resolution used the project directory's name RAW, so for
any project whose directory contains a dot (``~/.claude``,
``~/projects/dotdev.dev``) teardown derived a name no session could ever have —
matched nothing, and said it had removed one anyway. Same class as #855 on the
session-name axis. ``:`` is the second separator and had the identical hole
(#878); ``TestTmuxRewriteSet`` pins the complete set so this stops recurring.
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

    def test_colons_become_underscores(self):
        # `:` is tmux's session:window separator and gets the SAME treatment as
        # `.` — `-s proj:x` yields the session `proj_x` (#878).
        assert tmux_safe_name("proj:x-fork-1") == "proj_x-fork-1"

    def test_both_separators_in_one_name(self):
        assert tmux_safe_name("a.b:c.d") == "a_b_c_d"

    def test_slashes_are_preserved(self):
        # `project/branch` is a legal tmux name and IS the convention cmd_new
        # builds for worktree sessions — collapsing it would rename every one.
        assert tmux_safe_name("myapp/feat") == "myapp/feat"

    def test_idempotent(self):
        once = tmux_safe_name("dotdev.dev/v2.0:rc1")
        assert tmux_safe_name(once) == once

    def test_leaves_an_already_legal_name_alone(self):
        assert tmux_safe_name("myapp-fix-bug") == "myapp-fix-bug"


class TestTmuxRewriteSet:
    """Pin the COMPLETE set of characters tmux rewrites 1:1 in a session name.

    #865 → #868 → #870 → #878 is four rounds of the same class, three of them
    because nobody established the set. It was established by sweeping every
    printable ASCII character through ``tmux new-session`` on an isolated
    socket (tmux 3.5a); ``tests/integration/test_tmux_name_rewrite.py`` re-runs
    that sweep against the real binary. These are the cheap always-run pins.
    """

    #: Characters tmux substitutes 1:1 with ``_`` — its two address separators.
    SUBSTITUTED = {".", ":"}

    #: Characters tmux transforms by VIS-ESCAPING (expanding to a longer
    #: string: ``\\`` → ``\\\\``, tab → ``\\t``, ``\x01`` → ``\\001``). A
    #: different kind of transformation — not expressible as a ``replace`` —
    #: and deliberately NOT mirrored: see tmux_safe_name's docstring.
    VIS_ESCAPED = {"\\", "\t", "\n", "\r", "\x01", "\x7f"}

    @pytest.mark.parametrize("ch", sorted(SUBSTITUTED))
    def test_every_substituted_char_is_mapped(self, ch):
        assert tmux_safe_name(f"a{ch}b") == "a_b"

    @pytest.mark.parametrize("ch", sorted(
        set(map(chr, range(32, 127))) - SUBSTITUTED - VIS_ESCAPED
    ))
    def test_no_other_printable_ascii_is_touched(self, ch):
        """The other direction: over-sanitizing renames sessions that were fine.

        `/` is the load-bearing one — `project/branch` is the convention every
        worktree session uses — but the guarantee is general.
        """
        name = f"a{ch}b"
        assert tmux_safe_name(name) == name

    def test_utf8_passes_through(self):
        assert tmux_safe_name("café-→-fix") == "café-→-fix"

    @pytest.mark.parametrize("ch", sorted(VIS_ESCAPED))
    def test_vis_escaped_chars_are_a_documented_non_goal(self, ch):
        """tmux mangles these, and tmux_safe_name deliberately does NOT mirror it.

        Mirroring would mean reimplementing tmux's ``vis`` escaping, and these
        are unreachable from what actually feeds session names (project dir
        names, operator/agent-supplied names). Pinned so the gap is a recorded
        decision rather than an oversight — if one ever becomes reachable, this
        test is where the argument lives.
        """
        name = f"a{ch}b"
        assert tmux_safe_name(name) == name  # untouched — NOT a fixed point of tmux


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
