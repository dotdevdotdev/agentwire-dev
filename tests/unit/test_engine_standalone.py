"""The engine must be liftable out of agentwire (#1209 M0).

``agentwire/safety/_core.py`` and ``audit_logger.py`` are the code that becomes the
standalone package, so they may import only the standard library plus yaml, and the
GENERIC rule files must not name agentwire. The first is enforced now; the second is
true of most files today and is xfail-listed for the rest until M2 lifts the
agentwire-specific content into its own rules directory.
"""

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
ENGINE_FILES = [
    REPO / "agentwire" / "safety" / "_core.py",
    REPO / "agentwire" / "hooks" / "damage-control" / "audit_logger.py",
]
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"
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

# Files that, today, still carry agentwire or owner content. M2 lifts it into an
# agentwire rules directory; each entry leaves this list when its file goes clean.
ENTANGLED = {
    "agentwire.yaml": "the agentwire pack itself (carved into an opt-in tmux pack at M2)",
    "core.yaml": "agentwire tail + the owner ~/.agentwire/.env allow",
    "mcp-tools.yaml": "six mcp__agentwire__* tool policies",
    "outbound.yaml": "two agentwire outbound-channel rules",
    "control-plane.yaml": "agentwire config paths in the protected set",
}
NAMES = re.compile(r"agentwire|/Users/|dotdev", re.IGNORECASE)


def _rule_files():
    return sorted(RULES_DIR.glob("*.yaml")) + sorted(TOOLDEFS_DIR.glob("*.yaml"))


def _naming(files):
    return {
        f"{p.parent.name}/{p.name}": sorted({m.group(0).lower() for m in NAMES.finditer(p.read_text())})
        for p in files
        if NAMES.search(p.read_text())
    }


def test_the_generic_rule_files_that_are_clean_today_stay_clean():
    generic = [p for p in _rule_files() if p.name not in ENTANGLED]
    # remote.yaml / publish.yaml mention `agentwire/` only in a source-path COMMENT; that is
    # documentation of where code lives, not rule content, so comments are exempt.
    offenders = {}
    for p in generic:
        body = "\n".join(line for line in p.read_text().splitlines() if not line.lstrip().startswith("#"))
        hits = sorted({m.group(0).lower() for m in NAMES.finditer(body)})
        if hits:
            offenders[f"{p.parent.name}/{p.name}"] = hits
    assert not offenders, offenders


def test_entangled_list_names_only_files_that_really_are_entangled():
    """An entry whose file went clean must leave ENTANGLED, or the xfail below never flips."""
    stale = [name for name in ENTANGLED if not _naming([RULES_DIR / name])]
    assert not stale, stale


@pytest.mark.xfail(
    strict=True,
    reason="MEANING: these rule files are not yet generic — they still name agentwire. "
    "Expected to fail until M2 (#1209) moves that content into an agentwire rules directory; "
    "strict, so the first run after M2 reports XPASS and forces this marker and ENTANGLED to be deleted.",
)
def test_every_rule_file_is_free_of_agentwire_and_owner_names():
    assert not _naming([RULES_DIR / name for name in ENTANGLED])
