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
names are absent from the realtime surface BY NAME. Light writes are graded
but currently none are wired: the candidates (desktop arrangement, tab
tracking) have no CLI verb, and the voice layer dispatches only through the
CLI (see ``tools.py``'s module docstring for why).
"""

from __future__ import annotations

#: Tier 1 — observe only. Direct dispatch, expand freely.
TIER_READ = frozenset({
    "sessions_list", "sessions_context", "session_output", "session_info",
    "diff", "panes_list", "pane_output",
    "worktree_list", "worktree_status",
    "scheduler_status", "scheduler_board", "scheduler_live",
    "scheduler_events", "scheduler_history", "scheduler_report",
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
    "session_kill", "pane_kill", "pane_detach",
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
    # (e) mutates infrastructure identity
    "machine_add", "machine_remove",
})

ALL_TIERS = (TIER_READ, TIER_WRITE_LIGHT, TIER_WRITE_GATED, TIER_EXCLUDED)


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
