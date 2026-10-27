"""Tests for build_agent_command — the ONE flag-builder, keyed on posture (#729)."""

import stat
from pathlib import Path

import pytest

from agentwire.roles import RoleConfig


class TestBuildAgentCommand:
    @pytest.fixture(autouse=True)
    def _prompts_dir(self, tmp_path, monkeypatch):
        """Keep role prompts out of the real ~/.agentwire/role-prompts.

        Patching CONFIG_DIR (not a role-prompts path directly) is the whole
        point of #884's `role_prompts_dir()`: the store now FOLLOWS this
        repo's one isolation seam instead of being computed at import.
        """
        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
        self.prompts_dir = tmp_path / "role-prompts"

    def _build(self, posture, roles=None, model=None, resume_session_id=None):
        from agentwire.__main__ import build_agent_command

        return build_agent_command(
            posture, roles=roles, model=model, resume_session_id=resume_session_id
        )

    def test_bypass(self):
        cmd = self._build("bypass")
        assert "claude" in cmd.command
        assert "--dangerously-skip-permissions" in cmd.command

    def test_resume_inserts_flags_after_claude(self):
        """The conversation flags now come from a shell array the prelude
        fills in at launch (#901), so they still land first — just resolved
        at run time rather than baked in. See test_launch_line_reentry.py."""
        cmd = self._build("bypass", resume_session_id="abc-123")
        assert '--resume "abc-123" --fork-session' in cmd.command
        assert 'claude "${aw_flags[@]}"' in cmd.command
        # posture flags still present alongside resume
        assert "--dangerously-skip-permissions" in cmd.command


class TestConversationIdentity:
    """agentwire mints the conversation UUID rather than discovering it (#871)."""

    @pytest.fixture(autouse=True)
    def _prompts_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
        self.prompts_dir = tmp_path / "role-prompts"

    def _build(self, posture="bypass", roles=None, resume_session_id=None):
        from agentwire.__main__ import build_agent_command

        return build_agent_command(posture, roles=roles, resume_session_id=resume_session_id)

    def test_role_prompt_is_owner_only(self):
        """A permissions REGRESSION is what this pins: the tempfile this store
        replaced was 0600 and transient; this one is permanent and holds
        complete system-prompt text. It sits next to `~/.agentwire/.env`
        (chmod 600 by convention) and must match that posture."""
        roles = [RoleConfig(name="test", instructions="secret-ish context")]
        cmd = self._build(roles=roles)
        path = Path(cmd.role_prompt_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(self.prompts_dir.stat().st_mode) == 0o700
