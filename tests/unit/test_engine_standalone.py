"""The damage_control package must be liftable out of agentwire (#1209 M0, enforced by M4).

The dependency is one-way: agentwire imports ``damage_control``, never the reverse. This is the
CI-enforced form -- every module of the package may import only the standard library, its own
package and (lazily, for a human's YAML overlay) PyYAML; no ``agentwire`` import, no
``agentwire`` dependency in the package's pyproject, and no AgentWire name in the GENERIC rule
files. The 3.9 hook job runs this file without agentwire installed.
"""

import ast
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PACKAGE = REPO / "packages" / "damage-control-dev"
SRC = PACKAGE / "src" / "damage_control"
MODULES = sorted(SRC.glob("*.py"))
RULES_DIR = SRC / "rules"
TOOLDEFS_DIR = SRC / "tooldefs"
ALLOWED_THIRD_PARTY = {"yaml"}
OWN_PACKAGE = "damage_control"


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


def test_package_modules_are_found():
    assert {"hook.py", "adapter.py", "engine.py", "policy.py", "profile.py", "audit.py"} <= {m.name for m in MODULES}


def test_the_package_imports_nothing_from_agentwire():
    bad = [(m.name, lineno) for m in MODULES for lineno, top, _ in _imports(m) if top == "agentwire"]
    assert not bad, bad


def test_package_modules_import_only_stdlib_yaml_and_themselves():
    bad = [
        (path.name, lineno, top or "<relative>")
        for path in MODULES
        for lineno, top, relative in _imports(path)
        if relative or (top not in ALLOWED_THIRD_PARTY and top != OWN_PACKAGE and not _is_stdlib(top))
    ]
    assert not bad, bad


def test_yaml_is_only_ever_imported_lazily():
    """A hook process must not import PyYAML to learn its own (JSON) rules: that is the whole point of the JSON form."""
    top_level = []
    for path in MODULES:
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                    (a.name if isinstance(node, ast.Import) else node.module or "").split(".")[0] == "yaml"
                    for a in (node.names if isinstance(node, ast.Import) else [node])):
                top_level.append((path.name, node.lineno))
    assert not top_level, top_level


def test_the_bootstrap_wrapper_imports_only_os_and_sys_at_module_level():
    """Anything else imported here would fail OUTSIDE the wrapper's try/except, i.e. exit 1 (fail open)."""
    tree = ast.parse((SRC / "hook.py").read_text())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = sorted(a.name for n in top for a in n.names)
    assert names == ["os", "sys"], names


def test_the_package_declares_no_agentwire_dependency():
    text = (PACKAGE / "pyproject.toml").read_text()
    deps = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
    assert deps and "agentwire" not in deps.group(1).lower(), deps and deps.group(1)


# --------------------------------------------------------------------------
# Rule content
# --------------------------------------------------------------------------

NAMES = re.compile(r"agentwire|/Users/|dotdev", re.IGNORECASE)


def test_every_generic_rule_file_is_free_of_agentwire_and_owner_names():
    # Two things are exempt, both stated: comments (remote.yaml / publish.yaml mention
    # `agentwire/` only in a source-path comment -- documentation, not rule content), and
    # `id:` values -- an id is a frozen opaque handle (#1209 M1), so the tmux pack's
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
    # audit.py is the stated residual: its AGENTWIRE_DIR / AGENTWIRE_SESSION_ID reads become
    # the profile's `audit` field in M6.
    offenders = []
    for path in MODULES:
        if path.name == "audit.py":
            continue
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
        ]
    assert not offenders, offenders
