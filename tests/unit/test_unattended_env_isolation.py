"""Unattended-marker isolation (#674).

The scheduler stamps AGENTWIRE_UNATTENDED=1 on the dispatch subprocess. If
that var stays in the CLI's os.environ, a tmux client that boots the shared
tmux server leaks it into the server process — after which EVERY session the
server spawns (interactive ones included) is falsely treated as unattended.

core.py therefore pops the marker out of os.environ at import into a captured
module-level dict; propagation into new sessions happens only via the
deliberate `tmux new-session -e` path.
"""

import os
import subprocess
import sys

from agentwire import core


class TestCaptureUnattendedEnv:
    def test_pops_marker_out_of_environ(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
        monkeypatch.setenv("AGENTWIRE_UNATTENDED_ALLOW", "task.rule-a")
        captured = core._capture_unattended_env()
        assert captured == {
            "AGENTWIRE_UNATTENDED": "1",
            "AGENTWIRE_UNATTENDED_ALLOW": "task.rule-a",
        }
        assert "AGENTWIRE_UNATTENDED" not in os.environ
        assert "AGENTWIRE_UNATTENDED_ALLOW" not in os.environ

    def test_empty_when_not_dispatched_unattended(self, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)
        monkeypatch.delenv("AGENTWIRE_UNATTENDED_ALLOW", raising=False)
        assert core._capture_unattended_env() == {}


class TestWithUnattendedEnv:
    def test_reads_captured_copy_not_environ(self, monkeypatch):
        # Marker in os.environ alone (the leak vector) must NOT propagate...
        monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
        monkeypatch.setattr(core, "_UNATTENDED_ENV", {})
        assert "AGENTWIRE_UNATTENDED" not in core._with_unattended_env({})

        # ...while the captured copy (a real scheduler dispatch) does.
        monkeypatch.delenv("AGENTWIRE_UNATTENDED")
        monkeypatch.setattr(
            core, "_UNATTENDED_ENV",
            {"AGENTWIRE_UNATTENDED": "1", "AGENTWIRE_UNATTENDED_ALLOW": "x.y"},
        )
        merged = core._with_unattended_env({"FOO": "bar"})
        assert merged["AGENTWIRE_UNATTENDED"] == "1"
        assert merged["AGENTWIRE_UNATTENDED_ALLOW"] == "x.y"
        assert merged["FOO"] == "bar"

    def test_explicit_env_wins_over_captured(self, monkeypatch):
        monkeypatch.setattr(core, "_UNATTENDED_ENV", {"AGENTWIRE_UNATTENDED": "1"})
        merged = core._with_unattended_env({"AGENTWIRE_UNATTENDED": "0"})
        assert merged["AGENTWIRE_UNATTENDED"] == "0"

    def test_tmux_env_flags_carry_captured_marker(self, monkeypatch):
        monkeypatch.setattr(core, "_UNATTENDED_ENV", {"AGENTWIRE_UNATTENDED": "1"})
        flags = core._build_tmux_env_flags({})
        assert flags == ["-e", "AGENTWIRE_UNATTENDED=1"]


class TestImportScrubsEnviron:
    def test_fresh_process_import_scrubs_marker(self):
        """End to end: a process started with the marker (like a scheduler's
        `agentwire ensure` dispatch) drops it from os.environ on import, so
        any tmux server it boots can't inherit it — but keeps the captured
        copy for deliberate `-e` propagation."""
        env = dict(os.environ)
        env["AGENTWIRE_UNATTENDED"] = "1"
        env["AGENTWIRE_UNATTENDED_ALLOW"] = "task.rule"
        code = (
            "import os\n"
            "from agentwire import core\n"
            "assert 'AGENTWIRE_UNATTENDED' not in os.environ\n"
            "assert 'AGENTWIRE_UNATTENDED_ALLOW' not in os.environ\n"
            "assert core._UNATTENDED_ENV['AGENTWIRE_UNATTENDED'] == '1'\n"
            "assert core._UNATTENDED_ENV['AGENTWIRE_UNATTENDED_ALLOW'] == 'task.rule'\n"
            "m = core._with_unattended_env({})\n"
            "assert m['AGENTWIRE_UNATTENDED'] == '1'\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
