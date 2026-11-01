"""A deleted process cwd must never crash damage-control, doctor or hooks install (#1238).

``os.environ.get("PWD", os.getcwd())`` evaluates ``os.getcwd()`` eagerly, so it
raised ``FileNotFoundError`` for a session whose worktree was torn down beneath
it — and since #1223 an uncaught hook crash exits 2 (block), wedging every tool
call. These tests delete a REAL temp directory and exec into it (no mocked
``getcwd``): the child chdirs into a fresh dir, removes it, then ``execv``s the
target, which inherits the dead cwd.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import HOOKS_DIR

REPO = Path(__file__).resolve().parents[2]

# argv: <PWD mode: unset|stale> <target argv...>
_BOOTSTRAP = """
import os, sys, tempfile
mode, argv = sys.argv[1], sys.argv[2:]
d = tempfile.mkdtemp(prefix="deadcwd-")
os.chdir(d)
os.rmdir(d)
if mode == "unset":
    os.environ.pop("PWD", None)
else:
    os.environ["PWD"] = d  # what the shell still says after the dir is gone
os.execv(argv[0], argv)
"""

HOOKS = [
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
    "read-tool-damage-control.py",
    "mcp-tool-damage-control.py",
]

PAYLOADS = {
    "bash-tool-damage-control.py": {"tool_name": "Bash", "tool_input": {"command": "ls"}},
    "edit-tool-damage-control.py": {
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/safe-cwd-probe.txt", "old_string": "a", "new_string": "b"},
    },
    "write-tool-damage-control.py": {
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/safe-cwd-probe.txt", "content": "x"},
    },
    "read-tool-damage-control.py": {
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/safe-cwd-probe.txt"},
    },
    "mcp-tool-damage-control.py": {"tool_name": "mcp__agentwire__sessions_list", "tool_input": {}},
}


def _env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTWIRE")}
    env.update(HOME=str(home), AGENTWIRE_DIR=str(home / ".agentwire"))
    return env


def _in_dead_cwd(mode, argv, tmp_path, stdin=""):
    return subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP, mode, *argv],
        input=stdin, capture_output=True, text=True, env=_env(tmp_path), timeout=120,
    )


def test_every_hook_survives_a_deleted_cwd(tmp_path):
    bad = []
    for mode in ("unset", "stale"):
        for hook in HOOKS:
            r = _in_dead_cwd(
                mode, [sys.executable, str(HOOKS_DIR / hook)], tmp_path,
                stdin=json.dumps(PAYLOADS[hook]),
            )
            if r.returncode != 0 or "FileNotFoundError" in r.stderr:
                bad.append((mode, hook, r.returncode, r.stderr[-300:]))
    assert not bad, bad


def test_doctor_and_hooks_install_survive_a_deleted_cwd(tmp_path):
    bad = []
    for sub in (["doctor"], ["hooks", "install"]):
        r = _in_dead_cwd(
            "unset", [sys.executable, "-m", "agentwire", *sub], tmp_path
        )
        out = r.stdout + r.stderr
        for needle in ("FileNotFoundError", "Traceback", "Could not read safety config",
                       "Could not check allowedPaths", "Could not check rule files"):
            if needle in out:
                bad.append((sub, needle, out[-400:]))
    assert not bad, bad


def test_engine_entry_points_do_not_raise_from_a_deleted_cwd(tmp_path):
    script = (
        "from agentwire.safety import _core as c\n"
        "c.safe_cwd(); c._find_project_config(); c.policy_file_parse_failures()\n"
        "print('ok')\n"
    )
    r = _in_dead_cwd("unset", [sys.executable, "-c", script], tmp_path)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-400:]


def test_audit_logger_mirror_agrees_with_engine_on_a_deleted_cwd(tmp_path):
    script = (
        "import importlib.util, sys\n"
        f"s = importlib.util.spec_from_file_location('al', {str(HOOKS_DIR / 'audit_logger.py')!r})\n"
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        "from agentwire.safety._core import safe_cwd\n"
        "assert m._safe_cwd() == safe_cwd(), (m._safe_cwd(), safe_cwd())\n"
        "print(safe_cwd())\n"
    )
    r = _in_dead_cwd("unset", [sys.executable, "-c", script], tmp_path)
    assert r.returncode == 0, r.stderr[-400:]
    assert r.stdout.strip() == str(tmp_path / "home")
