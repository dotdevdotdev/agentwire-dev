"""The tier audit: every agentwire MCP capability, placed by a stated rule (#966).

The buddy's surface used to be whatever a spike demo needed. This module is
the replacement: EVERY tool name in ``agentwire/mcp_*.py`` appears in exactly
one tier below, a test parses those modules and fails the moment a new tool
ships untiered, and the rules are written down so the next tool's tier is
DERIVABLE rather than argued.

The rule
========

Classify by what the action touches, in order — the first clause that applies
wins:

**EXCLUDED (tier 3)** — never reachable from the buddy, by design and not by
omission. A capability is excluded when it:

(a) **creates or drives an agent session** — ``session_create``,
    ``worktree_create``, ``pane_spawn``, ``session_fork``, ``session_send``
    and kin. The buddy is an I/O layer, not a harness; #730 settled that
    there is exactly one coding harness, and a buddy that can start or steer
    sessions is a second one. ``session_send``/``pane_send`` paste into a live
    prompt and press Enter — forcibly driving a session — where ``msg_send``
    is the polite, guarded channel: the recipient acts on it inside its own
    damage-control, posture and routing. That is why msg is a graded write and
    send is excluded.

    The clause keys on the DISPATCH PATH, not the tool's name: anything that
    reaches ``agentwire ensure`` — ``task_run``, ``scheduler_run`` — is (a),
    because ensure creates the session when it is missing and then drives it
    with prompts to completion. The tempting carve-out ("the task content is
    owner-authored, in the protected ``.agentwire.tasks.yml``, behind a
    nonce") was considered and REJECTED: authorship of the prompt does not
    change who instantiated and drove the session, and the exclusion is not
    "this is expensive" but "this is not this layer's job at all". A test
    walks every tool's argv into the CLI call graph and fails any tier-1/2
    tool whose path can create a session, so the next ensure-shaped verb
    cannot land under an innocuous name.
(b) **is another output channel to the owner** — ``say``, ``notify_user``,
    the listen/transcribe family. The buddy IS the voice channel; a second
    path that speaks or toasts is #950 (two paths racing to speak) in
    different clothing.
(c) **publishes outward** — ``email_send``, ``quo_send``. An outward send
    bypasses every session guard; the handoff route (message a real session,
    which composes and sends under its own hooks) exists precisely for this.
(d) **authors work product** — ``handoff_init``/``render``,
    ``desktop_write_artifact``. The buddy never writes code, content or
    artifacts; producing work makes it a place work happens.
(e) **mutates infrastructure identity** — ``machine_add``/``remove``.

Two rulings from the wave-2 review (#979), recorded here because a tier move
with no reason is re-argued at the next reading:

- **``scheduler_report`` is EXCLUDED, not a read** — clauses (d) and (b).
  The name says report and the return value says summary, but the call writes
  an HTML artifact into ``~/.agentwire/artifacts/`` and, with ``artifact=True``,
  pushes a click-to-open portal notification at the owner. That is authored
  work product plus a second output channel. Nothing about it is undone by one
  action of the same kind, and "expand reads freely" would have wired it
  confirm-free on the strength of the verb. The buddy answers scheduler
  questions from ``scheduler_history``/``board``, which observe and no more.
- **``pane_detach`` is EXCLUDED, not gated** — clause (a). Its own docstring
  says the target session is "created if doesn't exist", so a mis-heard target
  name does not misfire a move: it INSTANTIATES a session, one with no #871
  metadata record behind it and therefore no conversation identity, no
  recorded role, and nothing for ``restart`` to regenerate. The dispatch-path
  analyzer cannot see this one (no ``build_agent_command`` on that path), so
  this entry is the only guard, and a nonce is the wrong guard: the harness
  boundary is not a thing the owner should be able to approve their way past.

**Remote ``name@machine`` targets are out of scope (owner ruling, 2026-08-09),
and the gate is LIVENESS, not the ``@`` character.** The syntax was
half-supported and
wrong in three directions at once: ``inbox.enqueue`` keyed an inbox dir on the
raw string, ``outbox.delivery_state`` stripped the suffix and interrogated the
LOCAL inbox, and ``write_tools._require_live`` checked the bare half against
LOCAL tmux — refusing a live remote session with a confidently false "nothing
is listening". Every layer now asks about the WHOLE name.

The first attempt at enforcing the ruling refused any name containing ``@``,
and that was itself a false statement: ``@`` does not mean remote. tmux accepts
it verbatim (only ``.`` and ``:`` are rewritten, #878) and ``inbox._SESSION_RE``
admits it, so ``ops@edge`` is a creatable, addressable LOCAL session that the
buddy told the owner was unreachable — a confident falsehood with no move from
it, which is the expensive failure in a channel with no screen. So
``tools._session_arg`` validates the SHAPE first (a garbled name containing an
``@`` is a mis-transcription and gets the mis-transcription answer), then
consults ``inbox.live_sessions()``: a whole name local tmux reports live is
local by demonstration and is allowed. What that refuses is exactly a name
nothing local answers to — every genuinely remote target — stated as the one
thing measured ("no live session called X on this machine") rather than as a
diagnosis of where it lives. An unreachable tmux proves nothing and so refuses
nothing (spec §5).

``core.session_metadata_path`` still strips ``@`` — that is the store's own
keying rule (#899/#988) and is unrelated to what this layer admits. Reaching a
remote session remains its own reviewed slice: a remote liveness probe, remote
inbox interrogation, and tests for both.

**Reads (tier 1)** — anything that only observes. Expand freely: a read the
buddy lacks is just a question it has to deflect.

**Writes, graded by the cost of the worst WRONG execution** — voice adds
mis-transcription as a first-class failure mode ("kill the worker" /
"kill the worktree" differ by one phoneme), so the grade keys on what a wrong
target or wrong verb costs, not on how scary the verb sounds:

- **light (confirm-free)** — the wrong execution is undone by ONE action of
  the same kind, destroys no state and no work, and causes no agent or human
  to act: window arrangement, pane focus, the buddy's own bookkeeping. A
  nonce here is not merely unnecessary — it is corrosive: a confirm phrase
  for opening a window trains the owner to speak the nonce reflexively, and a
  reflexive nonce is a dead gate (price BOTH halves of a guard).
- **gated (nonce, through the confirm spine)** — everything else: the write
  causes another agent or human to act, changes durable state, or destroys
  something (a killed session, a purged queue, a removed worktree cannot be
  un-done by one equal action). Destructive writes stay in this grade rather
  than a third ceremony tier: the spine's spoken read-back of the exact
  target plus the nonce IS the mis-transcription defence, and a third tier
  would just be a second nonce.

Tiering is capability classification; WIRING is a separate, smaller set.
``tools.READ_ONLY_TOOLS`` and ``write_tools.WRITE_SPECS`` hold what is live;
everything live must map into tier 1 or 2, and a test asserts the excluded
names are absent from the realtime surface BY NAME.

**Voice-native tools are ruled here too** (#979). The tier sets above are keyed
on MCP capability names, and for a while the audit swept exactly those — a
namespace that is not the exposed surface. ``buddy_inbox``, ``buddy_sent`` and
``fleet_pull_requests`` have no MCP capability behind them at all, so "every
tool appears in exactly one tier" was true of tools nobody had graded.
:data:`TOOL_CAPABILITY` maps each wired tool to the capability it exposes and
:data:`VOICE_NATIVE` carries a written grade for the ones that map to none;
:func:`unruled_tools` is what the audit calls, so a new voice-native tool is
red until someone rules on it.

**That map is checked against reality, not just against itself.** Every leg
that reads :data:`TOOL_CAPABILITY` believes it, so a wrong entry —
``fleet_session_output`` pointed at ``sessions_list`` — left the whole suite
green and made "a wired tool's tier is derivable" mean "a hand-written map
nothing checks", which is the same over-claim this module polices one level
up. The audit now runs each read tool with the CLI stubbed and compares the
argv it really builds against the argv the mapped MCP capability builds. One
entry cannot be corroborated that way: ``wiki_query`` builds no extractable
argv, so ``fleet_wiki_search``'s mapping rests on reading it. That exemption is
allowed only for capabilities the analyzer already records as unanalyzable, and
it is stated here rather than left as a silent pass.

One light write IS wired: ``buddy_inbox(ack=true)`` advances the buddy's own
read cursor. Light because the message itself is untouched — the same tool with
``unread_only=false`` reads it straight back — so the worst wrong execution
loses a read marker, not mail, and a nonce on "what's in my inbox" is the
reflex-training a light grade exists to avoid. The other candidates (desktop
arrangement, tab tracking) remain unwired: they have no CLI verb, and the voice
layer dispatches only through the CLI (see ``tools.py``'s module docstring).
"""

