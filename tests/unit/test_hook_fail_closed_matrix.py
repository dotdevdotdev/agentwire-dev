"""The hook's fail-closed matrix (#1209 M4): every way the entry can break must BLOCK.

A PreToolUse hook that dies with Python's own exit 1 is non-blocking, so the tool call runs
with the guard off. ``python -I -m damage_control.hook`` therefore has to turn each of these
into exit 2, and none of them may disable the guard it is breaking:

* any failure while IMPORTING the package (a syntax error, a raising module, a missing one)
* a ``json.py`` planted in the working directory (``-I`` keeps cwd off ``sys.path``)
* PyYAML absent (only a human's YAML overlay needs it; the bundled JSON rules still enforce)
* a bad or oversized payload

An interpreter that no longer exists never reaches Python at all; that case is the
registration's ``"onFailure": "block"`` (``test_onfailure_block.py``) and doctor's dead-interpreter
check (``test_safety_deployment.py``).

Standard library + pytest only: the Python 3.9 CI job runs this file without agentwire; the
AgentWire profile arrives as a manifest file (``DC_TEST_PROFILE``), as it does for the real hook.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from damage_control.engine import MAX_HOOK_INPUT_CHARS

PACKAGE_SRC = Path(__file__).resolve().parents[2] / "packages" / "damage-control-dev" / "src"
FAKE_HOME = "/golden/home"


def _profile() -> str:
    path = os.environ.get("DC_TEST_PROFILE")
    if path:
        return path
    from tests.conftest import shared_profile_manifest

    return str(shared_profile_manifest())


# Runs the hook module as ``python -m damage_control.hook`` would, after `setup` has run
# (a stand-in for "the interpreter is in this state"). ``-I`` is still on the command line.
_RUNNER = """
import runpy, sys
{setup}
sys.argv = ["damage_control.hook"] + sys.argv[1:]
runpy.run_module("damage_control.hook", run_name="__main__", alter_sys=True)
"""


def _env(tmp_path):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": FAKE_HOME, "AGENTWIRE_DIR": str(tmp_path / "aw")}


def _run_module(kind, payload, tmp_path, *, cwd=None, no_isolation=False):
    """The real command line: ``python -I -m damage_control.hook <kind> --profile <manifest>``."""
    argv = [sys.executable] + ([] if no_isolation else ["-I"]) + ["-m", "damage_control.hook", kind, "--profile", _profile()]
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(argv, input=raw, capture_output=True, text=True, env=_env(tmp_path),
                          cwd=str(cwd or tmp_path), timeout=120)


def _run_with(setup, kind, payload, tmp_path, *, pythonpath=None):
    """Run the hook after ``setup`` (python source), optionally against a modified copy of the package."""
    code = _RUNNER.format(setup=setup)
    if pythonpath:
        code = f"import sys; sys.path.insert(0, {str(pythonpath)!r})\n" + code
    argv = [sys.executable, "-I", "-c", code, kind, "--profile", _profile()]
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                          env=_env(tmp_path), cwd=str(tmp_path), timeout=120)


def _bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "permission_mode": "default"}


@pytest.fixture
def broken_copy(tmp_path):
    """A throwaway copy of the package that a test can damage."""
    root = tmp_path / "copy"
    shutil.copytree(PACKAGE_SRC / "damage_control", root / "damage_control",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_any_import_time_failure_exits_2(broken_copy, tmp_path):
    """A raising module, a syntax error and a deleted module are each a block, not Python's exit 1."""
    damages = {
        "raises": lambda root: _prepend(root / "damage_control" / "engine.py", "raise RuntimeError('boom at import')\n"),
        "syntax": lambda root: _prepend(root / "damage_control" / "engine.py", "def (:\n"),
        "missing": lambda root: (root / "damage_control" / "adapter.py").unlink(),
        # exit 0 is "allow": a module that calls sys.exit(0) at import must not read as a verdict
        "exits-0-at-import": lambda root: _prepend(root / "damage_control" / "engine.py", "import sys\nsys.exit(0)\n"),
        "raises-in-leaf": lambda root: _prepend(root / "damage_control" / "audit.py", "raise ImportError('leaf')\n"),
    }
    not_blocked = {}
    for name, damage in damages.items():
        root = tmp_path / f"copy-{name}"
        shutil.copytree(broken_copy, root)
        damage(root)
        proc = _run_with("", "bash", _bash("ls"), tmp_path, pythonpath=root)
        if proc.returncode != 2:
            not_blocked[name] = (proc.returncode, proc.stderr[-200:])
    assert not not_blocked, not_blocked


