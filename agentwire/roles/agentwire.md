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
| `session_send(session, message)` | Send a prompt to a session |
| `session_output(session, lines)` | Read session output |
| `session_info(session)` | Get session metadata |
| `session_kill(session)` | Kill a session |

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

### Spawn types

| `pane_type` | Agent |
|-------------|-------|
| `claude-bypass` | Claude Code with full permissions (default) |

## Hierarchy

Sessions can have parent sessions. When you go idle, your parent is notified. Use `notify(text, to=session)` to send text notifications up the chain.

## Overnight Queue

Queue prepared sessions for autonomous overnight execution with full conversation context.

| Tool | What it does |
|------|-------------|
| `overnight_prepare(session, description)` | Queue a session for overnight dispatch |
| `overnight_list()` | List queued/running items |
| `overnight_status()` | Orchestrator state and queue summary |
| `overnight_cancel(item_id)` | Cancel a queued or running item |
| `overnight_priority(item_id, priority)` | Update item priority |
| `overnight_report()` | Morning report of completed items |

## Wiki (Knowledge Base)

When you discover something noteworthy during your work — a technology gotcha, a debugging solution, a useful pattern, an API quirk — write or update a wiki page at `~/.agentwire/wiki/wiki/`. This compounds knowledge across sessions so future agents don't re-research the same things.

| Category | Path | What goes here |
|----------|------|----------------|
| Technologies | `wiki/technologies/<name>.md` | Tools, libraries, engines — how we use them, gotchas |
| Patterns | `wiki/patterns/<name>.md` | Architecture decisions, solutions, what worked/didn't |
| APIs | `wiki/apis/<name>.md` | External API reference, endpoints, auth, pricing |
| Research | `wiki/research/<name>.md` | Market research, comparisons, evaluations |

**Rules**: one page per entity (check before creating), update existing pages rather than duplicating, include code snippets, date your updates in frontmatter `last_updated`. Read `~/.agentwire/wiki/CLAUDE.md` for the full schema.

**Before researching**: check if a wiki page already exists on the topic. Use existing knowledge first.

## Notifications

| Tool | What it does |
|------|-------------|
| `notify(text)` | Text notification to parent session |
| `notify(text, to=name)` | Text notification to specific session |
| `email_send(body, to)` | Send outbound email via Resend |
| `quo_send(body, to)` | Send outbound SMS via Quo / OpenPhone |
