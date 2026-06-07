"""Per-prompt reply inbox — the council's fan-out/collect protocol.

Layout under ``~/.agentwire/council/prompts/``::

    0003/
      prompt.md                 # fanned-out prompt text
      meta.json                 # {id, created_at, roster}
      replies/
        brain.take.md           # substantive take
        conscience.ack.md       # researching, follow-up coming
        conscience.followup-1.md  # the substantive follow-up
        gut.pass.md             # nothing to add (synthesis omits)

The reply *kind* is encoded in the filename — ``ls`` is the protocol, no
frontmatter parsing. A soul's initial round is complete once it has any of
``take|ack|pass``; later ``--take`` replies from the same soul become numbered
``followup-N`` files.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentwire.council import state

KINDS = ("take", "ack", "pass")


@dataclass
class Reply:
    soul: str
    kind: str  # take | ack | pass | followup
    text: str
    path: Path
    written_at: str

    def to_dict(self) -> dict:
        return {
            "soul": self.soul,
            "kind": self.kind,
            "text": self.text,
            "path": str(self.path),
            "written_at": self.written_at,
        }


def prompt_dir(prompt_id: int) -> Path:
    return state.PROMPTS_DIR / f"{prompt_id:04d}"


def replies_dir(prompt_id: int) -> Path:
    return prompt_dir(prompt_id) / "replies"


def create_prompt(prompt_id: int, text: str, roster: list[str]) -> Path:
    """Create the prompt dir + inbox. Must exist before any fan-out send.

    Prompt ids restart at 1 each sitting while ``prompts/`` history is kept,
    so a reused id may collide with a previous sitting's dir — stale reply
    files would corrupt the new round's completion check. Clear them.
    """
    pdir = prompt_dir(prompt_id)
    replies = pdir / "replies"
    replies.mkdir(parents=True, exist_ok=True)
    for stale in replies.glob("*.md"):
        stale.unlink()
    (pdir / "prompt.md").write_text(text)
    meta = {
        "id": prompt_id,
        "created_at": state.now_iso(),
        "roster": list(roster),
    }
    (pdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return pdir


def read_meta(prompt_id: int) -> dict:
    meta_path = prompt_dir(prompt_id) / "meta.json"
    try:
        data = json.loads(meta_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _initial_reply(prompt_id: int, soul: str) -> Path | None:
    """The soul's initial take/ack/pass file, or None if not yet filed."""
    for kind in KINDS:
        path = replies_dir(prompt_id) / f"{soul}.{kind}.md"
        if path.exists():
            return path
    return None


def write_reply(prompt_id: int, soul: str, kind: str, text: str) -> tuple[Path, bool]:
    """File a reply. Returns ``(path, is_followup)``.

    First reply from a soul lands as ``<soul>.<kind>.md``. Once an initial
    reply exists, only further ``take`` replies are allowed — they become
    ``<soul>.followup-N.md`` (the ack-and-research delivery path).
    """
    if kind not in KINDS:
        raise ValueError(f"invalid reply kind: {kind!r} (expected one of {KINDS})")
    rdir = replies_dir(prompt_id)
    if not rdir.is_dir():
        raise FileNotFoundError(f"no inbox for council prompt #{prompt_id}")

    if _initial_reply(prompt_id, soul) is None:
        path = rdir / f"{soul}.{kind}.md"
        path.write_text(text)
        return path, False

    if kind != "take":
        raise ValueError(
            f"{soul} already filed an initial reply for prompt #{prompt_id}; "
            "only follow-up takes are allowed after that"
        )
    n = 1
    while (rdir / f"{soul}.followup-{n}.md").exists():
        n += 1
    path = rdir / f"{soul}.followup-{n}.md"
    path.write_text(text)
    return path, True


def list_replies(prompt_id: int) -> list[Reply]:
    """All filed replies for a prompt, initial rounds first, then follow-ups."""
    rdir = replies_dir(prompt_id)
    if not rdir.is_dir():
        return []
    out: list[Reply] = []
    for path in sorted(rdir.glob("*.md")):
        stem_parts = path.stem.rsplit(".", 1)
        if len(stem_parts) != 2:
            continue
        soul, marker = stem_parts
        kind = "followup" if marker.startswith("followup-") else marker
        if kind not in (*KINDS, "followup"):
            continue
        written_at = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
        out.append(
            Reply(soul=soul, kind=kind, text=path.read_text(), path=path, written_at=written_at)
        )
    out.sort(key=lambda r: (r.kind == "followup", r.written_at))
    return out


def pending_souls(prompt_id: int, roster: list[str]) -> list[str]:
    """Roster souls that haven't filed their initial take/ack/pass yet."""
    return [s for s in roster if _initial_reply(prompt_id, s) is None]


def initial_round_complete(prompt_id: int, roster: list[str]) -> bool:
    return not pending_souls(prompt_id, roster)


def collect(
    prompt_id: int,
    roster: list[str],
    timeout: float = 120.0,
    poll: float = 1.0,
    wait: bool = True,
) -> dict:
    """Block until every roster soul has filed an initial reply, or timeout.

    Returns ``{prompt_id, complete, timed_out, replies, pending}``. With
    ``wait=False`` it snapshots once and returns immediately. Re-collecting a
    complete prompt returns instantly (the follow-up re-collect path).
    """
    deadline = time.monotonic() + timeout
    timed_out = False
    while True:
        pending = pending_souls(prompt_id, roster)
        if not pending or not wait:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(poll)
    return {
        "prompt_id": prompt_id,
        "complete": not pending,
        "timed_out": timed_out,
        "replies": [r.to_dict() for r in list_replies(prompt_id)],
        "pending": pending,
    }
