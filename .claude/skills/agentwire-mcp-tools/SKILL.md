---
name: agentwire-mcp-tools
description: Reference for the `mcp__agentwire__*` MCP tools — session/pane management, voice/TTS, tasks/locks, channels (email + quo, outbound-only), machines/tunnels/network, history/roles/projects, scheduler, desktop UI, notifications. Use when agents inside agentwire sessions need to pick the right MCP tool instead of shelling out to the `agentwire` CLI.
---

# AgentWire MCP Tools

**Agents running in agentwire sessions should use MCP tools instead of CLI commands.** The agentwire MCP server provides tools that wrap CLI functionality. Use these instead of `Bash: agentwire <cmd>`.

## Session Management (13 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire list` | `sessions_list()` |
| `agentwire list --context` | `sessions_context(session="")` — context headroom (remaining %); LOW = bloated. Observe-only (#442). |
| `agentwire new -s name` | `session_create(name="...")` |
| `agentwire send -s name "msg"` | `session_send(session="...", message="...")` |
| `agentwire msg send --to name "msg"` | `msg_send(to="...", text="...", kind="note", ref="")` |
| `agentwire msg inbox -s name` | `msg_inbox(session="...")` |
| `agentwire msg pull -s name` | `msg_pull(session="...")` |
| `agentwire msg flush -s name` | `msg_flush(session="...")` |
| `agentwire msg dead -s name` | `msg_dead(session="...")` |
| `agentwire output -s name` | `session_output(session="...")` |
| `agentwire info -s name` | `session_info(session="...")` |
| `agentwire kill -s name` | `session_kill(session="...")` |
| `agentwire send-keys -s name key1 key2` | `session_send_keys(session="...", keys=["..."])` |
| `agentwire recreate -s name` | `session_recreate(session="...")` |
| `agentwire fork -s name -t project/branch` | `session_fork(session="...", target="...")` |
| `agentwire fork -s name -t project/branch --commit abc` | `session_fork(session="...", target="...", commit="abc")` |
| `agentwire wait --children` | `wait_children(timeout=300)` — block on the children YOU spawned; see below |

**`session_send` vs `msg_send`:** `session_send` pastes + Enter **immediately** — and clobbers a human's half-typed draft if the box is occupied. Use it only when you must forcibly drive a session *now*. `msg_send` is the polite path: it queues a typed message into a file inbox and the watchdog injects it only when the recipient's box is empty and the pane is safe (≤60s). For routine peer updates ("PR drafted", "picking up the footer"), prefer `msg_send`. `kind` ∈ note|done|request|escalation|**ingest**|**voice**|**idle** (idle = the idle hook's synthetic placeholder for a child that went quiet without reporting, #952 — never hand-send it); `to="@all"` broadcasts to live agent sessions except you. See wiki sessions/messaging.md.

**Passive `ingest` messages (Briefing Mode):** `kind="ingest"` is **never auto-delivered** — it lands silently and waits until the recipient calls `msg_pull()`. Use it for "output ready" awareness signals that must NOT drive the recipient into a turn. Pair with `ref="<path>"` so the pointer is machine-readable. `msg_flush` forces a (still-gated) drain of the *driving* queue; it never touches passive messages.

**`wait_children` after any fan-out.** If you spawn child sessions, do NOT just "wait" — idle ≠ done for a parent with outstanding children, and the idle handler will prompt you for a roll-up you can't write and then `/exit` you while they work (#852). `wait_children()` blocks inside the tool call (not idle), collects each child's report from your inbox, tears the child down, and names any that never reported. Bounded and re-callable — call it again if it comes back with children still pending. Enrollment is automatic on `session_create` / `worktree_create`, and survives a cross-project spawn even though the `created_by` link does not. See wiki sessions/fan-out-cohorts.md.

**Note:** `session_create` (via `agentwire new`) records the calling session as the new session's creator ONLY when `project_dir` is the same project you're already running in — interactive prompts (permission/plan/AskUserQuestion) in that child then route back to you as `[PROMPT from ...]` messages. Spawning into a genuinely different project defaults to a standalone root instead of nesting under you (#715); pass `created_by=<name>` to force a specific parent regardless of project (e.g. for a closely related project). Answer routed prompts with `agentwire prompts answer -s <session> --expect <hash> <key>` via Bash (guarded compare-and-send), NEVER with raw `session_send_keys` — a late keystroke races the portal and can type into the child's input or abort its turn.

## Pane Management (9 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire spawn --roles worker` | `pane_spawn(roles="worker")` |
| `agentwire send --pane 1 "msg"` | `pane_send(pane=1, message="...")` |
| `agentwire output --pane 1` | `pane_output(pane=1)` |
| `agentwire kill --pane 1` | `pane_kill(pane=1)` |
| `agentwire list` (in tmux) | `panes_list()` |
| `agentwire split -n 2` | `pane_split(count=2)` |
| `agentwire detach --pane 1 -s target` | `pane_detach(session="src", pane=1, target="target")` |
| `agentwire jump --pane 1` | `pane_jump(pane=1)` |
| `agentwire resize` | `pane_resize()` |

## Voice & TTS (6 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire say "text"` | `say(text="...", display="...")` |
| `agentwire notify-parent "text"` | `notify_parent(text="...", session="...")` |
| `agentwire notify-user "text"` | `notify_user(text="...", session="...", priority="normal")` |
| `agentwire notify-event EVENT` | `notify_event(event="...", session="...")` |
| `agentwire listen start` | `listen_start()` |
| `agentwire listen stop` | `listen_stop()` |
| `agentwire listen cancel` | `listen_cancel()` |
| (portal API) | `transcribe(audio_base64="...", format="webm")` |
| `agentwire tts voices` | `voices_list()` |

## Tasks & Locks (7 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire ensure -s x --task y` | `task_run(session="x", task="y")` |
| `agentwire task list x` | `task_list(session="x")` |
| `agentwire task show x/y` | `task_show(session="x", task="y")` |
| `agentwire task validate x/y` | `task_validate(session="x", task="y")` |
| `agentwire lock list` | `lock_list()` |
| `agentwire lock clean` | `lock_clean()` |
| `agentwire lock remove session` | `lock_remove(session="...")` |

## Operations (12 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire projects list` | `projects_list()` |
| `agentwire roles list` | `roles_list()` |
| `agentwire roles show name` | `role_show(name="...")` |
| `agentwire machine list` | `machines_list()` |
| `agentwire machine add id --host h --user u` | `machine_add(machine_id="...", host="...", user="...")` |
| `agentwire machine remove id` | `machine_remove(machine_id="...")` |
| `agentwire history list` | `history_list()` |
| `agentwire history show id` | `history_show(session_id="...")` |
| `agentwire history resume id -p path` | `history_resume(session_id="...", project="...")` |
| `agentwire handoff init --title hint` | `handoff_init(title="...")` |
| `agentwire handoff render <bundle-dir>` | `handoff_render(bundle_dir="...", story=True)` |
| `agentwire handoff list` | `handoff_list()` |

## Channels (outbound-only)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire channels list` | `channels_list()` |
| `agentwire email --body "..." --to addr` | `email_send(body="...", to="...", attachments=["..."], plain_text=False)` |
| `agentwire quo --body "..." --to "+1..."` | `quo_send(body="...", to="+1...")` |

## Notifications & Network (5 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire notify-event EVENT` | `notify_event(event="...")` |
| `agentwire tunnels up` | `tunnels_up()` |
| `agentwire tunnels down` | `tunnels_down()` |
| `agentwire tunnels status` | `tunnels_status()` |
| `agentwire network status` | `network_status()` |

## Status (3 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire portal status` | `portal_status()` |
| `agentwire tts status` | `tts_status()` |
| `agentwire stt status` | `stt_status()` |

## Scratch Pad (2 tools)

The user's shared notes drawer (portal, Alt+N) — server-backed, synced live to
every connected client. Use `scratchpad_add` when the user says "save this",
"note that", or "add to my notes".

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire scratchpad add "text" --source s` | `scratchpad_add(text, source)` |
| `agentwire scratchpad list` | `scratchpad_list()` |

## Custom Services (2 tools)

Registered long-running sessions (`services.custom` in config + the built-in
notifications bridge). Autostarted on portal launch; health-checked and
restarted by the portal watchdog. Start/stop is CLI-only (`agentwire services
up|down`) — the MCP surface is read-only introspection.

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire services list` | `services_list()` |
| `agentwire services status` | `services_status()` |

## Scheduler (8 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire scheduler status` | `scheduler_status()` |
| `agentwire scheduler board` | `scheduler_board()` |
| `agentwire scheduler live --json` | `scheduler_live()` |
| `agentwire scheduler events --json` | `scheduler_events(tail=20, task="")` |
| `agentwire scheduler run task` | `scheduler_run(task="...")` |
| `agentwire scheduler report --since 8h` | `scheduler_report(since="8h", artifact=False)` |
| `agentwire scheduler enable task` | `scheduler_enable(task="...")` |
| `agentwire scheduler disable task` | `scheduler_disable(task="...")` |
| `agentwire scheduler history` | `scheduler_history(limit=20)` |

## Desktop/Portal UI (12 tools)

| Action | MCP Tool |
|--------|----------|
| List open windows | `desktop_windows_list()` |
| Preview windows in a collage overlay | `desktop_collage()` |
| Open session window | `desktop_open_session(session="...", mode="monitor")` |
| Open panel | `desktop_open_panel(panel_type="sessions")` |
| Announce artifact, click-to-open (URL/file) | `desktop_open_artifact(url="...", title="...")` — notification, never steals focus (#817) |
| Write HTML + announce as artifact | `desktop_write_artifact(filename="...", html_content="...", title="...")` |
| Post toast to the human | `notify_user(text="...", session="...", priority="normal")` (safe markdown: bold, [links](url), line breaks) |
| Close window | `desktop_close_window(window_id="...")` |
| Focus window | `desktop_focus_window(window_id="...")` |
| Tile window | `desktop_tile_window(window_id="...", zone="left")` |
| Minimize all | `desktop_minimize_all()` |
| Multi-window layout | `desktop_layout(windows=[{id: "...", zone: "left"}])` |

**~92 tools total** (sessions, panes, voice, tasks, channels, scheduler, desktop UI, handoffs). When to use CLI vs MCP:
- **MCP tools** — Agents in sessions (orchestrators, workers)
- **CLI commands** — Humans, shell scripts, automation outside of agent sessions

**Worktree lifecycle (MCP):** `worktree_create(name, project_dir, roles, base, prompt, created_by, kind)` spawns a worktree session (new branch + checkout + tmux session, optionally seeded with `prompt`); the new session is only parented to you when `project_dir` is your own project (#715) — pass `created_by` to force a specific parent for a closely related project instead. `kind` is the ROLE axis (#716, independent of topology) — `"worker"` (default: safety-railed isolation/verify/draft-PR/notify), `"orchestrator"` (durable, replaceable-persona project window; roots by default unless `created_by` is given), or `"reviewer"` (#827: safety-railed the other way — adversarially reviews a sibling's PR, never opens/merges its own; stays parented like worker, not rooted). `worktree_status` / `worktree_list(project_dir, check_dangling)` (read-only git status; `check_dangling=True` also flags LIVE **worker**-kind sessions with an OPEN PR and no live parent — orchestrator-kind entries are excluded since a self-rooted orchestrator with an open PR is its normal healthy lifecycle; distinct from the "orphan" state, a dead session with a disk remnant). `worktree_remove(name, project_dir, keep_branch, force_delete_branch, close_pr_branch)` is an ATOMIC teardown — kill session + force-remove the git worktree + delete the branch (local + remote, once confirmed merged via `gh pr view` + a headRefOid tip-SHA check, or a git ancestor fallback) + unregister; it fails LOUDLY (never silently "unregisters" an orphan) if the directory can't actually be cleared, and never needs `main` checked out anywhere. `force_delete_branch` overrides the merge-state check for plain unmerged work, but NOT an OPEN PR on that branch (#756) — force-deleting the remote head branch of an open PR would silently close it, so that's refused (naming the PR#) unless `close_pr_branch` is also set. `worktree_prune(project_dir, gc_merged)` drops stale registry entries (dir already gone); `gc_merged=True` additionally tears down any still-present entry whose branch is confirmed merged. Worker *panes* via `pane_spawn` still share the orchestrator's working dir — use those for quick subtasks, worktree sessions for isolated parallel work.

**Chrome tab bookkeeping (MCP):** `chrome_tab_track(tab_id, url, session)` / `chrome_tab_untrack(tab_id, session)` — agentwire can't call `tabs_close_mcp` itself (that server runs inside the calling agent's own client), so this is pure tracking: a worktree session records a verification tab right after `tabs_create_mcp`, then closes it + untracks before finishing. `worktree_remove`/`worktree_prune --gc-merged` check this during teardown and surface any tab a session never closed, so the tearing-down agent can close it. `chrome_tab_list(session)` shows what's tracked.

**Briefing Mode** (asymmetric-verbosity orchestration): an `anchor` (terse, human-facing) fans out `correspondent` worktrees (verbose researchers) via `worktree_create(roles="correspondent")`; they file reports into `research_dir()` (`~/.agentwire/research/<session>/`) and drop a passive `msg_send(kind="ingest", ref="<path>")` pointer; the anchor `msg_pull()`s on the human's cue and briefs with `say(text=..., display=...)` (voice + toast, different content). See wiki briefing-mode.md.
