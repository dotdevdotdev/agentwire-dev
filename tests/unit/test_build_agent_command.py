"""Tests for build_agent_command — the ONE flag-builder, keyed on posture (#729)."""

import os
import stat
import uuid
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
        return build_agent_command(posture, roles=roles, model=model,
                                   resume_session_id=resume_session_id)

    def test_bare_empty_command(self):
        cmd = self._build("bare")
        assert cmd.command == ""
        assert cmd.role_prompt_path is None
        # No claude process means no conversation to identify (#871).
        assert cmd.conversation_id is None
        assert cmd.posture == "bare"

    def test_bypass(self):
        cmd = self._build("bypass")
        assert "claude" in cmd.command
        assert "--dangerously-skip-permissions" in cmd.command

    def test_prompted(self):
        cmd = self._build("prompted")
        assert "claude" in cmd.command
        assert "--dangerously-skip-permissions" not in cmd.command
        assert "--tools" not in cmd.command

    def test_restricted_rejected(self):
        # restricted/readonly were dropped (#729) — no longer valid postures
        import pytest

        from agentwire.project_config import resolve_posture
        with pytest.raises(ValueError):
            resolve_posture("restricted")
        with pytest.raises(ValueError):
            resolve_posture("readonly")

    def test_auto(self):
        cmd = self._build("auto")
        assert "--enable-auto-mode" in cmd.command
        assert "--permission-mode" in cmd.command and "auto" in cmd.command
        # auto injects the core tool-allows so the classifier is bypassed for the safe set
        assert "--allowedTools" in cmd.command

    def test_resume_inserts_flags_after_claude(self):
        """The conversation flags now come from a shell array the prelude
        fills in at launch (#901), so they still land first — just resolved
        at run time rather than baked in. See test_launch_line_reentry.py."""
        cmd = self._build("bypass", resume_session_id="abc-123")
        assert '--resume "abc-123" --fork-session' in cmd.command
        assert 'claude "${aw_flags[@]}"' in cmd.command
        # posture flags still present alongside resume
        assert "--dangerously-skip-permissions" in cmd.command

    def test_resume_carries_auto_tool_allows(self):
        # The old resume path dropped auto's tool-allows; the unified builder keeps them.
        fresh = self._build("auto")
        resumed = self._build("auto", resume_session_id="xyz")
        assert "--allowedTools" in resumed.command
        assert "--enable-auto-mode" in resumed.command
        assert "--enable-auto-mode" in fresh.command

    def test_with_model_override(self):
        cmd = self._build("bypass", model="haiku")
        assert "--model haiku" in cmd.command

    def test_with_roles_tools(self):
        roles = [RoleConfig(name="test", tools=["Bash", "Read"])]
        cmd = self._build("bypass", roles=roles)
        assert "--tools" in cmd.command
        assert "Bash" in cmd.command

    def test_with_roles_instructions(self):
        roles = [RoleConfig(name="test", instructions="Be helpful")]
        cmd = self._build("bypass", roles=roles)
        assert "--append-system-prompt" in cmd.command
        assert cmd.role_prompt_path is not None
        assert Path(cmd.role_prompt_path).read_text() == "Be helpful"

    def test_roles_apply_on_every_posture(self):
        """Role tools/instructions apply unconditionally now — no tool-locking posture."""
        roles = [RoleConfig(name="test", tools=["Read"], instructions="Hello")]
        for posture in ("bypass", "prompted", "auto"):
            cmd = self._build(posture, roles=roles)
            assert "--append-system-prompt" in cmd.command
            assert "--tools" in cmd.command