from __future__ import annotations

#: Tier 1 — observe only. Direct dispatch, expand freely.
TIER_READ = frozenset({
    "sessions_list", "sessions_context", "session_output", "session_info",
    "diff", "panes_list", "pane_output",
    "worktree_list", "worktree_status",
    "scheduler_status", "scheduler_board", "scheduler_live",
    "scheduler_events", "scheduler_history",
    "task_list", "task_show", "task_validate",
    "machines_list", "services_list", "services_status",
    "history_list", "history_show",
    "lock_list", "portal_status", "tts_status", "stt_status",
    "network_status", "tunnels_status",
    "council_status", "council_list",
    "msg_inbox", "msg_dead", "research_dir",
    "projects_list", "roles_list", "role_show",
    "wiki_query", "wiki_lint", "wiki_status",
    "channels_list", "handoff_list", "chrome_tab_list",
    "voices_list", "desktop_windows_list", "scratchpad_list",
})

#: Tier 2, light grade — ephemeral presentation or the buddy's own
#: bookkeeping; wrong execution is undone by one equal action. Confirm-free
#: when wired (none are yet — no CLI verb; see the module docstring).
TIER_WRITE_LIGHT = frozenset({
    "desktop_open_session", "desktop_open_panel", "desktop_open_artifact",
    "desktop_close_window", "desktop_focus_window", "desktop_tile_window",
    "desktop_minimize_all", "desktop_collage", "desktop_layout",
    "chrome_tab_track", "chrome_tab_untrack",
    "scratchpad_add", "pane_jump", "pane_resize",
})

