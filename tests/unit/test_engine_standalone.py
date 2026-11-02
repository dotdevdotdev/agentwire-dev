"""The engine must be liftable out of agentwire (#1209 M0).

``agentwire/safety/{_core,profile,policy,adapter}.py`` and ``audit_logger.py`` are the code
that becomes the standalone package, so they may import only the standard library plus yaml, and the
GENERIC rule files must not name agentwire. Both are enforced; M2 moved the
agentwire-specific content into ``agentwire-rules/``.
"""

import ast
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ENGINE_FILES = [
    REPO / "agentwire" / "safety" / name
    for name in ("_core.py", "profile.py", "policy.py", "adapter.py")
] + [
    REPO / "agentwire" / "hooks" / "damage-control" / "audit_logger.py",
]
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = bundled_tooldefs_dir()
ALLOWED_THIRD_PARTY = {"yaml"}


def _is_stdlib(top):
    names = getattr(sys, "stdlib_module_names", None)  # 3.10+
    if names is not None:
        return top in names or top == "__future__"
    spec = importlib.util.find_spec(top)
    return bool(spec) and "site-packages" not in (spec.origin or "")


def _imports(path):
    """Every import in the file, nested ones included, as (lineno, top-level name, relative?)."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0], False
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, (node.module or "").split(".")[0], bool(node.level)


def test_engine_files_import_only_stdlib_and_yaml():
    bad = [
        (path.name, lineno, top or "<relative>")
        for path in ENGINE_FILES
        for lineno, top, relative in _imports(path)
        if relative or (top not in ALLOWED_THIRD_PARTY and not _is_stdlib(top))
    ]
    assert not bad, bad


# --------------------------------------------------------------------------
# Rule content
# --------------------------------------------------------------------------

NAMES = re.compile(r"agentwire|/Users/|dotdev", re.IGNORECASE)


def test_every_generic_rule_file_is_free_of_agentwire_and_owner_names():
    # Two things are exempt, both stated: comments (remote.yaml / publish.yaml mention
    # `agentwire/` only in a source-path comment — documentation, not rule content), and
    # `id:` values — an id is a frozen opaque handle (#1209 M1), so the tmux pack's
    # `agentwire.tmux-kill-server-...` keeps the name it shipped under.
    offenders = {}
    for p in sorted(RULES_DIR.glob("*.yaml")) + sorted(TOOLDEFS_DIR.glob("*.yaml")):
        body = "\n".join(
            line
            for line in p.read_text().splitlines()
            if not line.lstrip().startswith("#") and not re.match(r"\s*(-\s+)?id:", line)
        )
        hits = sorted({m.group(0).lower() for m in NAMES.finditer(body)})
        if hits:
            offenders[f"{p.parent.name}/{p.name}"] = hits
    assert not offenders, offenders


# --------------------------------------------------------------------------
# Host coupling (#1209 M3)
# --------------------------------------------------------------------------

HOST_LITERAL = re.compile(r"AGENTWIRE_|~/\.agentwire|\.agentwire/")


def test_no_engine_code_names_an_agentwire_env_var_or_home_path():
    # M3 moved those into the host profile. Docstrings are prose, not behaviour; messages
    # that merely mention the CLI ("agentwire doctor") are a separate, smaller debt.
    offenders = []
    for path in ENGINE_FILES:
        tree = ast.parse(path.read_text())
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
        }
        offenders += [
            (path.name, n.lineno, n.value[:60])
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings and HOST_LITERAL.search(n.value)
            and path.name != "audit_logger.py"  # its AGENTWIRE_DIR read is the profile's `audit` field (M5/M6)
        ]
    assert not offenders, offenders