def _prepend(path, text):
    path.write_text(text + path.read_text())


def test_a_planted_json_py_in_the_cwd_is_ignored(tmp_path):
    """``-m`` without ``-I`` would put cwd first on sys.path and run an attacker's json.py."""
    plant = tmp_path / "plant"
    plant.mkdir()
    marker = tmp_path / "pwned"
    (plant / "json.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(0)\n")
    proc = _run_module("bash", _bash("rm -rf /"), tmp_path, cwd=plant)
    assert (proc.returncode, marker.exists()) == (2, False), proc.stderr[-300:]


def test_without_isolation_the_planted_json_py_does_run(tmp_path):
    """The control for the test above: if this stops being true the planted-file test proves nothing."""
    plant = tmp_path / "plant"
    plant.mkdir()
    marker = tmp_path / "pwned"
    (plant / "json.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(0)\n")
    _run_module("bash", _bash("rm -rf /"), tmp_path, cwd=plant, no_isolation=True)
    assert marker.exists(), "-m without -I no longer imports from cwd; re-derive why -I is mandatory"


def test_without_pyyaml_the_bundled_rules_still_enforce(tmp_path):
    """Only a human's YAML overlay needs PyYAML. ``ls`` is allowed and ``rm -rf /`` blocked without it."""
    no_yaml = "sys.modules['yaml'] = None  # makes `import yaml` raise ImportError"
    got = {
        "ls": _run_with(no_yaml, "bash", _bash("ls"), tmp_path).returncode,
        "rm -rf /": _run_with(no_yaml, "bash", _bash("rm -rf /"), tmp_path).returncode,
    }
    assert got == {"ls": 0, "rm -rf /": 2}


def test_an_unreadable_overlay_is_reported_not_silently_dropped(tmp_path):
    """No PyYAML + a human's YAML rule file: the file is skipped LOUDLY, the bundled rules stay on."""
    home = tmp_path / "home"
    overlay = home / ".agentwire" / "damage-control"
    overlay.mkdir(parents=True)
    (overlay / "mine.yaml").write_text("bashToolPatterns:\n  - pattern: 'frobnicate'\n    reason: mine\n")
    env = dict(_env(tmp_path), HOME=str(home), AGENTWIRE_DIR=str(home / ".agentwire"))
    code = _RUNNER.format(setup="sys.modules['yaml'] = None")
    proc = subprocess.run([sys.executable, "-I", "-c", code, "bash", "--profile", _profile()],
                          input=json.dumps(_bash("rm -rf /")), capture_output=True, text=True,
                          env=env, cwd=str(tmp_path), timeout=120)
    assert proc.returncode == 2 and "mine.yaml" in proc.stderr, (proc.returncode, proc.stderr[-400:])


def _oversized_body(chars):
    head, tail = '{"tool_name": "Other", "pad": "', '"}'
    return head + "a" * (chars - len(head) - len(tail)) + tail


def test_bad_json_and_an_oversized_payload_block(tmp_path):
    got = {
        "not json": _run_module("bash", "not json {{{", tmp_path).returncode,
        "at the cap": _run_module("bash", _oversized_body(MAX_HOOK_INPUT_CHARS), tmp_path).returncode,
        "one over": _run_module("bash", _oversized_body(MAX_HOOK_INPUT_CHARS + 1), tmp_path).returncode,
        "unknown kind": _run_module("nope", {}, tmp_path).returncode,
        "no kind": _run_module("", {}, tmp_path).returncode,
    }
    assert got == {"not json": 2, "at the cap": 0, "one over": 2, "unknown kind": 2, "no kind": 2}


def test_a_missing_profile_manifest_blocks(tmp_path):
    argv = [sys.executable, "-I", "-m", "damage_control.hook", "bash", "--profile", str(tmp_path / "absent.json")]
    proc = subprocess.run(argv, input=json.dumps(_bash("ls")), capture_output=True, text=True,
                          env=_env(tmp_path), cwd=str(tmp_path), timeout=120)
    assert proc.returncode == 2, proc.stderr[-300:]
