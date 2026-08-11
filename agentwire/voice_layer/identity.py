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

from .. import core, fleet_alerts
from . import delivery, realtime

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


def _valid_voice(voice: str) -> str:
    """:func:`realtime.validate_voice`, re-raised as a :class:`BuddyError`.

    One failure contract per module. Every caller of this module already
    catches ``BuddyError`` and nothing catches ``RealtimeError``, so letting
    the transport layer's exception escape from ``register`` would mean a
    second, uncaught contract at the only call site that has one — reachable
    the moment anything registers a buddy without pre-validating the flag.
    """
    try:
        return realtime.validate_voice(voice)
    except realtime.RealtimeError as exc:
        raise BuddyError(str(exc)) from exc


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

    Idempotent in the record it writes and, since #1017, in what the CLI SAYS
    about it — see :func:`registration_delta`, which is how the caller tells a
    fresh registration from a voice change without reading the record twice.
    """
    validate_name(name)
    voice = _valid_voice(voice)
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
    # Lease fleet alerts (#982). The buddy is the interrupt tier's consumer, so
    # it is the one recipient that wants a detector's escalation the moment it
    # fires. The lease EXPIRES on purpose: the buddy's mail is spooled rather
    # than pasted, so it never reads as "gone" the way a dead tmux session
    # does, and a permanent flag would keep filling a spool nobody reads and
    # then replay a fortnight of escalations at the next start. Renewed at
    # `buddy serve`; a bridge left running past the lease goes quiet until it
    # is restarted, which is the direction that fails safe.
    #
    # Guarded like every detector call site, and for a sharper reason here:
    # `store_session_metadata` RAISES by design (#885), so an unwritable store
    # would turn an optional extra into a failed registration — with the record
    # already written and the inbox/spool dirs below never created. The
    # alerting subsystem must not be able to break the thing it alerts about.
    try:
        fleet_alerts.subscribe(name)
    except Exception as exc:  # noqa: BLE001  # optional extra, never fatal
        fleet_alerts.log_event("subscribe_failed", session=name, error=str(exc))
    inbox_dir(name).mkdir(parents=True, exist_ok=True)
    delivery.session_state_dir(name).mkdir(parents=True, exist_ok=True)
    return metadata


def registration_delta(name: str, *, model: str = "", voice: str = "") -> dict:
    """What a :func:`register` call with these arguments would CHANGE.

    Read before the write, so ``agentwire buddy register`` can say "updated
    voice to marin" instead of re-announcing a registration that already
    happened (#1017). The full blurb names the inbox, the spool and how other
    sessions reach the buddy — true and useful exactly once; printed again over
    a one-word voice change it reads like a second identity was created.

    Returns ``{"registered": bool, "changes": {field: {"from": …, "to": …}}}``.
    ``registered`` is about the record that exists NOW, so the caller does not
    have to infer "was this new?" from an empty change set — re-registering
    with no arguments changes nothing and is still not a first registration.
    """
    validate_name(name)
    voice = _valid_voice(voice)
    before = core.load_session_metadata(name)
    changes = {}
    for field, key, wanted in (
        ("model", "realtime_model", model),
        ("voice", "realtime_voice", voice),
    ):
        if wanted and before.get(key) != wanted:
            changes[field] = {"from": before.get(key), "to": wanted}
    return {"registered": before.get("kind") == KIND, "changes": changes}


def registered_voice(name: str) -> str:
    """The voice recorded for *name*, or ``""`` if it has none.

    A recorded voice that nothing reads is a setting that silently does not
    work, which is what ``register --voice`` was before #1017: ``serve`` and
    ``mint`` both fell straight through to :data:`realtime.DEFAULT_VOICE`.
    """
    recorded = core.load_session_metadata(validate_name(name)).get("realtime_voice")
    return recorded if isinstance(recorded, str) else ""


def resolve_voice(name: str, requested: str = "") -> str:
    """The voice to actually use: explicit → recorded → default.

    Tolerates an unregistered name on purpose — the bridge is constructed
    before anything guarantees a record exists, and refusing here would turn a
    missing record into a bridge that will not start.
    """
    explicit = _valid_voice(requested)
    if explicit:
        return explicit
    try:
        recorded = _valid_voice(registered_voice(name))
    except BuddyError:
        # A record carrying a voice we no longer accept (an id retired
        # upstream) must not wedge the bridge — fall through to the default.
        recorded = ""
    return recorded or realtime.DEFAULT_VOICE


def set_voice(name: str, voice: str) -> str:
    """Record *voice* as the buddy's voice, so it survives the next ``serve``.

    Raises rather than warning on an unknown voice: this is reached from the
    page's picker, whose options come from :data:`realtime.VOICES`, so anything
    else is a caller bug rather than a typo the owner made.
    """
    validate_name(name)
    voice = _valid_voice(voice)
    if not voice:
        raise BuddyError("set_voice needs a voice")
    metadata = core.load_session_metadata(name)
    if metadata.get("kind") != KIND:
        raise BuddyError(_not_a_buddy(name))
    metadata["realtime_voice"] = voice
    core.store_session_metadata(name, metadata)
    return voice


def _not_a_buddy(name: str) -> str:
    return f"'{name}' is not a registered voice buddy"


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

    meta_file = core.session_metadata_path(name)
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
        # Reported because it is now READ by serve/mint (#1017). A setting the
        # status page cannot show is one the owner has to guess at.
        "realtime_voice": metadata.get("realtime_voice"),
        "inbox_dir": str(box),
        "spool_path": str(delivery.spool_path(name)),
        "pending": pending,
        "unread": delivery.unread_count(name) if registered else 0,
    }


def list_buddies() -> list[dict]:
    """Every voice-layer record in the session store."""
    root = core.sessions_dir()
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