class TestConversationIdentity:
    """agentwire mints the conversation UUID rather than discovering it (#871)."""

    @pytest.fixture(autouse=True)
    def _prompts_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
        self.prompts_dir = tmp_path / "role-prompts"

    def _build(self, posture="bypass", roles=None, resume_session_id=None):
        from agentwire.__main__ import build_agent_command
        return build_agent_command(posture, roles=roles,
                                   resume_session_id=resume_session_id)

    def test_session_id_flag_carries_a_valid_uuid(self):
        cmd = self._build()
        assert f'--session-id "{cmd.conversation_id}"' in cmd.command
        # `claude --session-id` rejects anything that isn't a real UUID.
        assert uuid.UUID(cmd.conversation_id)

    def test_the_id_is_not_passed_unconditionally(self):
        """`--session-id` is single-use, and the line it rides in is stored to
        be RE-RUN — so the flag is chosen by the shell against the transcript,
        never fixed at build time (#901)."""
        cmd = self._build()
        assert f"claude --session-id {cmd.conversation_id}" not in cmd.command
        assert f'--resume "{cmd.conversation_id}"' in cmd.command

    def test_every_build_mints_a_fresh_id(self):
        """`--session-id` HARD-ERRORS on a collision within the launch cwd
        ("Session ID <id> is already in use."), so reuse would refuse to boot.
        """
        ids = {self._build().conversation_id for _ in range(20)}
        assert len(ids) == 20

    def test_resume_forks_into_an_id_we_chose(self):
        """`--resume <old> --fork-session --session-id <new>` composes, so the
        forked conversation is recorded rather than guessed."""
        cmd = self._build(resume_session_id="old-conversation")
        assert (
            f'--resume "old-conversation" --fork-session '
            f'--session-id "{cmd.conversation_id}"'
        ) in cmd.command
        assert cmd.resumed_from == "old-conversation"
        assert cmd.conversation_id != "old-conversation"

    def test_posture_and_role_names_ride_along(self):
        """Recorded to REGENERATE the system prompt, not merely reference it."""
        roles = [RoleConfig(name="worker", instructions="A"),
                 RoleConfig(name="soul", instructions="B")]
        cmd = self._build("auto", roles=roles)
        assert cmd.posture == "auto"
        assert cmd.roles == ["worker", "soul"]

    def test_role_prompt_is_durable_and_keyed_by_conversation(self):
        """NOT /var/folders: macOS GCs that, and the launch line reads the file
        BY PATH, so a GC'd prompt relaunches the session with an empty one."""
        roles = [RoleConfig(name="test", instructions="Be a worker")]
        cmd = self._build(roles=roles)
        path = Path(cmd.role_prompt_path)
        assert path.parent == self.prompts_dir
        assert path.name == f"{cmd.conversation_id}.txt"
        assert path.read_text() == "Be a worker"

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

    def test_permissive_umask_cannot_widen_the_mode(self):
        """umask only CLEARS bits, so a create-time mode is a request, not a
        guarantee — the writer must state the mode outright."""
        old = os.umask(0o000)
        try:
            roles = [RoleConfig(name="test", instructions="x")]
            cmd = self._build(roles=roles)
        finally:
            os.umask(old)
        assert stat.S_IMODE(Path(cmd.role_prompt_path).stat().st_mode) == 0o600
        assert stat.S_IMODE(self.prompts_dir.stat().st_mode) == 0o700

    def test_pre_existing_world_readable_paths_heal_on_write(self):
        """Directories written before this rule are already on disk at 0755,
        and neither umask nor mkdir/open mode touches an existing path — so
        the fix has to repair them rather than only apply going forward."""
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.prompts_dir, 0o755)
        stale = self.prompts_dir / "stale.txt"
        stale.write_text("old")
        os.chmod(stale, 0o644)

        from agentwire.core import write_role_prompt
        healed = write_role_prompt("stale", "new")

        assert healed == stale
        assert stale.read_text() == "new"
        assert stat.S_IMODE(stale.stat().st_mode) == 0o600
        assert stat.S_IMODE(self.prompts_dir.stat().st_mode) == 0o700

    def test_append_system_prompt_stays_last(self):
        """Multiline content can break any flag that follows it."""
        roles = [RoleConfig(name="test", tools=["Read"], instructions="line1\nline2")]
        cmd = self._build(roles=roles)
        assert cmd.command.index("--session-id") < cmd.command.index("--append-system-prompt")
        assert cmd.command.endswith(f'--append-system-prompt "$(<{cmd.role_prompt_path})"')


class TestMirrorRolePromptRemote:
    """The remote half of the same store — same durability, same posture."""

    @pytest.fixture(autouse=True)
    def _wire(self, tmp_path, monkeypatch):
        from agentwire import core

        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
        self.sent = []

        def fake_run_remote(machine_id, command):
            self.sent.append(command)
            import subprocess
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(core, "_run_remote", fake_run_remote)

    def _agent(self, instructions="Be a worker"):
        from agentwire.core import build_agent_command
        from agentwire.roles import RoleConfig
        return build_agent_command("bypass", [RoleConfig(name="r", instructions=instructions)])

    def test_repoints_launch_line_and_record_at_the_remote_copy(self):
        from agentwire.core import mirror_role_prompt_remote

        agent = self._agent()
        local = agent.role_prompt_path
        rewritten = mirror_role_prompt_remote(agent, "box", agent.command)

        remote = f"$HOME/.agentwire/role-prompts/{agent.conversation_id}.txt"
        assert local not in rewritten
        assert remote in rewritten
        # The RECORD must name where the prompt actually lives for that session.
        assert agent.role_prompt_path == remote

    def test_remote_write_is_owner_only(self):
        """`cat > file` on the far side inherits the remote's umask and leaves
        an existing file's mode alone — both have to be stated explicitly."""
        from agentwire.core import mirror_role_prompt_remote

        agent = self._agent()
        mirror_role_prompt_remote(agent, "box", agent.command)

        cmd = self.sent[0]
        assert "umask 077" in cmd
        assert 'chmod 700 "$HOME/.agentwire/role-prompts"' in cmd
        assert f'chmod 600 "{agent.role_prompt_path}"' in cmd
        # /tmp was the old destination and is not durable.
        assert "/tmp" not in cmd

    def test_content_survives_the_heredoc(self):
        from agentwire.core import mirror_role_prompt_remote

        agent = self._agent(instructions="line1\nline2 'quoted' $VAR")
        mirror_role_prompt_remote(agent, "box", agent.command)
        body = self.sent[0].split("<< 'AGENTWIRE_EOF'\n", 1)[1].split("\nAGENTWIRE_EOF")[0]
        # Quoted delimiter => no expansion; $VAR must arrive literally.
        assert body == "line1\nline2 'quoted' $VAR"

    def test_failed_write_leaves_the_command_and_record_untouched(self, monkeypatch):
        import subprocess

        from agentwire import core

        monkeypatch.setattr(
            core, "_run_remote",
            lambda m, c: subprocess.CompletedProcess([], 1, "", "boom"))
        agent = self._agent()
        original = agent.command
        assert core.mirror_role_prompt_remote(agent, "box", original) == original
        assert agent.role_prompt_path.startswith(str(core.role_prompts_dir()))


