"""Retention for the durable role-prompt store (#884).

``~/.agentwire/role-prompts/<conversation-id>.txt`` (see
:func:`core.role_prompts_dir`) grew one file per agent launch, forever, with no
GC at all. ``spawn`` is the acute case: a worker pane gets a minted
conversation id and a durable role prompt, but a pane is not a session and has
no session-scoped record — so every pane launch, the highest-frequency launch
path there is, writes a file nothing will ever reference again.

## The rule, and why it isn't "delete on session exit"

Deleting on exit would reintroduce the exact bug #871 fixed with a tidier
implementation. The POINT of this store is that a prompt **outlives the process
that made it**: a session's tmux process dying is not the end of its
conversation, ``--resume`` brings the conversation back and needs its system
prompt, and one conversation chain can outlive many kill/recreate cycles. The
lifetime that matters is the CONVERSATION, not the session.

So:

1. **Reachable is forever.** A prompt whose conversation id appears in some
   session's ``conversation_ids`` chain (or in its recorded
   ``role_prompt_path``) is never deleted, at any age. An orchestrator
   conversation running for months stays resumable.
2. **Unreachable ages out**, on a generous timer — 30 days by default, not
   hours. Nothing references these, but they must not vanish mid-flight
   either.
3. **Panes are the age-out population, deliberately.** The alternative —
   giving panes somewhere to record a conversation id — invents a fourth
   identity axis for the one entity whose whole design is "short-lived, reaped
   by the idle hook, not a session". A pane's prompt is dead the moment the
   pane exits, which is minutes-to-hours; 30 days is two orders of magnitude
   of headroom over that.

## Safety: reachability is the guardrail, age is only disk-space policy

Read rule 1 as the entire safety mechanism, because it is. A live session can
never be swept AT ANY AGE — not because 30 days is a long time, but because its
conversation id is named by a session record, and reachability is checked
before age is ever consulted. ``max_age_days`` decides how much disk an
*unreferenced* file is worth; it is a housekeeping knob, and tuning it down to
an hour must never be able to delete a running agent's prompt.

That distinction is not academic. Before this module shipped,
:func:`reachable_conversation_ids` globbed ``sessions/*/metadata.json`` and so
missed every nested ``project/branch`` record — 58% of the real store (469 of
1106). With reachability broken, AGE was the only thing protecting a live
worktree session, which is how a one-character glob became a data-loss bug. If
reachability is ever wrong again, no threshold will save you.

This is the only destructive operation in the codebase pointed at a directory
full of live agents' system prompts, so it is deliberately paranoid:

- :func:`sweep` takes its store and its reachability source as **required
  parameters**. Nothing here reads a module-level path at call time, and the
  production entry point (:func:`tick`) is the only thing that resolves the
  real ones.
- Only regular files named ``<uuid4>.txt`` directly inside the store are ever
  unlinked. A directory, a symlink, or any file whose name isn't
  conversation-id-shaped is reported and left alone — so even a sweep aimed at
  the wrong directory can't delete a stranger's data.
- Nothing here copies a prompt, and nothing here widens a mode. The files are
  0600 in a 0700 dir (#881); a GC that leaked a world-readable copy would undo
  that fix.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

# Generous by design — see the module docstring. Weeks, not hours.
DEFAULT_MAX_AGE_DAYS = 30
# The sweep is cheap (a directory listing plus a few small reads), but there is
# nothing to gain from running it every watchdog minute.
DEFAULT_INTERVAL_HOURS = 24

PROMPT_SUFFIX = ".txt"
# uuid4 as `build_agent_command` mints it. Matching this — rather than "any
# .txt" — is what keeps a misaimed sweep harmless.
_CONVERSATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class PromptFile:
    """One role-prompt file, judged against the retention rule."""
    path: Path
    conversation_id: str
    size: int
    age_days: float
    reachable: bool

    def expired(self, max_age_days: float) -> bool:
        """Deletable: nothing references it AND it has aged out."""
        return not self.reachable and self.age_days > max_age_days


def reachable_conversation_ids(sessions_dir: Path) -> set[str]:
    """Every conversation id any session record still points at.

    The reachability set of the store, and therefore the ONLY thing standing
    between a live session and deletion of its system prompt — so it has to
    find EVERY record, at every depth.

    The glob is ``**/metadata.json``, not ``*/metadata.json``. Session names
    contain slashes by design: ``worktree.tmux_safe_name`` rewrites only ``.``
    and ``:``, and ``project/branch`` is what every ``agentwire worktree`` and
    every scheduler dispatch is called, so :func:`core.session_metadata_path`
    nests those records one level deeper. A flat glob saw 469 of 1106 records
    on the machine this was caught on — 58% of live conversations reading as
    unreachable, seven of their prompts misclassified.

    Both keys count: ``conversation_ids`` (the chain — ``--fork-session``
    mints a new id per resume, and an older link is still resumable) and
    ``role_prompt_path`` (whose stem IS a conversation id, including for a
    remote mirror).

    A record that can't be read contributes nothing, which is the safe
    direction only because an unreadable record's prompts then fall to the
    AGE rule rather than to immediate deletion.
    """
    ids: set[str] = set()
    if not sessions_dir.is_dir():
        return ids
    for meta_file in sorted(sessions_dir.glob("**/metadata.json")):
        try:
            meta = json.loads(meta_file.read_text()) or {}
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        for cid in meta.get("conversation_ids") or []:
            if isinstance(cid, str) and cid:
                ids.add(cid)
        prompt_path = meta.get("role_prompt_path")
        if isinstance(prompt_path, str) and prompt_path:
            ids.add(Path(prompt_path).stem)
    return ids


def scan(store: Path, sessions_dir: Path, *, now: float | None = None) -> list[PromptFile]:
    """Every recognized prompt file in *store*, with its reachability and age.

    Unrecognized entries (directories, symlinks, non-uuid names) are excluded
    here and surfaced separately by :func:`status` — this list is the only
    thing :func:`sweep` will ever consider unlinking.
    """
    now = time.time() if now is None else now
    reachable = reachable_conversation_ids(sessions_dir)
    found: list[PromptFile] = []
    for path in sorted(_recognized_files(store)):
        try:
            st = path.stat()
        except OSError:
            continue
        found.append(PromptFile(
            path=path,
            conversation_id=path.stem,
            size=st.st_size,
            age_days=max(0.0, (now - st.st_mtime) / 86400),
            reachable=path.stem in reachable,
        ))
    return found


def _recognized_files(store: Path):
    """Regular ``<uuid4>.txt`` files directly inside *store*. Nothing else."""
    if not store.is_dir():
        return
    for path in store.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix != PROMPT_SUFFIX or not _CONVERSATION_ID.match(path.stem):
            continue
        yield path


def unrecognized_entries(store: Path) -> list[Path]:
    """Anything in *store* the sweep refuses to touch. Reported, never deleted."""
    if not store.is_dir():
        return []
    recognized = set(_recognized_files(store))
    return sorted(p for p in store.iterdir() if p not in recognized)


def status(
    store: Path,
    sessions_dir: Path,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: float | None = None,
) -> dict:
    """Read-only report on the store. Never deletes anything."""
    files = scan(store, sessions_dir, now=now)
    expired = [f for f in files if f.expired(max_age_days)]
    return {
        "store": str(store),
        "exists": store.is_dir(),
        "total": len(files),
        "bytes": sum(f.size for f in files),
        "reachable": sum(1 for f in files if f.reachable),
        "unreachable": sum(1 for f in files if not f.reachable),
        "expired": len(expired),
        "expired_bytes": sum(f.size for f in expired),
        "unrecognized": [p.name for p in unrecognized_entries(store)],
        "max_age_days": max_age_days,
    }


def sweep(
    store: Path,
    sessions_dir: Path,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Delete unreachable prompt files older than *max_age_days*.

    Both directories are REQUIRED parameters — a destructive operation whose
    target comes from a module global is a bug regardless of whether any
    current caller trips it. :func:`tick` is the one place the real paths are
    resolved.

    Returns a summary; ``dry_run`` computes exactly the same summary without
    unlinking anything.
    """
    files = scan(store, sessions_dir, now=now)
    deleted: list[str] = []
    failed: list[str] = []
    freed = 0
    for f in files:
        if not f.expired(max_age_days):
            continue
        if not dry_run:
            try:
                f.path.unlink()
            except OSError as e:
                failed.append(f"{f.path.name}: {e}")
                continue
        deleted.append(f.path.name)
        freed += f.size
    return {
        "store": str(store),
        "deleted": deleted,
        "failed": failed,
        "bytes_freed": freed,
        "kept_reachable": sum(1 for f in files if f.reachable),
        "kept_young": sum(1 for f in files
                          if not f.reachable and not f.expired(max_age_days)),
        "skipped_unrecognized": [p.name for p in unrecognized_entries(store)],
        "dry_run": dry_run,
    }


