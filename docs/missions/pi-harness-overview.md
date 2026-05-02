> Living document. Update this, don't create new versions.

# Mission: Pi Harness Integration — Overview

Integrate [pi coding agent](https://github.com/badlogic/pi-mono) as the harness for all Z.AI work. Keeps Claude Code pure for Anthropic subscription and unlocks new capabilities (dual-mode REPL + programmable).

**Context:** The previous claudeGLM wrapper (env var override into the `claude` binary) was removed from main on 2026-04-12 — Claude Code ignores inline `ANTHROPIC_*` env vars when OAuth auth is active, so the wrapper silently stopped working. Pi replaces that approach with a proper standalone binary that has native Z.AI provider support.

**Decision made 2026-04-13** after hands-on evaluation. See `~/.agentwire/wiki/wiki/research/pi-coding-agent-zai-harness.md` for the full evaluation.

## Why Pi

| Benefit | Detail |
|---------|--------|
| Native Z.AI provider | `--provider zai` built-in — no env var hacks |
| Minimal tool surface | 4 tools (read/write/edit/bash), smaller system prompt, more context |
| Print mode (`-p`) | Non-interactive execution, exits when done — perfect for automation |
| JSON event stream | `--mode json` emits JSONL for programmatic parsing |
| RPC protocol | `--mode rpc` for bidirectional control (future) |
| CLAUDE.md aware | Loads CLAUDE.md/AGENTS.md from cwd automatically |
| `--append-system-prompt` | Identical flag to Claude Code — role injection works unchanged |
| MIT licensed | No subscription tied to anyone's binary |
| Thinking control | `--thinking off\|low\|medium\|high\|xhigh` per task |

## The Architecture

Pi unlocks a **dual-mode** model that Claude Code can't cleanly provide:

```
┌─────────────────────────────────────────────────────────┐
│  Claude Code (Anthropic subscription)                   │
│  ├─ Human-directed interactive sessions                 │
│  ├─ Orchestrator sessions (need MCP tools)              │
│  └─ Overnight queue dispatch                            │
├─────────────────────────────────────────────────────────┤
│  Pi REPL Mode (Z.AI subscription)                       │
│  ├─ Interactive coding, cost-sensitive                  │
│  ├─ Worker panes that take multiple tasks               │
│  └─ Live exploration / debugging                        │
├─────────────────────────────────────────────────────────┤
│  Pi Programmable Mode (Z.AI subscription)               │
│  ├─ Workflow action nodes                               │
│  ├─ Scheduler task nodes                                │
│  └─ Chainable automation DAGs                           │
└─────────────────────────────────────────────────────────┘
```

## Mission Phases

Each phase is its own mission doc. Execute sequentially — each validates the next.

| # | Mission | Doc | Status |
|---|---------|-----|--------|
| 1 | Pi Session Type | `completed/pi-session-type.md` | **complete (2026-04-13)** |
| 2 | Pi Workflow Engine | `completed/pi-workflow-engine.md` | **complete (2026-04-14, v1.22.0)** |
| 3 | Scheduler Workflows | `completed/pi-scheduler-workflows.md` | **complete (2026-04-16, v1.23.0)** |
| 6 | Agent-SDK Workflow Runner (parallel to pi) | `completed/anthropic-sdk-runner.md` | **complete (2026-04-21)** — includes portal history window + `agentwire brave` helper + 5-day canary |
| 4 | Advanced Workflow Patterns | `pi-workflow-advanced.md` | **on hold — awaiting real-usage trigger** (2026-04-21) |
| 5 | Workflow Desktop UI | `pi-workflow-ui.md` | **partially shipped during Phase 6; remainder on hold** (2026-04-21) |

**Phase 6 closeout (2026-04-21)**: Shipped in 5 days vs the 4-6 week estimate. Scope expanded during development to include the portal workflow history window and the `agentwire brave` research helper, both of which emerged from real-usage pressure rather than the original plan. Three SDK-runner workflows plus three A/B variants running in production since 2026-04-17. The canary revealed a key finding: **Claude's built-in `WebSearch` tool is the research-quality bottleneck, not the model** — Opus+Brave-via-bash beats Opus+WebSearch by a wide margin. This shaped how research-heavy future workflows get built. See `completed/anthropic-sdk-runner.md` → "Closeout" for the full picture.

**Phase 6 follow-on (2026-04-26)**: `runners/anthropic.py` is one of two consumers (alongside the Textual REPL) feeding the `agentwire-sdk-primitives.md` mission — extracting shared SDK plumbing into a reusable engine, then composing new views on top (fan-out N-column, portal watch-mode, etc.).

**Phase 4 + 5 reassessment (2026-04-21)**: Real-usage data from 6 workflows over 5 days showed the originally-planned advanced patterns (parallelism, loops, HITL, cost caps, cross-workflow calls, event-driven triggers) have **zero active demand**. Workflows are 1-2 nodes, autonomous, subscription-covered. What *did* emerge as friction was tooling (research helpers, multi-recipient email, portal diagnosis surface) — all shipped during Phase 6 closeout. Phases 4 and 5 are on hold with specific triggers documented; revisit if/when real workflow complexity demands them.

## Dependencies Across Phases

```
Phase 1 (Session Type) ✅
  │
  ├──► Phase 2 (Workflow Engine) ✅ ──► Phase 3 (Scheduler Workflows) ✅
                                             │
                                             └──► Phase 6 (Agent-SDK Runner) ✅
                                                    │
                                                    ├──► Phase 4 (Advanced Patterns) — on hold
                                                    └──► Phase 5 (Desktop UI) — partial, rest on hold
```

Phase 1 validated pi works in our stack. Phase 2 built the engine. Phase 3 wired the engine into the scheduler. Phase 6 added a second runner (Anthropic SDK) alongside pi so higher-level features can be built on a runner-agnostic abstraction — and brought that abstraction to life through a 5-day canary of real scheduled workflows. Phases 4 and 5 remain available as the engine's next chapters when real workflows demand them.

## Key Non-Goals

- **Not replacing Claude Code entirely** — Claude Code remains the primary harness for general Anthropic-subscription work. The `completed/agentwire-repl.md` mission shipped a complementary agentwire-native harness (`sdk-*` session types) for specialized cases where deep MCP / damage-control / workflow integration matters; it's not a replacement either.
- **Not adding MCP to pi** — pi intentionally doesn't ship MCP; workflow nodes can call agentwire CLI via bash when needed

## Related missions

- **`completed/agentwire-repl.md`** — completed peer mission. A clean-room interactive REPL built on `claude-agent-sdk`, living as `sdk-bypass`/`sdk-prompted`/`sdk-restricted` session types. Reuses the Phase 6 anthropic runner's primitives (event translation, capability validation, storage schema). Complementary to pi's interactive mode: pi for Z.AI subscription economics, `sdk-*` for agentwire-native integration on Anthropic subscription.

## Migration Plan (High Level)

1. ✅ Phase 1 ships: `pi-zai` session type is the sole Z.AI path (claudeGLM already gone from main)
2. ✅ Scheduler migrates tasks to `pi-zai` as Phase 3 workflow engine lands
3. ✅ Phase 6 adds anthropic runner alongside pi — workflows can mix runners per-node
4. ⏸ Migrate remaining legacy `task:` scheduler entries (agentwire-website/social, wiki-ingest) to `workflow:` over time — deferred cleanup, no urgency
5. ⏸ Phases 4–5 extend the engine on trigger-driven demand

## Open Questions

- **Pi binary upgrades.** Currently npm-global install; no version pinning. Consider if a workflow ever breaks on a pi upgrade.
- **Legacy scheduler entries.** The old `task:` + tmux-session pattern still exists in `scheduler.yaml` for some projects. Migrating them to workflows is a cleanup, not a blocker. When we're ready, the scheduler window UI can simplify (drop the "task vs workflow-backed" dual shape).

## Resolved Questions

- ~~Should workflow nodes support non-pi engines (Claude Code fallback)?~~ — Resolved by Phase 6: `runner: anthropic` uses `claude-agent-sdk`, not a fallback but a first-class peer to pi.
- ~~What's the right event/state persistence store for long-running workflows?~~ — Resolved for now by per-run JSONL under `~/.agentwire/workflows/runs/<id>/`. Cross-run shared state stays deferred until Phase 4 has a concrete trigger.
- ~~Pi RPC protocol stability?~~ — Not a blocker for current workflow patterns; revisit if a use case surfaces.
