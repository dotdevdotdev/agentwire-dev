"""Repair conversation history orphaned by a moved working directory (#871).

Claude Code keys a conversation's transcript by the directory it ran in:
``~/.claude/projects/<encoded-cwd>/<conversation-id>.jsonl``. Move the
directory and the key no longer matches, so ``--resume <id>`` fails with
"No conversation found with session ID" even though the transcript is sitting
on disk, intact, under the old name.

**Why this is a ``history migrate`` verb and not a ``worktree --move`` verb.**
The issue text for #871 asked for the latter, describing a flag that does not
exist. A move verb would only repair moves made *through agentwire*, and that
is the minority of them: ``git worktree move``, a plain ``mv``, and a
reorganised ``~/worktrees`` all orphan history exactly the same way and would
all still be broken. The damage is not caused by moving — it is caused by the
recorded cwd and the real cwd disagreeing, which #881 made *detectable* by
recording ``cwd_at_launch``. So the repair is a reconciliation keyed on that
disagreement, and it works no matter who moved the directory or how. It also
composes: the same :func:`scan` that powers a dry run is what a doctor check
consumes, and it stays correct if agentwire ever does grow a move verb.

**Two things this module refuses to do.**

1. It never destroys history. Every migration copies into a staging directory,
   verifies the copy byte-for-byte, and only then publishes it; the source is
   left alone unless the caller explicitly asks for it to be pruned *after*
   that verification passed. If the target already exists it refuses outright
   and reports, rather than merging two transcript sets or clobbering one.
2. It never treats missing history as a crash. A recorded conversation id does
   not guarantee a resumable conversation — history directories have been
   observed disappearing on their own — so "the source isn't there" is a
   normal, reportable outcome (:data:`SOURCE_ABSENT`), not an error.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from .core import CONFIG_DIR, load_session_metadata
from .history import (
    PROJECTS_DIR,
    history_key_candidates,
    history_key_sources,
    locate_conversation,
)

# Outcomes. Every one of these is a legitimate thing to report; only ERROR and
# TARGET_EXISTS are failures, and neither of them touches anything on disk.
ALIGNED = "aligned"                # recorded cwd already matches reality
READY = "ready"                    # a migration is available (dry run)
MIGRATED = "migrated"              # a migration was performed and verified
SOURCE_ABSENT = "source_absent"    # nothing to migrate — normal, not an error
TARGET_EXISTS = "target_exists"    # refused: would merge or clobber
UNDETERMINED = "undetermined"      # not enough recorded identity to judge
ERROR = "error"

FAILURE_STATUSES = {TARGET_EXISTS, ERROR}


def resumable(conversation_id: str, cwd: str | Path) -> bool:
    """Whether ``claude --resume <conversation_id>`` would work from *cwd*.

    ``resumable(id, cwd) == exists(<encoded-cwd>/<id>.jsonl)`` — one predicate,
    deliberately, rather than a second notion of resumability living next to
    the real one. The same file governs both directions: a launched-but-never-
    prompted session has no transcript at all (the ``.jsonl`` is written
    lazily on the first turn), which is why a recorded conversation id can be
    perfectly valid and still not resumable, and why ``--session-id`` reports a
    collision on that file EXISTING rather than on the id having been used.

    Delegates to :func:`history.locate_conversation`, which answers the same
    question with more detail (resumable / orphaned / gone) for ``restart``
    and ``doctor``. Passing ``PROJECTS_DIR`` explicitly keeps this module's
    own patchable store as the seam callers already isolate against.
    """
    return locate_conversation(conversation_id, cwd, projects_dir=PROJECTS_DIR).resumable


def _existing_source(cwd: str | Path) -> Path | None:
    for key in history_key_candidates(cwd):
        candidate = PROJECTS_DIR / key
        if candidate.is_dir():
            return candidate
    return None


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    """Content fingerprint of a tree: relative path -> (size, sha256).

    Symlinks are recorded by their target rather than followed, so a copy is
    only called verified when it reproduces the tree's actual shape.
    """
    out: dict[str, tuple[int, str]] = {}
    for item in sorted(root.rglob("*")):
        rel = str(item.relative_to(root))
        if item.is_symlink():
            out[rel] = (-1, os.readlink(item))
        elif item.is_file():
            out[rel] = (item.stat().st_size, _digest(item))
        elif item.is_dir():
            out[rel] = (-2, "")
    return out


STAGING_PREFIX = ".agentwire-migrate-"


def _first_recorded_cwd(transcript: Path) -> str | None:
    """The ``cwd`` a transcript says it was written from, or None."""
    try:
        with transcript.open() as fh:
            for i, line in enumerate(fh):
                if i > 30:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("cwd"):
                    return obj["cwd"]
    except OSError:
        return None
    return None


def classify_source(history_dir: Path, old_cwd: str | Path) -> tuple[dict[str, int], list[str]]:
    """Split a history directory by the cwd its transcripts were written from.

    Returns ``(cwd -> count, foreign_filenames)``. A file counts as foreign
    only when it *positively states* a different cwd. Transcripts with no
    readable cwd, and every non-transcript entry (``memory/`` and friends),
    are treated as belonging to the directory's own key — they carry no
    evidence to the contrary, and the source is retained anyway.
    """
    own = set(history_key_sources(old_cwd))
    provenance: dict[str, int] = {}
    foreign: list[str] = []
    for f in sorted(history_dir.glob("*.jsonl")):
        cwd = _first_recorded_cwd(f)
        if not cwd:
            continue
        provenance[cwd] = provenance.get(cwd, 0) + 1
        if cwd not in own:
            foreign.append(f.name)
    return provenance, foreign


def recorded_cwds(history_dir: Path) -> dict[str, int]:
    """The cwds the transcripts in *history_dir* say they were written from.

    Every ``*.jsonl`` carries a ``cwd`` on its records — the same ground truth
    the encoding itself was derived from. Normally they all agree with the
    directory's own key, but they need not: because the encoding is
    non-injective, and because a directory can be renamed under a live
    session, one history directory can legitimately hold transcripts from
    **two different projects**. One such directory exists on the machine this
    was written on.

    Returns cwd -> transcript count. Unreadable or cwd-less files are skipped
    rather than raising: this informs a report, and a malformed transcript
    must not be able to block a migration.
    """
    counts: dict[str, int] = {}
    for f in sorted(history_dir.glob("*.jsonl")):
        try:
            with f.open() as fh:
                for i, line in enumerate(fh):
                    if i > 30:
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = isinstance(obj, dict) and obj.get("cwd")
                    if cwd:
                        counts[cwd] = counts.get(cwd, 0) + 1
                        break
        except OSError:
            continue
    return counts


def sweep_staging() -> list[str]:
    """Remove staging directories abandoned by an interrupted run.

    A staging directory only ever exists mid-``apply``; one still present is
    the remnant of a process that died between copy and publish. It holds a
    *copy*, never the only extant transcript — the source is untouched until
    after publication — so clearing it cannot lose history. Without this they
    accumulate silently, the same shape as #884.
    """
    if not PROJECTS_DIR.is_dir():
        return []
    swept = []
    for p in PROJECTS_DIR.iterdir():
        if p.is_dir() and p.name.startswith(STAGING_PREFIX):
            try:
                shutil.rmtree(p)
                swept.append(p.name)
            except OSError:
                pass
    return swept


def plan(old_cwd: str | Path, new_cwd: str | Path) -> dict:
    """Decide what migrating *old_cwd*'s history to *new_cwd* would do.

    Pure inspection — reads the filesystem, writes nothing. The returned dict
    is the same shape :func:`apply` returns, so a dry run and a real run are
    reported identically.
    """
    source = _existing_source(old_cwd)
    target_key = history_key_candidates(new_cwd)[-1]
    target = PROJECTS_DIR / target_key

    result = {
        "old_cwd": str(old_cwd),
        "new_cwd": str(new_cwd),
        "source": str(source) if source else str(PROJECTS_DIR / history_key_candidates(old_cwd)[0]),
        "target": str(target),
    }

    if source and source.resolve() == target.resolve():
        return {**result, "status": ALIGNED,
                "detail": "history is already keyed to the current directory"}
    if source is None:
        return {**result, "status": SOURCE_ABSENT,
                "detail": "no history directory for the recorded cwd — nothing to migrate"}
    if target.exists():
        return {**result, "status": TARGET_EXISTS,
                "detail": "target history directory already exists — refusing to merge or "
                          "overwrite it; inspect both and resolve by hand"}

    files = [p for p in source.rglob("*") if p.is_file()]
    out = {**result, "status": READY,
           "files": len(files),
           "bytes": sum(p.stat().st_size for p in files),
           "detail": "ready to migrate"}

    # The destination is guarded by refusing to merge; the SOURCE needs its own
    # check. A history directory can hold transcripts from more than one
    # project — a directory renamed under a live session leaves the old key
    # holding both. One such directory exists on the machine this was built
    # on: 7 transcripts from one project and 6 from another. Relocating all 13
    # would orphan the 6, violating this module's headline property while
    # reporting success.
    #
    # We migrate SELECTIVELY rather than refusing outright. Refusing would be
    # safe but leaves no way forward except moving files by hand, and the
    # correct split is exactly computable from the same ground truth the
    # encoder came from — each transcript names its own cwd. Foreign
    # transcripts are left where they are, which leaves them no worse off than
    # before (they are already under a key that is not theirs), whereas moving
    # them would strand them somewhere new.
    provenance, foreign = classify_source(source, old_cwd)
    if foreign:
        others = {c: n for c, n in provenance.items()
                  if c not in set(history_key_sources(old_cwd))}
        summary = ", ".join(f"{c} ({n})" for c, n in sorted(others.items(), key=lambda kv: -kv[1]))
        out["mixed_provenance"] = provenance
        out["foreign_files"] = foreign
        out["files"] = len(files) - len(foreign)
        out["detail"] = (
            f"ready to migrate {out['files']} of {len(files)} file(s); {len(foreign)} recorded "
            f"under {summary} will be LEFT IN PLACE, not relocated"
        )
    return out


def apply(old_cwd: str | Path, new_cwd: str | Path, *, prune_source: bool = False) -> dict:
    """Migrate history from *old_cwd*'s key to *new_cwd*'s. Copy, then verify.

    The copy lands in a staging directory alongside the target and is
    fingerprinted against the source before being published with a single
    rename, so an interrupted run leaves the source untouched and the target
    absent rather than half-written. ``prune_source`` removes the original
    only after that verification succeeded; the default is to keep it, because
    the cheapest possible recovery from a bad migration is the original still
    being there.
    """
    sweep_staging()
    decided = plan(old_cwd, new_cwd)
    if decided["status"] != READY:
        return decided

    source = Path(decided["source"])
    target = Path(decided["target"])
    staging = PROJECTS_DIR / f"{STAGING_PREFIX}{uuid.uuid4().hex[:12]}"

    try:
        expected = _fingerprint(source)
        shutil.copytree(source, staging, symlinks=True)
        actual = _fingerprint(staging)
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            differing = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
            shutil.rmtree(staging, ignore_errors=True)
            return {**decided, "status": ERROR,
                    "detail": "copy did not verify against the source; nothing was changed "
                              f"(missing: {len(missing)}, differing: {len(differing)})"}

        # Verification above covers the WHOLE copy; only now do the foreign
        # transcripts come back out of staging, so what publishes is exactly
        # the subset this migration claims to move. The source still holds
        # every original.
        for name in decided.get("foreign_files", []):
            (staging / name).unlink(missing_ok=True)

        # Re-check immediately before publishing: the window between plan() and
        # here is small, but a concurrent claude run in the new directory would
        # have created the target, and os.rename would then clobber it.
        if target.exists():
            shutil.rmtree(staging, ignore_errors=True)
            return {**decided, "status": TARGET_EXISTS,
                    "detail": "target history directory appeared while copying — refusing to "
                              "overwrite it; nothing was changed"}
        os.rename(staging, target)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return {**decided, "status": ERROR, "detail": f"migration failed: {exc}"}

    out = {**decided, "status": MIGRATED, "source_retained": True,
           "detail": f"copied and verified {decided['files']} file(s)"}

    # Pruning a partially-migrated source would delete precisely the
    # transcripts we deliberately declined to move — turning the safe choice
    # into the destructive one. Never prune when anything was left behind.
    if prune_source and decided.get("foreign_files"):
        out["detail"] += (
            f"; source NOT pruned — it still holds {len(decided['foreign_files'])} transcript(s) "
            f"belonging to another project"
        )
    elif prune_source:
        try:
            shutil.rmtree(source)
            out["source_retained"] = False
            out["detail"] += "; source pruned after verification"
        except OSError as exc:
            out["detail"] += f"; source could not be pruned ({exc})"
    else:
        out["detail"] += f"; source retained at {source}"
    return out


def resolve_session(session_name: str) -> dict:
    """Where a session's history is keyed vs. where the session actually is.

    ``cwd_at_launch`` is the recorded key. Reality comes from **git**, via the
    repo/branch recorded alongside it — the same "ask git, never rebuild the
    convention" rule that #837 applied to worktree paths. A session whose
    metadata predates #881, or which never ran in a repo, yields
    :data:`UNDETERMINED`: there is nothing to compare, and guessing a path
    here is precisely how a teardown reports success while acting on the wrong
    directory.
    """
    from .worktree import find_git_worktree

    metadata = load_session_metadata(session_name)
    base = {"session": session_name}

    # load_session_metadata returns {} both for "no such session" and for "a
    # session with an empty record", so distinguish them here rather than
    # telling someone who mistyped a name that their session predates #881.
    if not (CONFIG_DIR / "sessions" / session_name.split("@")[0] / "metadata.json").is_file():
        return {**base, "status": UNDETERMINED, "detail": "no such session recorded"}

    old = metadata.get("cwd_at_launch")
    if not old:
        return {**base, "status": UNDETERMINED,
                "detail": "no cwd_at_launch recorded (session predates #881)"}

    base["old_cwd"] = old
    repo = metadata.get("repo")
    if not repo or not Path(repo).is_dir():
        return {**base, "status": UNDETERMINED,
                "detail": "no live repo recorded — cannot ask git where this session now lives"}

    if metadata.get("worktree_path"):
        found = _find_worktree_cached(find_git_worktree, repo,
                                      metadata.get("branch"), Path(old).name)
        if not found:
            return {**base, "status": UNDETERMINED,
                    "detail": "git no longer knows a worktree for this session's branch"}
        new = str(found["path"])
    else:
        # Ran in the main checkout; `repo` is git's own path for it.
        new = repo

    result = {**base, **plan(old, new)}

    # Report per-conversation resumability rather than leaving "there is a
    # history directory" to be read as "your conversations will resume". The
    # recorded chain is ids agentwire minted; whether each is resumable is a
    # separate question only the transcript file answers, and the answer moves
    # with the migration.
    chain = metadata.get("conversation_ids") or []
    if chain:
        where = new if result["status"] == ALIGNED else old
        result["conversations"] = [
            {"id": cid, "resumable": resumable(cid, where)} for cid in chain
        ]
        live = sum(1 for c in result["conversations"] if c["resumable"])
        result["detail"] += f" ({live}/{len(chain)} recorded conversation(s) resumable)"
    return result


@functools.lru_cache(maxsize=256)
def _cached_worktrees(finder, repo: str, branch: str | None, name: str) -> dict | None:
    return finder(Path(repo), branch=branch, name=name)


def _find_worktree_cached(finder, repo: str, branch: str | None, name: str) -> dict | None:
    """Memoised worktree lookup, so a sweep shells out to git once per repo.

    :func:`scan` asks this for every recorded session — several hundred here —
    and each miss is a ``git worktree list`` subprocess. The cache is
    process-local and short-lived by construction (a CLI invocation), so it
    cannot serve a stale answer across runs.
    """
    try:
        return _cached_worktrees(finder, repo, branch, name)
    except TypeError:  # a finder that isn't hashable (patched in tests)
        return finder(Path(repo), branch=branch, name=name)


def known_sessions() -> list[str]:
    """Session names that have a metadata record, in stable order."""
    root = CONFIG_DIR / "sessions"
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if (d / "metadata.json").is_file())


def scan() -> list[dict]:
    """Every recorded session's history alignment.

    This is the shared read that both the dry run and an orphan check want.
    It is deliberately exported rather than inlined into the CLI so the doctor
    check being built on ``epic-871-restart-verb`` can consume it directly
    instead of growing a second, drifting implementation of the same question.
    """
    _cached_worktrees.cache_clear()
    return [resolve_session(name) for name in known_sessions()]
