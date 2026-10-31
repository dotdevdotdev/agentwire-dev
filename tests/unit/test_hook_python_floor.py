"""Every generated hook must import on the declared Python floor, and a crash must BLOCK (#1223).

``rule_id: str | None`` is evaluated at ``def`` time, so under macOS
``/usr/bin/python3`` (3.9.6) the Bash and MCP hooks died with ``TypeError`` and
exit 1 — non-blocking, so the tool call ran with the guard off.
"""

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control"
HOOKS = sorted(HOOKS_DIR.glob("*-tool-damage-control.py"))
FLOOR = (3, 9)


def _declared_floor(source: str) -> tuple:
    m = re.search(r'requires-python = ">=(\d+)\.(\d+)"', source)
    return (int(m.group(1)), int(m.group(2)))


def _annotations(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation:
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def test_five_hooks_found_and_declare_the_floor():
    assert len(HOOKS) == 5, HOOKS
    bad = [h.name for h in HOOKS if _declared_floor(h.read_text()) != FLOOR]
    assert not bad, bad


def test_hooks_parse_on_the_floor_grammar_and_defer_pep604_annotations():
    bad = []
    for hook in HOOKS:
        src = hook.read_text()
        try:
            tree = ast.parse(src, feature_version=FLOOR)
        except SyntaxError as e:
            bad.append((hook.name, f"syntax: {e}"))
            continue
        lazy = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "__future__"
            and any(a.name == "annotations" for a in n.names)
            for n in tree.body
        )
        piped = any(
            isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)
            for ann in _annotations(tree)
            for sub in ast.walk(ann)
        )
        if piped and not lazy:
            bad.append((hook.name, "X | Y annotation evaluated at def time"))
    assert not bad, bad


def test_import_time_crash_exits_2_not_1(tmp_path):
    bad = []
    for hook in HOOKS:
        src = hook.read_text()
        marker = "_sys.excepthook = _fail_closed_on_crash\n"
        assert marker in src, hook.name
        crashing = tmp_path / hook.name
        crashing.write_text(src.replace(marker, marker + "raise RuntimeError('boom at import')\n", 1))
        proc = subprocess.run(
            [sys.executable, str(crashing)], input="{}", capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 2:
            bad.append((hook.name, proc.returncode))
    assert not bad, bad


@pytest.mark.slow
def test_hooks_run_under_the_floor_interpreter():
    uv = shutil.which("uv")
    assert uv, "uv is required to fetch the floor interpreter"
    bad = []
    for hook in HOOKS:
        proc = subprocess.run(
            [uv, "run", "--no-project", "--python", f"{FLOOR[0]}.{FLOOR[1]}", "--with", "pyyaml",
             "python", str(hook)],
            input="{}", capture_output=True, text=True, timeout=300,
        )
        if proc.returncode not in (0, 2) or "Traceback" in proc.stderr:
            bad.append((hook.name, proc.returncode, proc.stderr[-300:]))
    assert not bad, bad
