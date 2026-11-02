"""The damage_control package must import on the declared Python floor (#1223, #1209 M4).

``rule_id: str | None`` is evaluated at ``def`` time, so under macOS
``/usr/bin/python3`` (3.9.6) the Bash and MCP hooks once died with ``TypeError`` and
exit 1 — non-blocking, so the tool call ran with the guard off. The hook is now
``python -I -m damage_control.hook`` over a real package, so the floor is a property
of every module in it, not of five generated scripts.
"""

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "packages" / "damage-control-dev"
MODULES = sorted((PACKAGE / "src" / "damage_control").glob("*.py"))
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


def test_package_declares_the_floor_and_ships_the_hook_modules():
    names = {m.name for m in MODULES}
    assert {"hook.py", "adapter.py", "engine.py", "policy.py", "profile.py", "audit.py"} <= names
    assert _declared_floor((PACKAGE / "pyproject.toml").read_text()) == FLOOR


def test_modules_parse_on_the_floor_grammar_and_defer_pep604_annotations():
    bad = []
    for module in MODULES:
        src = module.read_text()
        try:
            tree = ast.parse(src, feature_version=FLOOR)
        except SyntaxError as e:
            bad.append((module.name, f"syntax: {e}"))
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
            bad.append((module.name, "X | Y annotation evaluated at def time"))
    assert not bad, bad


@pytest.mark.slow
def test_every_hook_kind_runs_under_the_floor_interpreter():
    uv = shutil.which("uv")
    assert uv, "uv is required to fetch the floor interpreter"
    bad = []
    for kind in ("bash", "edit", "write", "read", "mcp"):
        proc = subprocess.run(
            [uv, "run", "--no-project", "--python", f"{FLOOR[0]}.{FLOOR[1]}", "--with", "pyyaml",
             "--with", str(PACKAGE), "python", "-I", "-m", "damage_control.hook", kind],
            input="{}", capture_output=True, text=True, timeout=300,
        )
        if proc.returncode not in (0, 2) or "Traceback" in proc.stderr:
            bad.append((kind, proc.returncode, proc.stderr[-300:]))
    assert not bad, bad
