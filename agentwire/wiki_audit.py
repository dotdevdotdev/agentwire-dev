"""Ground-truth audit for the LLM-maintained wiki.

The wiki at ``~/.agentwire/wiki/`` (Karpathy LLM-wiki pattern) accrues concrete
claims about the agentwire codebase — ``agentwire`` subcommands and flags, repo
file paths, config keys, qualified Python symbols — that nothing ever verifies.
An auto-maintained knowledge layer that's never checked rots into
confident-but-wrong, which is worse than no wiki at all.

This module extracts the *checkable* assertions from wiki markdown and verifies
each against the actual codebase, flagging the ones that no longer resolve. It
is the engine behind the ``/wiki lint`` skill's ground-truth audit.

Design notes:
- **Precision over recall.** A noisy audit is worse than none, so every category
  is scoped tightly (flags only inside ``agentwire`` command spans, paths only
  under known repo roots, symbols/config-keys disambiguated by context). We would
  rather miss a stale claim than cry wolf on a true one.
- **Stdlib only.** Runs as ``python -m agentwire.wiki_audit`` straight from a
  source checkout with no agentwire build/install.
- **Never auto-fixes.** Matches the existing lint stance — report ``file:line``,
  let the human decide.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_WIKI_DIR = Path.home() / ".agentwire" / "wiki" / "wiki"

# Repo-relative roots we treat as "this is a path claim about the codebase".
# Deliberately excludes the wiki's own ``wiki/`` and ``raw/`` trees and anything
# absolute or under ``~`` (runtime/home paths aren't codebase claims).
REPO_PATH_ROOTS = ("agentwire", "docs", "scripts", "tests", "examples", ".claude", ".github")

# ``agentwire <subcommand>`` — the token right after the binary name.
_RE_SUBCOMMAND = re.compile(r"\bagentwire\s+([a-z][a-z0-9-]*)")
# A long flag, e.g. ``--dev`` / ``--dry-run``.
_RE_FLAG = re.compile(r"--[a-z][a-z0-9-]+")
# An inline code span: `like this`.
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
# A dotted lowercase identifier chain, e.g. ``stt.backend`` or
# ``prompt_router.prompt_is_empty``. Used for both config-key and symbol claims.
_RE_DOTTED = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+)")
# A repo-relative path under one of REPO_PATH_ROOTS. Negative lookbehind keeps us
# from matching ``agentwire/server.py`` inside a URL like ``github.com/x/agentwire/...``.
_RE_REPO_PATH = re.compile(
    r"(?<![\w/.-])("
    + "|".join(r.replace(".", r"\.") for r in REPO_PATH_ROOTS)
    + r")(/[A-Za-z0-9_.\-]+)+"
)
# Shell separators that end one command and start another within a code span.
_RE_SHELL_SEP = re.compile(r"&&|\|\||[|;]")
# Config-key claims appear near a mention of a config file.
_RE_CONFIG_CONTEXT = re.compile(r"config\.yaml|\.agentwire\.yml|scheduler\.yaml|\byaml\b|\bconfig\b", re.I)

# File extensions — a dotted token ending in one of these is a filename, not a symbol.
_EXTENSIONS = frozenset(
    "py js ts mjs md yaml yml json toml cfg ini txt sh html css scss png jpg jpeg "
    "gif svg sql lock env log".split()
)
# Module-position words that are really local variables / stdlib modules, not an
# agentwire module — suppresses ``config.get`` / ``args.foo`` style false positives.
_COMMON_VARS = frozenset(
    "config self cls args kwargs ctx data result results response resp req request "
    "err error ex os re sys json io idx item items obj val value payload msg event "
    "node path paths line text src out ret cfg env db conn cur row rows doc app "
    "client session task parser ns logger log".split()
)
# Symbol-position words that are common method/attribute names on arbitrary objects.
_COMMON_METHODS = frozenset(
    "get set pop append extend items keys values update format join split rsplit "
    "strip lstrip rstrip replace startswith endswith read write close open group "
    "groups match search sub findall finditer dumps loads dump load add remove "
    "count index lower upper title encode decode copy sort reverse name value text "
    "json status_code content exists is_dir is_file parent stem suffix parts resolve".split()
)


@dataclass
class Finding:
    """One wiki claim that does not resolve against the codebase."""

    wiki_file: str  # path relative to the wiki dir
    line: int  # 1-based
    kind: str  # subcommand | flag | path | symbol | config-key
    claim: str  # the offending token
    reason: str  # why it failed to resolve
    text: str  # the source line (stripped)


@dataclass
class CodebaseIndex:
    """Everything we can cheaply pre-compute about the repo, built once."""

    repo_dir: Path
    subcommands: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)
    config_fields: set[str] = field(default_factory=set)
    modules: dict[str, Path] = field(default_factory=dict)  # top-level agentwire module name -> file

    def path_exists(self, rel: str) -> bool:
        return (self.repo_dir / rel).exists()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _python_sources(repo_dir: Path):
    pkg = repo_dir / "agentwire"
    root = pkg if pkg.is_dir() else repo_dir
    for p in sorted(root.rglob("*.py")):
        if "node_modules" in p.parts:
            continue
        yield p


def _collect_dataclass_fields(config_src: str) -> set[str]:
    """Flat set of every field name declared in any ``@dataclass`` in config.py.

    A flat membership set (rather than a strict nested path) is intentional: it
    covers both section names (``stt``, held as a field on the parent dataclass)
    and leaf keys (``backend``), and it keeps the check robust to refactors that
    move a field between nesting levels. We trade some recall (won't catch a key
    nested under the wrong section) for near-zero false positives.
    """
    fields: set[str] = set()
    try:
        tree = ast.parse(config_src)
    except SyntaxError:
        return fields
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_dataclass = any(
            (isinstance(d, ast.Name) and d.id == "dataclass")
            or (isinstance(d, ast.Attribute) and d.attr == "dataclass")
            or (isinstance(d, ast.Call) and (
                (isinstance(d.func, ast.Name) and d.func.id == "dataclass")
                or (isinstance(d.func, ast.Attribute) and d.func.attr == "dataclass")
            ))
            for d in node.decorator_list
        )
        if not is_dataclass:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.add(stmt.target.id)
    return fields


def build_codebase_index(repo_dir: Path) -> CodebaseIndex:
    """Pre-compute the valid subcommands / flags / config fields / modules."""
    idx = CodebaseIndex(repo_dir=repo_dir)
    idx.flags.add("--help")  # argparse-provided, never declared explicitly

    for src_path in _python_sources(repo_dir):
        src = _read(src_path)
        for m in re.finditer(r"""add_parser\(\s*["']([a-z][\w-]*)["']""", src):
            idx.subcommands.add(m.group(1))
        for m in re.finditer(r"""add_argument\(\s*["'](--[a-z][\w-]*)["']""", src):
            idx.flags.add(m.group(1))

    # Top-level agentwire modules (for symbol routing): agentwire/<name>.py
    pkg = repo_dir / "agentwire"
    if pkg.is_dir():
        for p in pkg.glob("*.py"):
            if p.stem != "__init__":
                idx.modules[p.stem] = p

    config_py = pkg / "config.py"
    if config_py.exists():
        idx.config_fields = _collect_dataclass_fields(_read(config_py))

    return idx


def _code_spans(line: str, in_fence: bool):
    """Yield the code portions of a line.

    Inside a fenced block the whole line is code; otherwise only inline
    ``backtick`` spans count. Flag/symbol/config claims are only trusted inside
    code, which is where they actually appear — this is the main false-positive
    guard against prose that merely mentions a flag.
    """
    if in_fence:
        yield line
    else:
        for m in _RE_INLINE_CODE.finditer(line):
            yield m.group(1)


def _strip_path(token: str) -> str:
    token = token.split("#", 1)[0]  # drop ``...md#anchor``
    return token.rstrip("/.,:;)\"'")


def _audit_line(
    idx: CodebaseIndex,
    rel_file: str,
    lineno: int,
    line: str,
    in_fence: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    text = line.strip()
    spans = list(_code_spans(line, in_fence))

    # --- subcommands + flags: only from code spans, scoped to the `agentwire ...`
    # command. Bare prose is excluded on purpose — outside code, "agentwire" is
    # just the product name followed by an ordinary English word, not a command.
    for span in spans:
        i = span.find("agentwire")
        if i == -1:
            continue
        cmd = _RE_SHELL_SEP.split(span[i:])[0]  # stop at && | ; etc.
        sm = _RE_SUBCOMMAND.match(cmd)
        # ``.`` right after the token means it's a version/sentence, not a command
        # (``agentwire v1.35.1``), so don't treat it as a subcommand claim.
        if sm and cmd[sm.end():sm.end() + 1] != "." and sm.group(1) not in idx.subcommands:
            findings.append(Finding(rel_file, lineno, "subcommand", f"agentwire {sm.group(1)}",
                                    "no such subcommand (no add_parser)", text))
        for fm in _RE_FLAG.finditer(cmd):
            flag = fm.group(0)
            if flag not in idx.flags:
                findings.append(Finding(rel_file, lineno, "flag", flag,
                                        "no such agentwire flag (no add_argument)", text))

    # --- repo paths: scan the full line, anchored to known repo roots
    for pm in _RE_REPO_PATH.finditer(line):
        raw = _strip_path(pm.group(0))
        if not raw or "<" in raw or ">" in raw or "*" in raw:
            continue
        if not idx.path_exists(raw):
            findings.append(Finding(rel_file, lineno, "path", raw,
                                    "path does not exist in repo", text))

    # --- dotted tokens (code spans only): route to config-key OR symbol check
    config_context = bool(_RE_CONFIG_CONTEXT.search(line))
    seen_dotted: set[str] = set()
    for span in spans:
        for dm in _RE_DOTTED.finditer(span):
            dotted = dm.group(1)
            if dotted in seen_dotted:
                continue
            seen_dotted.add(dotted)
            head, _, _ = dotted.partition(".")
            segments = dotted.split(".")

            if segments[-1] in _EXTENSIONS:
                continue  # ``__main__.py`` / ``config.yaml`` — a filename, not a claim
            if config_context and head in idx.config_fields:
                bad = [s for s in segments if s not in idx.config_fields]
                if bad:
                    findings.append(Finding(rel_file, lineno, "config-key", dotted,
                                            f"unknown config field(s): {', '.join(bad)}", text))
            elif (
                head in idx.modules
                and len(segments) == 2
                and head not in _COMMON_VARS
                and segments[1] not in _COMMON_METHODS
            ):
                symbol = segments[1]
                if not _symbol_defined(idx.modules[head], symbol):
                    findings.append(Finding(rel_file, lineno, "symbol", dotted,
                                            f"{symbol} not defined in agentwire/{head}.py", text))

    return findings


def _symbol_defined(module_path: Path, symbol: str) -> bool:
    src = _read(module_path)
    pat = re.compile(
        rf"\bdef\s+{re.escape(symbol)}\b|\bclass\s+{re.escape(symbol)}\b|^\s*{re.escape(symbol)}\s*[:=]",
        re.M,
    )
    return bool(pat.search(src))


def audit_text(idx: CodebaseIndex, rel_file: str, content: str) -> list[Finding]:
    """Audit a single wiki document's text. Exposed for testing."""
    findings: list[Finding] = []
    in_fence = False
    for i, line in enumerate(content.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        findings.extend(_audit_line(idx, rel_file, i, line, in_fence))
    return findings


def audit(wiki_dir: Path, repo_dir: Path) -> list[Finding]:
    """Audit every markdown page under ``wiki_dir`` against ``repo_dir``."""
    idx = build_codebase_index(repo_dir)
    findings: list[Finding] = []
    for md in sorted(wiki_dir.rglob("*.md")):
        rel = str(md.relative_to(wiki_dir))
        findings.extend(audit_text(idx, rel, _read(md)))
    return findings


def _default_repo_dir() -> Path:
    # agentwire/wiki_audit.py -> parents[1] is the repo root.
    return Path(__file__).resolve().parents[1]


def _format_text(findings: list[Finding]) -> str:
    if not findings:
        return "✓ Wiki ground-truth audit: no drift found."
    out: list[str] = [f"Wiki ground-truth audit: {len(findings)} drift finding(s)\n"]
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.wiki_file, []).append(f)
    for wiki_file in sorted(by_file):
        out.append(f"\n{wiki_file}")
        for f in sorted(by_file[wiki_file], key=lambda x: x.line):
            out.append(f"  {wiki_file}:{f.line}  [{f.kind}] {f.claim}")
            out.append(f"      → {f.reason}")
    out.append("\nThese are claims that no longer resolve. Review and update the wiki — nothing was auto-fixed.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentwire.wiki_audit",
        description="Verify concrete claims in the LLM wiki against the actual codebase.",
    )
    parser.add_argument("--wiki-dir", type=Path, default=DEFAULT_WIKI_DIR,
                        help=f"Wiki pages directory (default: {DEFAULT_WIKI_DIR})")
    parser.add_argument("--repo-dir", type=Path, default=_default_repo_dir(),
                        help="Codebase to verify against (default: this repo)")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 when drift is found (for CI); default exits 0")
    args = parser.parse_args(argv)

    if not args.wiki_dir.is_dir():
        print(f"wiki dir not found: {args.wiki_dir}", file=sys.stderr)
        return 2

    findings = audit(args.wiki_dir, args.repo_dir)

    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        print(_format_text(findings))

    return 1 if (findings and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