#: Tier 2, gated grade — causes agents/humans to act, changes durable state,
#: or destroys something. Only ever reachable through the confirm spine.
TIER_WRITE_GATED = frozenset({
    "msg_send",
    "session_kill", "pane_kill",
    "worktree_remove", "worktree_prune",
    "lock_clean", "lock_remove",
    # msg_pull reads AND REMOVES another session's ingest messages (it takes
    # a session param). The name reads like a fetch; the effect is a consume
    # with no one-action undo — a mis-heard target silently destroys a
    # Briefing-Mode anchor's queued pointers, and nothing tells anyone.
    "msg_pull", "msg_purge", "msg_flush",
    "scheduler_enable", "scheduler_disable",
    "tunnels_up", "tunnels_down",
})

#: Tier 3 — permanently excluded, by the lettered clauses in the module
#: docstring. A DESIGN DECISION, not an oversight.
TIER_EXCLUDED = frozenset({
    # (a) creates or drives an agent session — the harness boundary (#730).
    # task_run and scheduler_run dispatch through `agentwire ensure`, which
    # creates the session if missing and drives it with prompts — clause (a)
    # by dispatch path, whatever the verb sounds like (see the docstring for
    # the rejected owner-authored-content carve-out).
    "session_create", "session_recreate", "session_fork",
    "session_send", "session_send_keys",
    "pane_spawn", "pane_send", "pane_split",
    # pane_detach's target session is "created if doesn't exist" — clause (a)
    # under a name that reads like a move (#979).
    "pane_detach",
    "worktree_create", "history_resume", "wait_children",
    "task_run", "scheduler_run",
    "council_start", "council_stop", "council_ask",
    "council_collect", "council_minutes",
    # (b) another output channel to the owner (#950)
    "say", "transcribe", "listen_start", "listen_stop", "listen_cancel",
    "notify_user", "notify_parent", "notify_event",
    # (c) publishes outward, past every session guard
    "email_send", "quo_send",
    # (d) authors work product
    "handoff_init", "handoff_render", "desktop_write_artifact",
    # scheduler_report writes an HTML artifact and can push a portal
    # notification — (d) plus (b), whatever the verb sounds like (#979).
    "scheduler_report",
    # (e) mutates infrastructure identity
    "machine_add", "machine_remove",
})

