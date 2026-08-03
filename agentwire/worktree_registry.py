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
         "base": "develop", "worktree_path": "/Users/me/worktrees/monorepo/fix-bug",
         "created_at": "2026-06-14T10:30:00-04:00",
         "kind": "worker", "topology": "worktree"}
      ]
    }

Files are plain JSON and **hand-editable** — delete a stale entry by hand,
or run ``agentwire worktree --prune`` to drop entries whose worktree path
no longer exists.
"""

import contextlib
import datetime
import fcntl
import json
import os
import tempfile
from pathlib import Path

REGISTRY_DIR = Path.home() / ".agentwire" / "worktrees"


def _repo_key(project_path: Path) -> str:
    """Stable, filesystem-safe key from a repo's absolute path."""
    p = str(Path(project_path).expanduser().resolve())
    return p.strip("/").replace("/", "_").replace(" ", "_") or "root"


def registry_file(project_path: Path) -> Path:
    """Path to the registry JSON for a given repo."""
    return REGISTRY_DIR / f"{_repo_key(project_path)}.json"


@contextlib.contextmanager
def _locked(path: Path):
    """Serialize read-modify-write across processes via an flock sidecar.

    Concurrent ``agentwire worktree`` PROCESSES race on the same registry
    file (load → mutate → save). Without this, parallel dispatch of 4–5
    worktree sessions would lose all-but-one update. We flock a ``.lock``
    sibling (held for the whole RMW, not just the write) so writers
    serialize; pair it with the atomic replace in ``_save`` so a reader
    never sees a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    """Atomically write the registry (temp file in same dir + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def register(
    project_path: Path,
    *,
    branch: str | None,
    session: str,
    base: str | None,
    worktree_path: Path,
    created_at: str | None = None,
    kind: str | None = None,
    topology: str = "worktree",
) -> dict:
    """Record (or replace) a worktree session. Idempotent per session/path.

    Prefer ``worktree.register_worktree`` /
    ``worktree.create_and_register_worktree`` over calling this directly —
    they record the path git actually reports rather than whatever the
    caller string-built (#855).

    ``kind`` (#716) is the session's ROLE at registration time ("worker" or
    "orchestrator") — recorded so the dangling-PR detector (see
    ``session_cli.scan_dangling_worktrees``) can tell a safety-railed worker
    (which structurally can't self-merge and needs a live reviewer) apart
    from a self-rooted orchestrator (full authority, expected to be
    parentless — flagging it as "dangling" would be a false positive).

    ``topology`` (#837) is the orthogonal WHERE axis: "worktree" for a
    standalone worktree *session* (``session`` names a tmux session that IS
    this worktree), "pane" for a worker pane's isolated branch created by
    ``agentwire spawn --branch`` (``session`` names the *owning* session,
    whose pane 0 is an unrelated orchestrator). Teardown must not kill the
    session of a "pane" entry — that would take down the orchestrator, not
    the worker.

    The full read-modify-write is held under an exclusive flock so
    concurrent registration processes serialize instead of clobbering.
    """
    path = registry_file(project_path)
    if created_at is None:
        created_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    entry = {
        "branch": branch,
        "session": session,
        "base": base,
        "worktree_path": str(worktree_path),
        "created_at": created_at,
        "kind": kind,
        "topology": topology,
    }
    with _locked(path):
        data = _load(path)
        data["project"] = str(Path(project_path).expanduser().resolve())
        # Drop any prior entry for the same worktree path, then append. The
        # session name is a second identity key ONLY for worktree topology,
        # where one session IS one worktree — a "pane" entry's session is the
        # OWNING session, and several worker panes legitimately share it, so
        # deduping those by session would silently evict every branch but the
        # newest (#837).
        data["entries"] = [
            e for e in data["entries"]
            if e.get("worktree_path") != str(worktree_path)
            and not (
                topology == "worktree"
                and e.get("topology", "worktree") == "worktree"
                and e.get("session") == session
            )
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
    wt = str(worktree_path) if worktree_path is not None else None

    def matches(e: dict) -> bool:
        return (
            (session is not None and e.get("session") == session)
            or (branch is not None and e.get("branch") == branch)
            or (wt is not None and e.get("worktree_path") == wt)
        )

    with _locked(path):
        data = _load(path)
        before = len(data["entries"])
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
