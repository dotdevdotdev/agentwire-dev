"""
Enumerate the CLAUDE.md instruction chain for inclusion in a handoff bundle.

The bundle must be portable across machines and project folders, so the
receiving agent doesn't need to recreate the sender's environment to act on
the handoff. We inline:

- ~/.claude/CLAUDE.md (user global)
- ~/.claude/rules/*.md (rule files referenced by global)
- ./CLAUDE.md (project root) and any nested CLAUDE.md walking up to the user home
- ~/.claude/projects/<encoded>/memory/MEMORY.md + linked memory files
"""

from __future__ import annotations

import re
from pathlib import Path

from ..history import encode_project_path
from .schema import Instruction


HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _user_global() -> Instruction | None:
    p = CLAUDE_DIR / "CLAUDE.md"
    content = _read(p)
    if content is None:
        return None
    return Instruction(path=str(p), content=content, kind="claude_md")


def _user_rules() -> list[Instruction]:
    rules_dir = CLAUDE_DIR / "rules"
    if not rules_dir.is_dir():
        return []
    out: list[Instruction] = []
    for md in sorted(rules_dir.glob("*.md")):
        content = _read(md)
        if content is None:
            continue
        out.append(Instruction(path=str(md), content=content, kind="rule"))
    return out


def _project_chain(cwd: Path) -> list[Instruction]:
    """Walk from cwd up toward home, collecting CLAUDE.md files.

    Walking up matches Claude Code's recursive discovery, which loads parent
    CLAUDE.md files for any project nested under another instructed dir.
    """
    out: list[Instruction] = []
    seen: set[Path] = set()
    cursor = cwd.resolve()
    while True:
        candidate = cursor / "CLAUDE.md"
        if candidate.exists() and candidate.resolve() not in seen:
            content = _read(candidate)
            if content is not None:
                out.append(
                    Instruction(
                        path=str(candidate),
                        content=content,
                        kind="project_claude_md",
                    )
                )
                seen.add(candidate.resolve())
        if cursor == cursor.parent or cursor == HOME:
            break
        cursor = cursor.parent
    # Reverse so root-most CLAUDE.md comes first (loaded earliest by Claude Code).
    return list(reversed(out))


_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+\.md)\)")


def _memory(cwd: Path) -> list[Instruction]:
    """Read memory dir for the project. Index file is MEMORY.md.

    Includes any sibling .md files that MEMORY.md links to.
    """
    encoded = encode_project_path(str(cwd.resolve()))
    memory_dir = CLAUDE_DIR / "projects" / encoded / "memory"
    if not memory_dir.is_dir():
        return []

    out: list[Instruction] = []
    index = memory_dir / "MEMORY.md"
    index_content = _read(index)
    if index_content is None:
        return []

    out.append(Instruction(path=str(index), content=index_content, kind="memory"))

    # Pull in linked .md files inside the memory dir.
    referenced: set[str] = set()
    for match in _LINK_RE.finditer(index_content):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "/")):
            continue
        referenced.add(target)

    for ref in sorted(referenced):
        candidate = (memory_dir / ref).resolve()
        # Stay within the memory dir.
        try:
            candidate.relative_to(memory_dir.resolve())
        except ValueError:
            continue
        content = _read(candidate)
        if content is None:
            continue
        out.append(Instruction(path=str(candidate), content=content, kind="memory"))

    return out


def collect(cwd: str | Path | None = None) -> list[Instruction]:
    """Collect the full instruction chain for the given project directory.

    Args:
        cwd: Project directory. Defaults to current working directory.

    Returns:
        List of Instruction objects ordered roughly as Claude Code loads them:
        global → rules → project (root-most → cwd) → memory.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    out: list[Instruction] = []

    if user := _user_global():
        out.append(user)
    out.extend(_user_rules())
    out.extend(_project_chain(cwd_path))
    out.extend(_memory(cwd_path))

    return out