def tick() -> dict:
    """One watchdog pass: sweep at most once per :data:`DEFAULT_INTERVAL_HOURS`.

    The ONLY function here that resolves the real store. Tests must call
    :func:`sweep` / :func:`status` with their own fixture directories instead
    — pointing a deletion pass at the operator's live store would strip the
    role from running agents, which is precisely the failure #881 fixed.

    Rides the limits watchdog, which already runs every 60s and owns the other
    periodic housekeeping. The stamp file makes the extra 59 wakeups nearly
    free and keeps the cadence honest across restarts.
    """
    from . import core

    store = core.role_prompts_dir()
    sessions_dir = core.CONFIG_DIR / "sessions"
    stamp = core.CONFIG_DIR / "role-prompt-sweep.json"
    now = time.time()

    try:
        last = stamp.stat().st_mtime
    except OSError:
        last = None
    if last is not None and now - last < DEFAULT_INTERVAL_HOURS * 3600:
        return {"skipped": "recent", "deleted": []}

    result = sweep(store, sessions_dir, now=now)
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps(
            {"swept_at": now, **{k: v for k, v in result.items()
                                 if k in ("deleted", "bytes_freed",
                                          "kept_reachable", "kept_young")}},
            indent=2,
        ))
    except OSError:
        # A stamp we couldn't write means we sweep again next tick — wasteful,
        # never wrong. Not worth failing a housekeeping pass over.
        pass
    return result
