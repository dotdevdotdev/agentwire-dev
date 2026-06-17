"""Read-only board view of a sitting — the portal's council visualization.

Pure derivation from on-disk state (``sitting.json`` + ``prompts/NNNN/`` files);
no tmux, no mutation. The portal's ``/api/council/*`` endpoints and the
``council_update`` WebSocket deltas both render through here so the live fill
and the snapshot can never disagree.

The tile state machine is keyed by **soul**, not by file — a soul files at most
one initial ``take|ack|pass`` and then any number of ``followup-N`` takes
(the ack-then-research path). The derivation collapses all of a soul's files
into one tile:

    pending → acked (working…) → answered (latest take) | passed

with precedence **take > pass > ack > pending**: a terminal take/pass must never
be repainted by a late ack, and the highest-numbered followup take is the final
verdict.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentwire.council import inbox, state

# status enum the frontend styles by class. ``stalled`` is a pending soul whose
# lens session has died — only the caller (which can see tmux) can know that, so
# it's supplied via ``dead_souls`` rather than derived from files.
STATUS_PENDING = "pending"
STATUS_ACKED = "acked"
STATUS_ANSWERED = "answered"
STATUS_PASSED = "passed"
STATUS_STALLED = "stalled"

_FOLLOWUP_RE = re.compile(r"^followup-(\d+)$")


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _mtime_iso(path: Path) -> str:
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except OSError:
        return ""


def derive_tile(
    name: str, prompt_id: int, soul: str, *, dead: bool = False
) -> dict:
    """Collapse one soul's filed replies into a single board tile.

    Returns ``{soul, status, kind, verdict, filed_at}``. ``kind`` is the kind of
    the *final* verdict (``take|pass|ack`` or ``None`` while pending); ``verdict``
    is its full text (the UI clamps for display and expands on click).
    """
    rdir = inbox.replies_dir(name, prompt_id)

    take_path = rdir / f"{soul}.take.md"
    ack_path = rdir / f"{soul}.ack.md"
    pass_path = rdir / f"{soul}.pass.md"

    # Highest-numbered followup take wins as the final verdict.
    latest_followup: Path | None = None
    latest_n = 0
    if rdir.is_dir():
        for path in rdir.glob(f"{soul}.followup-*.md"):
            # filename is "<soul>.followup-N.md"; recover the marker after soul.
            marker = path.name[len(soul) + 1 : -3]  # strip "<soul>." and ".md"
            m = _FOLLOWUP_RE.match(marker)
            if not m:
                continue
            n = int(m.group(1))
            if n > latest_n:
                latest_n = n
                latest_followup = path

    # Precedence take > pass > ack > pending.
    if latest_followup is not None:
        return {
            "soul": soul,
            "status": STATUS_ANSWERED,
            "kind": "take",
            "verdict": _read(latest_followup),
            "filed_at": _mtime_iso(latest_followup),
        }
    if take_path.exists():
        return {
            "soul": soul,
            "status": STATUS_ANSWERED,
            "kind": "take",
            "verdict": _read(take_path),
            "filed_at": _mtime_iso(take_path),
        }
    if pass_path.exists():
        return {
            "soul": soul,
            "status": STATUS_PASSED,
            "kind": "pass",
            "verdict": _read(pass_path),
            "filed_at": _mtime_iso(pass_path),
        }
    if ack_path.exists():
        return {
            "soul": soul,
            "status": STATUS_ACKED,
            "kind": "ack",
            "verdict": _read(ack_path),
            "filed_at": _mtime_iso(ack_path),
        }
    return {
        "soul": soul,
        "status": STATUS_STALLED if dead else STATUS_PENDING,
        "kind": None,
        "verdict": "",
        "filed_at": "",
    }


def _is_final(status: str) -> bool:
    """The completion counter ("N of M in") counts only terminal states."""
    return status in (STATUS_ANSWERED, STATUS_PASSED)


def snapshot(
    name: str,
    prompt_id: int | None = None,
    *,
    dead_souls: set[str] | None = None,
) -> dict | None:
    """Full board state for a sitting at a given prompt (latest by default).

    Returns ``None`` if the sitting has no state on disk. The view is keyed on
    the sitting ``name``; the per-prompt ``meta.json.roster`` (falling back to
    the sitting roster) fixes both soul order and grid size — nothing is
    hardcoded to a roster of six.
    """
    sitting = state.read_sitting(name)
    if sitting is None:
        return None

    if prompt_id is None:
        prompt_id = state.latest_prompt_id(name)

    dead_souls = dead_souls or set()

    meta = inbox.read_meta(name, prompt_id) if prompt_id else {}
    roster = meta.get("roster") or list(sitting.roster)

    prompt_text = ""
    if prompt_id:
        prompt_text = _read(inbox.prompt_dir(name, prompt_id) / "prompt.md")

    tiles = [
        derive_tile(name, prompt_id, soul, dead=soul in dead_souls)
        for soul in roster
    ] if prompt_id else []

    final = sum(1 for t in tiles if _is_final(t["status"]))

    return {
        "sitting": name,
        "orchestrator": sitting.orchestrator,
        "roster": list(roster),
        "prompt_id": prompt_id,
        "prompt_ids": available_prompt_ids(name),
        "prompt_text": prompt_text,
        "created_at": meta.get("created_at", ""),
        "tiles": tiles,
        "total": len(tiles),
        "final": final,
    }


def available_prompt_ids(name: str) -> list[int]:
    """Every prompt id with a dir on disk, ascending (drives the round selector)."""
    pdir = state.prompts_dir(name)
    if not pdir.is_dir():
        return []
    ids = []
    for child in pdir.iterdir():
        if child.is_dir() and child.name.isdigit():
            ids.append(int(child.name))
    return sorted(ids)
