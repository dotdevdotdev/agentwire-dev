"""Unit tests for tmux-legal session-name derivation (#868, #878).

tmux rewrites its address separators in session names, so every creation path
maps them to ``_``. Resolution used the project directory's name RAW, so for
any project whose directory contains a dot (``~/.claude``,
``~/projects/dotdev.dev``) teardown derived a name no session could ever have —
matched nothing, and said it had removed one anyway. Same class as #855 on the
session-name axis. ``:`` is the second separator and had the identical hole
(#878); ``TestTmuxRewriteSet`` pins the complete set so this stops recurring.
"""


from agentwire.worktree import (
    teardown_session_note,
    tmux_safe_name,
)


class TestTmuxSafeName:


    def test_both_separators_in_one_name(self):
        assert tmux_safe_name("a.b:c.d") == "a_b_c_d"







class TestTeardownSessionNote:


    def test_no_match_is_stated_out_loud(self):
        """The #868 reporting bug: this used to render as nothing at all."""
        note = teardown_session_note(
            {"session": ".claude-fix", "killed": False, "session_kill_skipped": False})
        assert "NO live tmux session named '.claude-fix'" in note
        assert "nothing killed" in note
