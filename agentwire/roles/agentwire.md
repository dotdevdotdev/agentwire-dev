---
name: agentwire
description: Understands the agentwire session and pane system
---

# AgentWire

You're running inside an agentwire session. You have MCP tools for managing sessions, panes, and communication.

## Sessions

Sessions are tmux sessions running AI agents. You can create, message, and monitor them.

| Tool | What it does |
|------|-------------|
| `sessions_list()` | List all active sessions |
| `session_create(name)` | Create a new session |
| `session_send(session, message)` | Send a prompt to a session **now** (pastes + Enter immediately) |
| `msg_send(to, text, kind)` | **Polite** peer message — queued, injected only when their box is empty |
| `msg_inbox(session)` | Peek a session's pending polite messages |
| `session_output(session, lines)` | Read session output |
| `session_info(session)` | Get session metadata |
| `session_kill(session)` | Kill a session |

**Polite vs forceful.** Prefer `msg_send` for routine peer updates that must not interrupt — "PR drafted", "picking up the footer". It drops the message into a file inbox and the watchdog injects it only when the recipient's input box is empty (≤60s), so it **never clobbers a human's half-typed draft**. Reach for `session_send` **only** when you must drive a session right now — it pastes + Enter immediately, overwriting any uncommitted draft. `kind` ∈ note|done|request|escalation; `to="@all"` broadcasts to live agent sessions except you.

**Subtask vs standalone.** By default a session you create in **your own project** is recorded as your child — its prompts (permission / plan / AskUserQuestion) route back to you, which is right for a **subtask of your own work**. But when you spin up a session in a **separate project you're only advising or delegating to**, you don't want its prompts routed to you — they should reach the human. Cross-project spawns already auto-root (a genuinely different project becomes its own standalone root, never your child). To force standalone anyway — a same-project session you want detached, or belt-and-suspenders on a cross-project one — pass `session_create(name, standalone=True)`; that opts the new session out of parenting entirely. (Set `created_by` instead to force a *specific* parent; it wins over `standalone`.) To detach a session you already spawned, clear `created_by` from `~/.agentwire/sessions/<name>/metadata.json`.

## Panes (Workers)

Panes are sub-processes within your session. Pane 0 is you. Panes 1+ are workers.

**Do NOT spawn workers unless the user asks you to, or the task clearly requires parallel work across multiple files/features.** Most tasks are simpler and faster to do yourself. Workers have overhead (session startup, context loading, summary handoff) that isn't worth it for straightforward work.

Workers are for: large refactors touching many files, parallel independent subtasks, long-running operations you want to monitor.

| Tool | What it does |
|------|-------------|
| `pane_spawn(pane_type, roles)` | Spawn a worker pane |
| `pane_send(pane, message)` | Send a task to a worker |
| `pane_output(pane)` | Read worker output |
| `panes_list()` | List all panes |
| `pane_kill(pane)` | Kill a worker pane |

Workers auto-exit when idle. They write summary files before exiting, and you receive the summary via an alert notification.

### Spawn postures

| `posture` | Meaning |
|-------------|-------|
| `bypass` | Full permissions (default) |
| `prompted` | Interactive approval (hooks gate) |
| `auto` | Classifier permission mode |

## Hierarchy

Sessions can have parent sessions. When you go idle, your parent is notified. Use `notify(text, to=session)` to send text notifications up the chain.

`session_create` / `worktree_create` record you as the new session's parent **only when the target project is the one you're already running in** — spawning into a genuinely different project gets its own standalone root instead of nesting under you. Fanning out more work within your own project still parents as before.

## Wiki (Knowledge Base)

When you discover something noteworthy during your work — a technology gotcha, a debugging solution, a useful pattern, an API quirk — write or update a wiki page at `~/.agentwire/wiki/wiki/`. This compounds knowledge across sessions so future agents don't re-research the same things.

| Category | Path | What goes here |
|----------|------|----------------|
| Technologies | `wiki/technologies/<name>.md` | Tools, libraries, engines — how we use them, gotchas |
| Patterns | `wiki/patterns/<name>.md` | Architecture decisions, solutions, what worked/didn't |
| APIs | `wiki/apis/<name>.md` | External API reference, endpoints, auth, pricing |
| Research | `wiki/research/<name>.md` | Market research, comparisons, evaluations |

**Rules**: one page per entity (check before creating), update existing pages rather than duplicating, include code snippets, date your updates in frontmatter `last_updated`. Read `~/.agentwire/wiki/CLAUDE.md` for the full schema.

**Before researching**: call the `wiki_query` MCP tool (`wiki_query("<topic>")`) to see what's already recorded — it returns the top pages with paths + snippets; read them and use existing knowledge first. (From a shell, the same search is `agentwire wiki query "<topic>"`.) When you discover something new, scaffold the page with `agentwire wiki new <category> <name>` and write it in-context.

## Notifications

| Tool | What it does |
|------|-------------|
| `notify(text)` | Text notification to parent session |
| `notify(text, to=name)` | Text notification to specific session |
| `email_send(body, to)` | Send outbound email via Resend |
| `quo_send(body, to)` | Send outbound SMS via Quo / OpenPhone |
