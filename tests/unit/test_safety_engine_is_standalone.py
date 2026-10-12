"""The damage-control engine must stay importable without the rest of agentwire (#1064).

The five hook scripts run as standalone processes and `_core.py` is inlined into
them, so a stray `import agentwire.<x>` would fail at hook time on a machine
where the package is not importable from the hook's interpreter, and it would
weld the engine to the product, closing the door on extracting it. Coupling to
agentwire is by convention (paths, env var names, rule ids), never by import.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ENGINE_FILES = [REPO / "agentwire" / "safety" / "_core.py"] + sorted(
    (REPO / "agentwire" / "hooks" / "damage-control").glob("*.py")
)


def _agentwire_imports(path: Path) -> list:
    bad = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            bad += [f"{path.name}:{node.lineno} import {a.name}" for a in node.names if a.name.split(".")[0] == "agentwire"]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "agentwire":
            bad.append(f"{path.name}:{node.lineno} from {node.module} import ...")
    return bad


def test_engine_and_hooks_import_nothing_from_agentwire():
    assert len(ENGINE_FILES) >= 6, ENGINE_FILES  # _core + 5 generated hooks + audit logger
    offenders = [b for f in ENGINE_FILES for b in _agentwire_imports(f)]
    assert not offenders, offenders
