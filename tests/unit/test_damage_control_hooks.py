"""Tests for agentwire/hooks/damage-control/ — Bash/Edit/Write tool hooks.

These hooks are PEP 723 inline-deps scripts invoked by Claude Code. They live
under hyphenated filenames (`bash-tool-damage-control.py`), so they're loaded
via importlib instead of normal imports. Each hook exposes a top-level
`check_command` (bash) or `check_path` (edit/write) function plus a `main()`
that reads JSON from stdin.

We test the pure decision functions directly (fast, deterministic) and a
representative subprocess flow per hook (covers the stdin/exit-code surface).
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control"


def _load_hook(filename: str):
    """Load a hyphenated hook script as an importable module.

    The script's `audit_logger` import resolves via sys.path injection so the
    fallback no-op log_* functions are not needed.
    """
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        path = HOOKS_DIR / filename
        spec = importlib.util.spec_from_file_location(
            filename.replace(".py", "").replace("-", "_"), path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


@pytest.fixture(scope="module")
def bash_hook():
    return _load_hook("bash-tool-damage-control.py")


@pytest.fixture(scope="module")
def edit_hook():
    return _load_hook("edit-tool-damage-control.py")


@pytest.fixture(scope="module")
def write_hook():
    return _load_hook("write-tool-damage-control.py")


# ---------------------------------------------------------------------------
# bash-tool-damage-control.py: check_command decision matrix
# ---------------------------------------------------------------------------


class TestBashHookCheckCommand:
    @staticmethod
    def _config(**overrides):
        """Minimal config; tests override the relevant key."""
        return {
            "bashToolPatterns": [],
            "zeroAccessPaths": [],
            "readOnlyPaths": [],
            "noDeletePaths": [],
            "allowedPaths": [],
            **overrides,
        }

    def test_no_patterns_allows(self, bash_hook):
        blocked, ask, reason = bash_hook.check_command("echo hello", self._config())
        assert (blocked, ask) == (False, False)

    def test_hard_block_pattern(self, bash_hook):
        cfg = self._config(bashToolPatterns=[
            {"pattern": r"\brm\s+-rf\s+/", "reason": "rm -rf /"},
        ])
        blocked, ask, reason = bash_hook.check_command("rm -rf /", cfg)
        assert blocked is True
        assert ask is False
        assert "rm -rf /" in reason

    def test_ask_pattern(self, bash_hook):
        cfg = self._config(bashToolPatterns=[
            {"pattern": r"\bgit\s+push\b", "reason": "git push", "ask": True},
        ])
        blocked, ask, reason = bash_hook.check_command("git push origin main", cfg)
        assert (blocked, ask) == (False, True)
        assert reason == "git push"

    def test_bypassable_pattern_blocks_without_allowlist(self, bash_hook):
        cfg = self._config(bashToolPatterns=[
            {"pattern": r"\brm\s+", "reason": "rm deletion", "bypassable": True},
        ])
        blocked, ask, _ = bash_hook.check_command("rm /etc/passwd", cfg)
        assert (blocked, ask) == (True, False)

    def test_bypassable_pattern_allowed_via_allowlist(self, bash_hook):
        cfg = self._config(
            bashToolPatterns=[
                {"pattern": r"\brm\s+", "reason": "rm deletion", "bypassable": True},
            ],
            allowedPaths=[{"path": "*/dist/*", "allow": "all"}],
        )
        blocked, ask, _ = bash_hook.check_command(
            "rm /home/user/proj/dist/old.whl", cfg
        )
        assert (blocked, ask) == (False, False)

    def test_zero_access_path_blocks(self, bash_hook):
        cfg = self._config(zeroAccessPaths=["/etc/secret"])
        blocked, _, reason = bash_hook.check_command("cat /etc/secret", cfg)
        assert blocked is True
        assert "zero-access" in reason

    def test_zero_access_method_call_skipped(self, bash_hook):
        """`module.py(...)` should not match `*.py` zero-access pattern."""
        cfg = self._config(zeroAccessPaths=["*.py"])
        blocked, _, _ = bash_hook.check_command(
            "python -c 'import module.py()'", cfg
        )
        assert blocked is False

    def test_invalid_regex_skipped_not_crashed(self, bash_hook):
        cfg = self._config(bashToolPatterns=[
            {"pattern": r"[unclosed", "reason": "bad pattern"},
            {"pattern": r"\bdanger\b", "reason": "real danger"},
        ])
        # Bad regex skipped; real one fires.
        blocked, _, reason = bash_hook.check_command("danger ahead", cfg)
        assert blocked is True
        assert reason == "Blocked: real danger"

    def test_read_only_path_caught_via_redirect(self, bash_hook):
        cfg = self._config(readOnlyPaths=["/etc/"])
        blocked, _, _ = bash_hook.check_command("echo data > /etc/foo", cfg)
        assert blocked is True

    def test_no_delete_path_blocks_rm(self, bash_hook):
        cfg = self._config(noDeletePaths=[".git/"])
        blocked, _, _ = bash_hook.check_command("rm .git/HEAD", cfg)
        assert blocked is True

    def test_no_delete_path_allows_cat(self, bash_hook):
        """no-delete only blocks deletes, not reads."""
        cfg = self._config(noDeletePaths=[".git/"])
        blocked, ask, _ = bash_hook.check_command("cat .git/HEAD", cfg)
        assert (blocked, ask) == (False, False)


# ---------------------------------------------------------------------------
# bash-tool-damage-control.py: helper functions
# ---------------------------------------------------------------------------


class TestBashHookHelpers:
    def test_glob_to_regex_extension(self, bash_hook):
        regex = bash_hook.glob_to_regex("*.py")
        # Should match "rm test.py" but not "module.python"
        import re
        assert re.search(regex, "rm test.py")
        assert not re.search(regex, "module.python")

    def test_is_glob_pattern_detection(self, bash_hook):
        assert bash_hook.is_glob_pattern("*.py") is True
        assert bash_hook.is_glob_pattern("file?.txt") is True
        assert bash_hook.is_glob_pattern("[abc]") is True
        assert bash_hook.is_glob_pattern("plain.txt") is False

    @pytest.mark.parametrize("reason,expected", [
        ("rm anything", "delete"),
        ("trash this", "delete"),
        ("rmdir empty", "delete"),
        ("chmod -x", "chmod"),
        ("chown user", "chmod"),
        ("mv operation", "move"),
        ("write to disk", "write"),
        ("", "write"),
    ])
    def test_infer_operation_from_reason(self, bash_hook, reason, expected):
        assert bash_hook._infer_operation_from_reason(reason) == expected

    def test_extract_paths_from_command(self, bash_hook):
        paths = bash_hook._extract_paths_from_command("cat /etc/passwd /tmp/foo")
        assert "/etc/passwd" in paths
        assert "/tmp/foo" in paths


# ---------------------------------------------------------------------------
# bash hook: subprocess end-to-end (stdin → exit code → stderr)
# ---------------------------------------------------------------------------


class TestBashHookSubprocess:
    """Test the full main() flow: read JSON from stdin, exit 0/1/2."""

    HOOK = HOOKS_DIR / "bash-tool-damage-control.py"

    def _run(self, payload, env_extra=None):
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": "/tmp",
            **(env_extra or {}),
        }
        # Use system python directly (skip uv-run startup) — the script's
        # imports (yaml, audit_logger) are present in the venv
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        return proc

    def test_non_bash_tool_passes(self):
        # Edit tool input must be ignored (this hook only checks Bash)
        proc = self._run({"tool_name": "Edit", "tool_input": {"file_path": "/x"}})
        assert proc.returncode == 0

    def test_empty_command_passes(self):
        proc = self._run({"tool_name": "Bash", "tool_input": {"command": ""}})
        assert proc.returncode == 0

    def test_invalid_json_exits_1(self):
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input="not json {{{",
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
            timeout=10,
        )
        assert proc.returncode == 1
        assert "Invalid JSON" in proc.stderr or "Error" in proc.stderr


# ---------------------------------------------------------------------------
# edit-tool-damage-control.py & write-tool-damage-control.py
# ---------------------------------------------------------------------------


class TestEditWriteHookStructure:
    """Sanity checks: hooks are loadable and expose the expected entry points."""

    def test_edit_hook_loads(self, edit_hook):
        # Should expose the same helper API as bash hook
        assert hasattr(edit_hook, "main")

    def test_write_hook_loads(self, write_hook):
        assert hasattr(write_hook, "main")


class TestEditHookSubprocess:
    HOOK = HOOKS_DIR / "edit-tool-damage-control.py"

    def _run(self, payload):
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            timeout=10,
        )
        return proc

    def test_non_edit_tool_passes(self):
        # Bash tool input must be ignored (this hook only checks Edit/MultiEdit)
        proc = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert proc.returncode == 0

    def test_missing_file_path_passes(self):
        proc = self._run({"tool_name": "Edit", "tool_input": {}})
        assert proc.returncode == 0


class TestWriteHookSubprocess:
    HOOK = HOOKS_DIR / "write-tool-damage-control.py"

    def _run(self, payload):
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            timeout=10,
        )
        return proc

    def test_non_write_tool_passes(self):
        proc = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert proc.returncode == 0

    def test_missing_file_path_passes(self):
        proc = self._run({"tool_name": "Write", "tool_input": {}})
        assert proc.returncode == 0


# ---------------------------------------------------------------------------
# audit_logger.py
# ---------------------------------------------------------------------------


class TestAuditLogger:
    @pytest.fixture
    def audit_module(self, tmp_path, monkeypatch):
        """Load audit_logger with AGENTWIRE_DIR pointing at tmp_path."""
        monkeypatch.setenv("AGENTWIRE_DIR", str(tmp_path / ".agentwire"))
        spec = importlib.util.spec_from_file_location(
            "audit_logger_test", HOOKS_DIR / "audit_logger.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_log_blocked_writes_jsonl(self, audit_module):
        audit_module.log_blocked("Bash", "rm -rf /", "rm -rf root")
        log_file = audit_module.get_log_file()
        assert log_file.exists()
        entry = json.loads(log_file.read_text().strip())
        assert entry["decision"] == "blocked"
        assert entry["tool"] == "Bash"
        assert entry["blocked_by"] == "rm -rf root"
        assert entry["command"] == "rm -rf /"

    def test_log_asked_writes_jsonl(self, audit_module):
        audit_module.log_asked("Bash", "git push", "destructive op")
        entry = json.loads(audit_module.get_log_file().read_text().strip())
        assert entry["decision"] == "asked"
        assert entry["blocked_by"] == "destructive op"

    def test_log_allowed_user_approved_flag(self, audit_module):
        audit_module.log_allowed("Bash", "git commit", user_approved=True)
        entry = json.loads(audit_module.get_log_file().read_text().strip())
        assert entry["decision"] == "allowed"
        assert entry["user_approved"] is True

    def test_log_allowed_no_user_approval_omits_flag(self, audit_module):
        """user_approved=False stores None to keep entries lean."""
        audit_module.log_allowed("Bash", "ls", user_approved=False)
        entry = json.loads(audit_module.get_log_file().read_text().strip())
        assert entry["user_approved"] is None

    def test_session_context_from_env(self, audit_module, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_SESSION_ID", "sess-123")
        monkeypatch.setenv("AGENTWIRE_AGENT_ID", "worker-1")
        ctx = audit_module.get_session_context()
        assert ctx == {"session_id": "sess-123", "agent_id": "worker-1"}

    def test_session_context_defaults(self, audit_module, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_SESSION_ID", raising=False)
        monkeypatch.delenv("AGENTWIRE_AGENT_ID", raising=False)
        ctx = audit_module.get_session_context()
        assert ctx == {"session_id": "unknown", "agent_id": "main"}

    def test_log_dir_creates_path(self, audit_module, tmp_path):
        log_dir = audit_module.get_log_dir()
        assert log_dir.exists()
        assert log_dir.is_dir()
        # AGENTWIRE_DIR fixture pointed it at tmp_path/.agentwire
        assert str(tmp_path) in str(log_dir)

    def test_multiple_entries_append(self, audit_module):
        audit_module.log_blocked("Bash", "cmd1", "r1")
        audit_module.log_asked("Bash", "cmd2", "r2")
        audit_module.log_allowed("Bash", "cmd3")
        lines = audit_module.get_log_file().read_text().strip().split("\n")
        assert len(lines) == 3
        decisions = [json.loads(line)["decision"] for line in lines]
        assert decisions == ["blocked", "asked", "allowed"]
