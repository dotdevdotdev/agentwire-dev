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
