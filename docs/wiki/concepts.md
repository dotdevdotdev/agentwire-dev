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

This pattern is load-bearing in three ways. **First**, it bounds risk: a worker can be `pi-zai-restricted` (no edit, no write) while the orchestrator is `claude-bypass` (full access). The orchestrator delegates dangerous reads to a sandboxed worker without inheriting that worker's privileges. **Second**, it bounds context: workers run with a fresh prompt and a tiny system message, so they don't drag in the orchestrator's 200K-token conversation. **Third**, it bounds attention: pane 0 is where you look. Workers are noise that scrolls by; their summaries are the signal that surfaces.

The pattern composes. An orchestrator in a "main" session can spawn workers AND send messages to other sessions. Those other sessions are also orchestrators with their own workers. The whole graph forms naturally: idle notifications flow upward (worker → orchestrator → `parent:` session → human), commands flow downward (human → main session → child sessions → workers).

It also explains a lot of design choices in the wiki. Damage-control rules are session-local (per-pane, really) because workers are short-lived and their privileges should be too. Channels target sessions (not panes) because the orchestrator decides who handles inbound work. The scheduler creates orchestrator sessions (not workers) because tasks need their own context.

→ Detailed: [CLAUDE.md](../../CLAUDE.md), [Architecture — Process model](architecture.md#process-model).

---

## How channels turn agents into bots

Without channels, AgentWire is a developer tool: you `agentwire new`, you talk to the orchestrator, you watch panes. Channels invert the direction. Now an external platform — Discord, Slack, Telegram, email, SMS, a webhook URL — can address an agentwire session as if it were a chatbot, and the session can speak back through the same medium.

There are two flavors. **Send-only channels** (email, SMS, webhook, Quo) are stateless and outbound: a session calls `agentwire email --body ...` and that's it. **Service channels / bridges** (Discord, Slack, Telegram) are long-lived processes — usually their own tmux session — that hold a connection to the platform, route inbound messages to the right session, and forward outbound events (alerts, AskUserQuestion, voice) back out.

The bridge half is the interesting half. A bridge subscribes to the portal's WebSocket for outbound events on the sessions it manages. When the agent emits an alert or a voice message or an AskUserQuestion, the bridge formats it for the platform (a Slack thread, a Discord message, a Telegram audio file) and posts it. When a user replies on the platform, the bridge calls `send_to_session()` and the agent continues the conversation. Composable session config (default → scope → specific) means a single Slack workspace can route every channel and every DM to a different session with a different role and different instructions, all via YAML.

The takeaway: a channel is just an addressable transport between AgentWire sessions and the outside world. The agent doesn't know it's "in Slack" any more than it knows it's "in tmux" — it gets text in, produces text and tool calls, and the channel layer handles the medium.

→ Detailed: [Channels](communication/channels.md).

---

## Scheduled workloads vs workflows

There's frequent confusion about when to reach for **`agentwire ensure`** (scheduler with `task:`), **workflows** (scheduler with `workflow:`), and the **overnight queue**. They overlap, but each is shaped by a different question.

**Ensure tasks** answer: "How do I run a Claude Code session, headless, on a schedule, with branch management and PR creation?" The whole machinery — tmux session, pre-commands, prompt templating, summary file, on_task_end, post-commands, lock — exists because you want a *Claude Code session that mostly does its thing* and reports back. Use this when the work needs MCP tools, Claude's reasoning quality, or git plumbing wired in. Nightly tests, lint cleanup, doc rewrites, refactor passes — all ensure-shaped.

**Workflows** answer: "How do I chain N small reliable nodes with retries, conditional branches, and outputs flowing between them?" Each node runs against pi (cheap, fast, Z.AI-backed) or anthropic-runner (Claude SDK in-process). No tmux, no session, no Claude Code. Use this when each step has clear inputs/outputs and you'd rather pay for retries than pray a giant prompt does the right thing. Web research pipelines, doc-drift checks, daily briefings — workflow-shaped.

**The overnight queue** answers a different question entirely: "How do I run *judgment-heavy* work that I can't express as a recurring YAML?" The premise is that autonomous agents fail on judgment-heavy work because they lack the micro-decisions humans make. The overnight queue front-loads all human judgment into interactive preparation time (5–30 minutes per task in the evening), captures the prepared session's Claude conversation context, and dispatches the prepared sessions overnight. You wake up to a stack of draft PRs that contain decisions you'd have made yourself, but didn't have to stay up to make.

A practical decision shortcut:

- Recurring? → scheduler. Inside the scheduler:
  - One Claude prompt per run + needs git/MCP? → ensure task.
  - DAG of small reliable steps? → workflow task.
- One-shot but autonomous? → overnight queue.
- Ad-hoc, interactive? → just open a session.

→ Detailed: [Scheduled workloads](scheduling/scheduled-workloads.md), [Pi workflows](scheduling/workflows.md).

---

## Where to go next

You now have the mental model. Pick a path:

- **Run something today**: `agentwire new -s test` and follow the [REPL walkthrough](sessions/repl-tui.md), or pick a session type from the [sessions index](INDEX.md#sessions).
- **Define a recurring task**: [Scheduled workloads](scheduling/scheduled-workloads.md).
- **Wire a channel**: [Channels](communication/channels.md).
- **Need a term defined**: [Glossary](glossary.md).
- **Need a diagram**: [Architecture](architecture.md).