ALL_TIERS = (TIER_READ, TIER_WRITE_LIGHT, TIER_WRITE_GATED, TIER_EXCLUDED)

#: Wired voice tool → the MCP capabilities it exposes, so a WIRED tool's tier
#: is derivable rather than assumed from its ``fleet_`` prefix. Writes are
#: keyed by their ``send_<spec>`` name — the step that executes.
TOOL_CAPABILITY: dict[str, tuple[str, ...]] = {
    "fleet_sessions": ("sessions_list",),
    "fleet_worktrees": ("worktree_list",),
    "fleet_dangling": ("worktree_list",),
    "fleet_scheduler": ("scheduler_board",),
    "fleet_projects": ("projects_list",),
    "fleet_dead_letters": ("msg_dead",),
    "fleet_session_output": ("session_output",),
    "fleet_session_info": ("session_info",),
    "fleet_scheduler_status": ("scheduler_status",),
    "fleet_scheduler_history": ("scheduler_history",),
    "fleet_scheduler_live": ("scheduler_live",),
    "fleet_tasks": ("task_list",),
    "fleet_machines": ("machines_list",),
    "fleet_services": ("services_status",),
    "fleet_history": ("history_list",),
    "fleet_locks": ("lock_list",),
    "fleet_portal": ("portal_status",),
    "fleet_councils": ("council_list",),
    "fleet_wiki_search": ("wiki_query",),
    "fleet_session_inbox": ("msg_inbox",),
    "fleet_roles": ("roles_list",),
    "fleet_network": ("network_status",),
    "fleet_voice_health": ("tts_status", "stt_status"),
    "send_session_message": ("msg_send",),
}

#: Wired tools with NO MCP capability behind them. The tier sets cannot grade
#: these — nothing to look up — so the grade is written out here, with its
#: reason, and :func:`unruled_tools` makes an ungraded one fail the audit.
VOICE_NATIVE: dict[str, dict] = {
    "fleet_pull_requests": {
        "grade": "read",
        "ruling": (
            "Runs `gh pr list --json` in a subprocess: no agentwire capability "
            "exists for it, and it only observes. Read by the tier-1 rule. The "
            "repo is validated to `owner/name` and never defaulted from a cwd, "
            "because the buddy has no checkout to be wrong about."
        ),
    },
    "buddy_inbox": {
        "grade": "write_light",
        "ruling": (
            "Reads the buddy's own spool, and with ack=true advances its read "
            "cursor — a mutation, from the read-only allowlist. Light, not "
            "gated: the message is untouched, `unread_only=false` reads it "
            "straight back, and the worst wrong execution loses a read marker "
            "rather than mail. A nonce on 'what's in my inbox' would train the "
            "reflex that makes the gated nonce worthless."
        ),
    },
    "buddy_sent": {
        "grade": "read",
        "ruling": (
            "Reads the buddy's own outbox and computes delivery state from the "
            "recipient's inbox. Observes only; writes nothing (#958)."
        ),
    },
}


def unruled_tools(names) -> dict[str, str]:
    """Wired tool names with neither a tier nor a voice-native ruling.

    The audit's entry point (#979/5). Sweeping ``@mcp.tool`` names proves
    things about a namespace that is not the exposed surface; this sweeps what
    is actually WIRED and demands each name resolve to a written grade.
    """
    unruled: dict[str, str] = {}
    for name in names:
        native = VOICE_NATIVE.get(name)
        if native is not None:
            if native.get("grade") and native.get("ruling"):
                continue
            unruled[name] = "voice-native entry with no grade or no ruling"
            continue
        capabilities = TOOL_CAPABILITY.get(name)
        if not capabilities:
            unruled[name] = "no capability mapping and no voice-native ruling"
            continue
        untiered = [c for c in capabilities if tier_of(c) == "untiered"]
        if untiered:
            unruled[name] = f"maps to untiered capabilities: {untiered}"
    return unruled


def tier_of(name: str) -> str:
    """The tier of one MCP capability name, or ``"untiered"``."""
    if name in TIER_READ:
        return "read"
    if name in TIER_WRITE_LIGHT:
        return "write_light"
    if name in TIER_WRITE_GATED:
        return "write_gated"
    if name in TIER_EXCLUDED:
        return "excluded"
    return "untiered"
