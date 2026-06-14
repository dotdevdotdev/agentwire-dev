"""Local branch↔session registry for worktree sessions.

agentwire-owned **local state** — never provider data. It records which
worktree sessions exist for a repo so the `agentwire worktree` command can
list, clean up, and recover them robustly when 4–5 branches are in flight
at once (inference from the `{project}-{branch}` session name alone is
fragile at that scale).

Layout: one JSON file per repo under ``~/.agentwire/worktrees/``, keyed by
the repo's absolute path. Each file holds a list of entries:

    {
      "project": "/Users/me/projects/monorepo",
      "entries": [
        {"branch": "fix-bug", "session": "monorepo-fix-bug",
         "base": "develop", "worktree_path": "/Users/me/worktrees/monorepo-fix-bug",
         "created_at": "2026-06-14T10:30:00-04:00"}
      ]
    }

Files are plain JSON and **hand-editable** — delete a stale entry by hand,
or run ``agentwire worktree --prune`` to drop entries whose worktree path
no longer exists.
"""

import datetime
import json
from pathlib import Path

REGISTRY_DIR = Path.home() / ".agentwire" / "worktrees"


def _repo_key(project_path: Path) -> str:
    """Stable, filesystem-safe key from a repo's absolute path."""
    p = str(Path(project_path).expanduser().resolve())
    return p.strip("/").replace("/", "_").replace(" ", "_") or "root"


def registry_file(project_path: Path) -> Path:
    """Path to the registry JSON for a given repo."""
    return REGISTRY_DIR / f"{_repo_key(project_path)}.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {"project": None, "entries": []}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {"project": None, "entries": []}
        data.setdefault("entries", [])
        if not isinstance(data["entries"], list):
            data["entries"] = []
        return data
    except (json.JSONDecodeError, OSError):
        return {"project": None, "entries": []}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def register(
    project_path: Path,
    *,
    branch: str | None,
    session: str,
    base: str | None,
    worktree_path: Path,
    created_at: str | None = None,
) -> dict:
    """Record (or replace) a worktree session. Idempotent per session/path."""
    path = registry_file(project_path)
    data = _load(path)
    data["project"] = str(Path(project_path).expanduser().resolve())
    if created_at is None:
        created_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    entry = {
        "branch": branch,
        "session": session,
        "base": base,
        "worktree_path": str(worktree_path),
        "created_at": created_at,
    }
    # Drop any prior entry for the same session or worktree path, then append.
    data["entries"] = [
        e for e in data["entries"]
        if e.get("session") != session
        and e.get("worktree_path") != str(worktree_path)
    ]
    data["entries"].append(entry)
    _save(path, data)
    return entry


def entries(project_path: Path) -> list[dict]:
    """All recorded entries for a repo (oldest first)."""
    return _load(registry_file(project_path)).get("entries", [])


def unregister(
    project_path: Path,
    *,
    session: str | None = None,
    branch: str | None = None,
    worktree_path: Path | str | None = None,
) -> int:
    """Remove matching entries. Returns the number removed."""
    path = registry_file(project_path)
    data = _load(path)
    wt = str(worktree_path) if worktree_path is not None else None
    before = len(data["entries"])

    def matches(e: dict) -> bool:
        return (
            (session is not None and e.get("session") == session)
            or (branch is not None and e.get("branch") == branch)
            or (wt is not None and e.get("worktree_path") == wt)
        )

    data["entries"] = [e for e in data["entries"] if not matches(e)]
    _save(path, data)
    return before - len(data["entries"])


def all_entries() -> list[dict]:
    """Every entry across every repo, each tagged with its ``project`` path."""
    out: list[dict] = []
    if not REGISTRY_DIR.exists():
        return out
    for f in sorted(REGISTRY_DIR.glob("*.json")):
        data = _load(f)
        for e in data.get("entries", []):
            e = dict(e)
            e.setdefault("project", data.get("project"))
            out.append(e)
    return out
