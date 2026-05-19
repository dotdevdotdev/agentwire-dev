"""Mission name derivations — pure functions, no I/O.

Convention:
- Session name:  {repo_short}/mission-{N}-{slug}
- Git branch:    mission-{N}-{slug}
- Worktree path: {projects_dir}/{repo_short}-worktrees/mission-{N}-{slug}

The branch uses a dash (not `mission/N-slug` with a slash) because
`agentwire/worktree.py:parse_session_name` splits a session name on its first
`/` to derive (project, branch); a slashed branch would collide with that
parser. The dash form preserves a 1:1 mapping between session, branch, and
worktree.
"""

import re
import unicodedata
from pathlib import Path

MAX_SLUG_LEN = 40


def slugify(title: str) -> str:
    """Convert a freeform issue title to an ASCII lower-kebab slug (≤40 chars).

    Empty input or input that contains no alphanumerics returns "untitled".
    """
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    if not slug:
        return "untitled"
    if len(slug) > MAX_SLUG_LEN:
        slug = slug[:MAX_SLUG_LEN].rstrip("-")
    return slug or "untitled"


def branch_name(issue_number: int, slug: str) -> str:
    """Git branch name for a mission. Format: ``mission-{N}-{slug}``."""
    return f"mission-{issue_number}-{slug}"


def session_name(repo_short: str, issue_number: int, slug: str) -> str:
    """Tmux session name for a mission worker. Format: ``{repo}/mission-{N}-{slug}``.

    Parses (via ``parse_session_name``) as project=``{repo_short}``,
    branch=``mission-{N}-{slug}`` — matching the worktree path derivation.
    """
    return f"{repo_short}/{branch_name(issue_number, slug)}"


def worktree_path(
    projects_dir: Path,
    repo_short: str,
    issue_number: int,
    slug: str,
) -> Path:
    """Filesystem path for the mission worker's git worktree.

    Matches ``agentwire/worktree.py:get_session_path`` derivation:
    ``{projects_dir}/{repo_short}-worktrees/{branch_name}``.
    """
    return projects_dir / f"{repo_short}-worktrees" / branch_name(issue_number, slug)


_MISSION_BRANCH_RE = re.compile(r"^mission-(\d+)-(.+)$")


def parse_mission_session(name: str) -> tuple[str, int, str] | None:
    """Inverse of :func:`session_name`. Returns ``(repo_short, issue_number, slug)``
    or ``None`` for foreign session names.
    """
    if "/" not in name:
        return None
    repo, branch = name.split("/", 1)
    m = _MISSION_BRANCH_RE.match(branch)
    if not m:
        return None
    return repo, int(m.group(1)), m.group(2)


def is_mission_session(name: str) -> bool:
    """True iff ``name`` is a well-formed mission session name."""
    return parse_mission_session(name) is not None