@pytest.mark.real_agentwire_home
def test_default_prompt_dir_is_under_agentwire_config():
    """The whole point of #871's prompt move. Deliberately module-level: the
    classes above redirect CONFIG_DIR to a tmp dir that pytest itself happens
    to put under /var/folders, which would make this vacuous.

    Same reason it opts out of the #893 home redirect, which relocates the
    config dir into precisely such a directory: this test asserts on the REAL
    deployment path, and reads only."""
    from agentwire.core import CONFIG_DIR, role_prompts_dir
    assert role_prompts_dir().parent == CONFIG_DIR
    assert "/var/folders" not in str(role_prompts_dir())


class TestSessionEnvInjection:
    def test_build_tmux_env_flags_empty(self):
        from agentwire.__main__ import _build_tmux_env_flags
        assert _build_tmux_env_flags({}) == []

    def test_build_tmux_env_flags_pairs(self):
        from agentwire.__main__ import _build_tmux_env_flags
        flags = _build_tmux_env_flags({"SVC_API_KEY": "abc", "FOO": "bar"})
        # Each var becomes two list entries: "-e" and "K=V"
        assert flags.count("-e") == 2
        assert "SVC_API_KEY=abc" in flags
        assert "FOO=bar" in flags

    def test_build_tmux_env_flags_shell_empty(self):
        from agentwire.__main__ import _build_tmux_env_flags_shell
        assert _build_tmux_env_flags_shell({}) == ""

    def test_build_tmux_env_flags_shell_quoted(self):
        from agentwire.__main__ import _build_tmux_env_flags_shell
        frag = _build_tmux_env_flags_shell({"SVC_API_KEY": "abc 123"})
        # Trailing space so it splices into the middle of a command string
        assert frag.endswith(" ")
        assert "-e" in frag
        # Value with spaces must be shell-quoted as a single -e argument
        assert "'SVC_API_KEY=abc 123'" in frag

    def test_build_tmux_env_flags_shell_multiple(self):
        from agentwire.__main__ import _build_tmux_env_flags_shell
        frag = _build_tmux_env_flags_shell({"A": "1", "B": "2"})
        assert frag.count("-e") == 2
        assert "A=1" in frag
        assert "B=2" in frag


class TestParseEnvArgs:
    def test_none_returns_empty(self):
        from agentwire.__main__ import parse_env_args
        assert parse_env_args(None) == {}
        assert parse_env_args([]) == {}

    def test_single_pair(self):
        from agentwire.__main__ import parse_env_args
        assert parse_env_args(["FOO=bar"]) == {"FOO": "bar"}

    def test_multiple_pairs(self):
        from agentwire.__main__ import parse_env_args
        result = parse_env_args(["A=1", "B=2", "C=3"])
        assert result == {"A": "1", "B": "2", "C": "3"}

    def test_value_with_equals_sign_preserved(self):
        from agentwire.__main__ import parse_env_args
        # Values can contain `=` (e.g. base64 payloads) — only split on the first.
        assert parse_env_args(["TOKEN=abc=def=xyz"]) == {"TOKEN": "abc=def=xyz"}

    def test_empty_value_allowed(self):
        from agentwire.__main__ import parse_env_args
        assert parse_env_args(["DEBUG="]) == {"DEBUG": ""}

    def test_missing_equals_exits(self):
        from agentwire.__main__ import parse_env_args
        with pytest.raises(SystemExit):
            parse_env_args(["BROKEN"])

    def test_empty_key_exits(self):
        from agentwire.__main__ import parse_env_args
        with pytest.raises(SystemExit):
            parse_env_args(["=value"])

    def test_later_value_wins(self):
        from agentwire.__main__ import parse_env_args
        # If the same key appears twice, last one wins (standard dict semantics).
        assert parse_env_args(["K=1", "K=2"]) == {"K": "2"}
