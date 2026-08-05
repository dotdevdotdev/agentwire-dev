"""The voice buddy's session identity — a name, without a tmux session (spike).

The buddy needs to be *addressable* by the machinery agentwire already has:
``msg send --to buddy``, ``notify_parent``, cohort enrollment, dangling-PR
detection. All of that keys off a session NAME plus the metadata record at
``~/.agentwire/sessions/<name>/metadata.json`` (#871's SSOT) — none of it
requires a tmux session to exist. So the buddy registers a record and an inbox
directory, and everything downstream works unchanged.

What is deliberately NOT recorded:

- **No conversation id.** ``conversation_ids`` is a chain of Claude Code
  conversation UUIDs minted by ``build_agent_command``. The buddy has no Claude
  conversation; writing a synthetic id there would corrupt the one store that
  is supposed to be authoritative rather than reconstructed.
- **No git identity.** ``repo``/``branch``/``worktree_path`` answer "where is
  this session working". The buddy never works in a checkout. Absent keys mean
  unknown, which is the truth.
- **No posture, no role prompt.** Those configure a Claude launch. There is no
  launch.

What IS recorded is the delivery adapter (so the inbox drain routes to the
spool instead of a pane) and ``kind: "voice_layer"``, so anything walking the
session store can tell at a glance that this record does not describe an agent.
"""

from __future__ import annotations

import datetime
import json
import re

from .. import core
from . import delivery

#: Session-record marker. Anything reading the session store can use this to
#: tell "not an agent session" without inferring it from missing keys.
KIND = "voice_layer"

#: The ROLE axis value. Not orchestrator/worker/reviewer — the buddy is not in
#: the topology at all (see the harness boundary in docs/wiki/voice-layer.md).
ROLE = "buddy"

DEFAULT_NAME = "buddy"

# Deliberately tighter than inbox's `_SESSION_RE`: no `/`, so the buddy's state
# directory can never nest, and no `@`, which means "on another machine".
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BuddyError(Exception):
    """A buddy identity operation could not be completed."""


def validate_name(name: str) -> str:
    if not name or not _NAME_RE.match(name) or ".." in name:
        raise BuddyError(
            f"invalid buddy name: {name!r} "
            "(letters, digits, dot, dash and underscore; must not start with a separator)"
        )
    return name


def inbox_dir(name: str):
    """The buddy's message inbox — the same layout every session's inbox uses."""
    return core.CONFIG_DIR / "inbox" / validate_name(name)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def register(name: str = DEFAULT_NAME, *, model: str = "", voice: str = "") -> dict:
    """Create (or refresh) the buddy's identity. Idempotent.

    Merge-preserving like ``record_session_launch``: re-registering keeps
    ``created_at`` and anything else already on the record.
    """
    validate_name(name)
    metadata = core.load_session_metadata(name)

    if metadata and metadata.get("kind") != KIND:
        raise BuddyError(
            f"'{name}' already exists as a real session record "
            f"(kind={metadata.get('kind') or 'agent session'}) — refusing to overwrite it. "
            "Pick a different buddy name."
        )

    metadata.update({
        "kind": KIND,
        "role": ROLE,
        delivery.DELIVERY_KEY: delivery.VOICE_ADAPTER,
        "registered_at": _now(),
    })
    if model:
        metadata["realtime_model"] = model
    if voice:
        metadata["realtime_voice"] = voice
    metadata.setdefault("created_at", _now())

    core.store_session_metadata(name, metadata)
    inbox_dir(name).mkdir(parents=True, exist_ok=True)
    delivery.session_state_dir(name).mkdir(parents=True, exist_ok=True)
    return metadata


def is_registered(name: str) -> bool:
    return core.load_session_metadata(validate_name(name)).get("kind") == KIND


def unregister(name: str = DEFAULT_NAME, *, purge: bool = False) -> dict:
    """Remove the buddy's identity record.

    Refuses to touch anything that isn't a voice-layer record. ``purge`` also
    drops the spool, the cursor and any pending inbox mail — without it, mail
    queued for the buddy is left on disk rather than silently destroyed.
    """
    validate_name(name)
    metadata = core.load_session_metadata(name)
    if not metadata:
        raise BuddyError(f"no record for '{name}'")
    if metadata.get("kind") != KIND:
        raise BuddyError(f"'{name}' is not a voice-layer record — refusing to remove it")

    removed = {"metadata": False, "spool": False, "cursor": False, "pending": 0}

    meta_file = delivery.session_state_dir(name) / "metadata.json"
    if meta_file.exists():
        meta_file.unlink()
        removed["metadata"] = True

    if purge:
        for key, path in (("spool", delivery.spool_path(name)),
                          ("cursor", delivery.cursor_path(name))):
            if path.exists():
                path.unlink()
                removed[key] = True
        box = inbox_dir(name)
        if box.exists():
            for entry in box.glob("*.json"):
                entry.unlink()
                removed["pending"] += 1

    return removed


def status(name: str = DEFAULT_NAME) -> dict:
    """Everything the CLI and the voice layer need to report about the buddy."""
    validate_name(name)
    metadata = core.load_session_metadata(name)
    registered = metadata.get("kind") == KIND
    box = inbox_dir(name)
    pending = len(list(box.glob("*.json"))) if box.exists() else 0
    return {
        "name": name,
        "registered": registered,
        "kind": metadata.get("kind"),
        "role": metadata.get("role"),
        "delivery": metadata.get(delivery.DELIVERY_KEY),
        "registered_at": metadata.get("registered_at"),
        "realtime_model": metadata.get("realtime_model"),
        "inbox_dir": str(box),
        "spool_path": str(delivery.spool_path(name)),
        "pending": pending,
        "unread": delivery.unread_count(name) if registered else 0,
    }


def list_buddies() -> list[dict]:
    """Every voice-layer record in the session store."""
    root = core.CONFIG_DIR / "sessions"
    if not root.exists():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        meta_file = entry / "metadata.json"
        if not (entry.is_dir() and meta_file.exists()):
            continue
        try:
            with open(meta_file, encoding="utf-8") as fh:
                metadata = json.load(fh) or {}
        except (OSError, json.JSONDecodeError):
            continue
        if metadata.get("kind") == KIND:
            found.append(status(entry.name))
    return found
