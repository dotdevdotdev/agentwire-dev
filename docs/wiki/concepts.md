# Concepts

> Living document. Update this, don't create new versions.

If the [glossary](glossary.md) answers "what is X?", this page answers "why does X exist, and when do I reach for it?" Read it once to load the mental model; come back when a deep-dive page makes a choice you don't agree with and you want to understand the reasoning.

If you'd rather read a diagram than prose, jump to [Architecture](architecture.md).

---

## Why tmux as the substrate

Most agentic tools spawn one subprocess per agent and call it a day. AgentWire instead runs every agent inside a long-lived **tmux session**. The cost is one extra dependency on every machine; the payoff is large.

A tmux session is a process tree that survives shell exits, network drops, and even the agentwire CLI crashing. You can SSH into a remote box, attach to a session a worker started two hours ago, and pick up exactly where it left off. You can pop multiple panes inside the same session and watch a worker stream output while the orchestrator plans the next step. You can capture a pane's scrollback with one command (`tmux capture-pane -p`) and feed it to another agent as context.

Crucially, tmux gives you a stable *addressing* model. A session has a name; a pane has an index. `agentwire send -s myproject 0 "..."` is unambiguous. There's no PID race, no "which terminal tab was that." That's why every channel, every hook, every MCP tool talks to "session + pane" rather than "process." Browser tabs and bare subprocesses can't carry that semantic.

The downside: if you don't have tmux, you don't have AgentWire. We've made peace with that. Every dev machine either has tmux already or is one `apt install` away.

→ Detailed: [Architecture — Process model](architecture.md#process-model).

---

## Sessions as the unit of work

In AgentWire, a *session* is the smallest unit of meaningful work. Sessions have an identity (name + optional `@machine` suffix), a configuration (`.agentwire.yml` + global config), a state (idle/active/dead), and a transcript (the JSONL Claude Code writes). Everything else — pre-prompts, gates, channels, MCP tools, voice — is plumbing around that core unit.

This matters because most automation systems try to make "tasks" or "messages" the unit of work. Tasks are too small (you lose context across them) and messages are too small (you lose state). A session is just the right size: long enough to hold a project's context, short enough to bound a unit of risk, persistent enough to resume.

When you ask "should this be one session or two?" the answer is almost always informed by *context boundaries*. Two pieces of work that share a code branch and a recent conversation belong in one session. Two pieces of work that don't should be different sessions, possibly with `parent:` linking them so the user only sees one notification stream. The orchestrator/worker pattern (next concept) is what falls out when you push this principle through.

→ Detailed: [Sessions index](INDEX.md#sessions).

---

## The orchestrator/worker pattern

Inside a session, **pane 0 is the orchestrator** — the agent the user (or another session) talks to. Workers live in panes 1+ and are spawned by the orchestrator (typically via the MCP `pane_spawn` tool) for bounded subtasks. When a worker goes idle, an idle-handler hook captures the worker's output, sends a summary alert to pane 0, and kills the worker. Pane 0 is then free to dispatch the next worker, talk to the user, or start another agent.

This pattern is load-bearing in three ways. **First**, it bounds risk: a worker can be `restricted` (say-only — no edit, no write) while the orchestrator is `bypass` (full access). The orchestrator delegates dangerous reads to a sandboxed worker without inheriting that worker's privileges. **Second**, it bounds context: workers run with a fresh prompt and a tiny system message, so they don't drag in the orchestrator's 200K-token conversation. **Third**, it bounds attention: pane 0 is where you look. Workers are noise that scrolls by; their summaries are the signal that surfaces.

The pattern composes. An orchestrator in a "main" session can spawn workers AND send messages to other sessions. Those other sessions are also orchestrators with their own workers. The whole graph forms naturally: idle notifications flow upward (worker → orchestrator → `parent:` session → human), commands flow downward (human → main session → child sessions → workers).

It also explains a lot of design choices in the wiki. Damage-control rules are session-local (per-pane, really) because workers are short-lived and their privileges should be too. Channels target sessions (not panes) because the orchestrator decides who handles inbound work. The scheduler creates orchestrator sessions (not workers) because tasks need their own context.

→ Detailed: [CLAUDE.md](../../CLAUDE.md), [Architecture — Process model](architecture.md#process-model).

---

## How channels turn agents into pushers

Inbound interaction with AgentWire flows through the **portal** — the web UI + WebSocket. Channels handle the *other* direction: outbound notifications from a session to a human reachable somewhere off the wire. Today that means email (Resend) and SMS (Quo / OpenPhone).

The shape is dead simple. A channel is a `SendOnlyChannel` subclass with a `send()` coroutine, a YAML config slot, and a CLI wrapper. A session that wants to escalate runs `agentwire email --subject "..." --body "..."` or `agentwire quo --body "..."` and the channel module makes the API call. No background process, no inbound webhook, no public surface.

The takeaway: channels are stateless outbound notifications. Inbound user input is the portal's job. If you want a session to nudge you when it's done or stuck, you wire an `agentwire email` or `agentwire quo` call into the prompt — that's the whole pattern.

→ Detailed: [Channels](communication/channels.md).

---

## Scheduled workloads

**Ensure tasks** (scheduler with `task:`) answer: "How do I run a Claude Code session, headless, on a schedule, with branch management and PR creation?" The whole machinery — tmux session, pre-commands, prompt templating, summary file, on_task_end, post-commands, lock — exists because you want a *Claude Code session that mostly does its thing* and reports back. Use this when the work needs MCP tools, Claude's reasoning quality, or git plumbing wired in. Nightly tests, lint cleanup, doc rewrites, refactor passes — all ensure-shaped.

A practical decision shortcut:

- Recurring + autonomous? → scheduler ensure task.
- Ad-hoc, interactive? → just open a session.

→ Detailed: [Scheduled workloads](scheduling/scheduled-workloads.md).

---

## Where to go next

You now have the mental model. Pick a path:

- **Run something today**: `agentwire new -s test` and pick a posture from the [sessions index](INDEX.md#sessions).
- **Define a recurring task**: [Scheduled workloads](scheduling/scheduled-workloads.md).
- **Wire a channel**: [Channels](communication/channels.md).
- **Need a term defined**: [Glossary](glossary.md).
- **Need a diagram**: [Architecture](architecture.md).
