"""agentwire owns the unattended marker and passes grants through opaquely."""

import json
import subprocess
import sys

from agentwire import unattended


def test_is_unattended_only_for_exactly_one(monkeypatch):
    bad = []
    for value, expected in [("1", True), ("0", False), ("", False), ("true", False)]:
        monkeypatch.setenv(unattended.UNATTENDED_ENV, value)
        if unattended.is_unattended() is not expected:
            bad.append(value)
    monkeypatch.delenv(unattended.UNATTENDED_ENV)
    if unattended.is_unattended():
        bad.append("<unset>")
    assert not bad, bad


def test_encode_allow_wire_format():
    scoped = [{"id": "git.commit", "paths": ["~/x/"]}, "a.b"]
    assert unattended.encode_allow(["a.b", " c.d ", ""]) == "a.b,c.d"
    assert unattended.encode_allow(None) == ""
    assert unattended.encode_allow([]) == ""
    assert json.loads(unattended.encode_allow(scoped)) == scoped


def test_task_modules_import_without_engine():
    code = (
        "import sys\n"
        "sys.modules['damage_control'] = None\n"
        "import agentwire.tasks, agentwire.tasks_cli, agentwire.scheduler.dispatch\n"
        "from agentwire.tasks import parse_task_config, validate_task\n"
        "tc = parse_task_config('t', {'prompt': 'hi', 'unattended_allow': ['a', {'id': 'b', 'paths': ['/x']}, 7]})\n"
        "assert not [i for i in validate_task(tc) if 'unattended_allow' in i]\n"
        "assert tc.unattended_allow == ['a', {'id': 'b', 'paths': ['/x']}, 7]\n"
        "import os; os.environ['AGENTWIRE_UNATTENDED'] = '1'\n"
        "from types import SimpleNamespace\n"
        "from agentwire.tasks_cli import cmd_tasks_promote\n"
        "assert cmd_tasks_promote(SimpleNamespace(json=False, yes=True, session=None)) == 1\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_dispatch_env_carries_marker_and_opaque_grants(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agentwire.scheduler import dispatch

    entries = ["x.y", {"id": "git.commit", "paths": ["~/m/"]}]
    monkeypatch.setattr("agentwire.tasks.load_task", lambda p, t: SimpleNamespace(unattended_allow=entries))
    env = dispatch._unattended_env(SimpleNamespace(project=str(tmp_path), task="t"))
    assert env[unattended.UNATTENDED_ENV] == "1"
    assert env[unattended.UNATTENDED_ALLOW_ENV] == unattended.encode_allow(entries)
