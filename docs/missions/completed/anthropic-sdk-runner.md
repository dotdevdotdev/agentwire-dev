> Living document. Update this, don't create new versions.

# Mission: Phase 6 — Agent-SDK Workflow Runner (parallel to pi)

Add a second, Anthropic-SDK-backed node executor alongside `pi_runner.py`. Workflows declare a `runner:` per node (or inherit workflow-level default); the DAG engine, storage, templating, and scheduler integration stay unchanged. Let real-world usage over weeks reveal which runner wins for which node type — *without* ripping out pi.

**Phase of:** `pi-harness-overview.md` (extends — not replaces — the pi path)
**Status:** **complete (code shipped 2026-04-16 through 2026-04-21; canary running since 2026-04-17)**
**Estimated effort:** 4–6 weeks · **actual: ~5 days** (2026-04-16 through 2026-04-21)
**Depends on:** Phase 2 (workflow engine), Phase 3 (scheduler integration)
**Unblocked:** Phase 4 (advanced patterns) and Phase 5 (desktop UI) — runner-agnostic abstraction in place.

**SDK choice (locked 2026-04-16):** `claude-agent-sdk>=0.1.43` (already in `pyproject.toml`). Not the raw `anthropic` Messages API SDK. Rationale: `claude-agent-sdk` is the only package that uses Anthropic Claude monthly subscription auth (picks up creds from the `claude` CLI); raw `anthropic` requires an API key which we explicitly don't want. Claude Code also owns tool execution inside the SDK loop, so we inherit its tool set and our existing `~/.claude/hooks` damage-control plumbing for free.

**Strategic note (2026-04-16)**: After Anthropic confirmed subscription-mode Agent SDK usage is supported, we're reordering: Phase 6 (this mission) comes next, Phase 4 + 5 are explicitly gated on it. Rationale — every Phase 4 feature (parallelism, loops, cost gates, HITL) must work uniformly across runners, and building them pi-only would force a retrofit later.

---

## Context

- Phase 1–3 shipped pi → Z.AI as the primary LLM harness inside the workflow engine because we believed Anthropic was blocking Agent-SDK usage on subscription plans.
- **2026-04-16**: Anthropic publicly confirmed on X that subscription usage of the Agent SDK is supported and intended long-term.
- The workflow engine itself (DAG, Jinja2, output extraction, storage, scheduler dispatch) is already *ours*. The only place we hand control off is `pi_runner.run_node()` → the `pi` binary.
- Replacing just that layer unlocks native Python observability, in-process event streaming, mid-run control hooks, and optional MCP-from-inside-a-node — without losing pi (which still handles multi-provider + Z.AI subscription economics).

## Goal

Ship `agentwire/workflows/anthropic_runner.py` so a workflow YAML can say:

```yaml
nodes:
  cheap_scan:
    runner: pi                # default — glm-5.1 via Z.AI subscription
    prompt: "…"
  deep_reason:
    runner: anthropic         # Claude via Agent SDK, subscription-backed
    model: sonnet
    thinking: high
    prompt: "…"
```

…and have both paths produce byte-compatible `NodeResult` objects, so every downstream consumer (`runner.py`, `storage.py`, `context.py`, the morning report, the portal) works unchanged.

## Why Both, Not One

| Dimension | pi runner | Anthropic-SDK runner |
|---|---|---|
| **Cost / subscription** | Z.AI plan covers glm-5.1 in full | Anthropic plan |
| **Multi-provider** | Yes (zai, anthropic, openai, google, deepseek, …) | Claude-only |
| **Subprocess overhead** | 500ms–1s per node | Near-zero (in-process) |
| **Observability** | Parse JSONL after the fact | Live Python callbacks |
| **Tool customization** | 4 fixed tools from pi | Register any tools + custom behavior |
| **Damage-control bash** | Pi's bash bypasses our hooks | Native — tool wraps our damage-control |
| **MCP inside nodes** | No (pi intentionally skips MCP) | Yes — direct `mcp__agentwire__*` calls |
| **Mid-run control** | Can only inspect outputs | Can budget-cap, cancel, branch adaptively |
| **Maintenance** | Someone else owns the CLI | We own the SDK wrapping |

No single answer wins on every axis. That's why we build both and compare.

---

## Design

### 1. Runner abstraction

Define a tiny protocol:

```python
# agentwire/workflows/runners/__init__.py

class NodeRunner(Protocol):
    name: str               # "pi" | "anthropic"
    def run(self, node: ActionNode, context: Context,
            event_log_path: Path | None) -> NodeResult: ...
```

`pi_runner.run_node` moves to `agentwire/workflows/runners/pi.py` (thin rename + wrap). New `runners/anthropic.py` implements the same protocol.

A registry keeps it extensible:

```python
# agentwire/workflows/runners/__init__.py
RUNNERS: dict[str, NodeRunner] = {
    "pi": PiRunner(),
    "anthropic": AnthropicRunner(),
}
```

`workflows/runner.py` resolves the runner by name per node; no other engine code changes.

### 2. YAML: `runner:` field

- Per-node override: `runner: anthropic`
- Workflow-level default: top-level `runner: anthropic` applies to nodes that don't set their own
- Global default: `pi` (backwards-compatible — every existing workflow keeps working with no edits)

`definitions.py` validates: unknown runner name → parse error.

### 3. Anthropic-SDK runner

`agentwire/workflows/runners/anthropic.py` uses `claude-agent-sdk` with subscription auth. The SDK spawns a `claude` CLI subprocess under the hood and streams structured `Message` objects back; we translate those into pi-shaped JSONL for parity:

```python
import asyncio
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, UserMessage, ResultMessage, StreamEvent,
)

class AnthropicRunner:
    name = "anthropic"

    def run(self, node, context, event_log_path):
        return asyncio.run(self._run_async(node, context, event_log_path))

    async def _run_async(self, node, context, event_log_path):
        options = ClaudeAgentOptions(
            model=node.model,                            # full string e.g. "claude-opus-4-7"
            system_prompt=self._compose_system(node),    # role prompts, CLAUDE.md auto-loaded
            allowed_tools=self._resolve_tools(node),     # ["Read","Write","Edit","Bash","Grep","Glob"]
            permission_mode="bypassPermissions",         # headless, no interactive prompts
            thinking=self._thinking(node),               # ThinkingConfigAdaptive | Enabled | Disabled
            effort=node.effort,                          # "low"|"medium"|"high"|"max" or None
            max_thinking_tokens=node.max_thinking_tokens,
            max_budget_usd=node.max_budget_usd,          # hard cost ceiling (works even on sub)
            cwd=context.cwd,
            env=node.extra_env,
            setting_sources=["user"],                    # load ~/.claude hooks (damage-control)
            include_partial_messages=True,               # live streaming events
        )

        events: list[dict] = []
        tool_calls: list[dict] = []
        final_text = ""
        tokens: dict = {}

        async with ClaudeSDKClient(options=options) as client:
            await client.query(node.prompt)
            async for message in client.receive_response():
                jsonl_event = translate_to_pi_shape(message)
                events.append(jsonl_event)
                if self.on_event:
                    self.on_event(jsonl_event)

                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if block.type == "text":
                            final_text += block.text
                        elif block.type == "tool_use":
                            tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
                elif isinstance(message, ResultMessage):
                    tokens = {
                        "input": message.usage.input_tokens,
                        "output": message.usage.output_tokens,
                        "cost": message.total_cost_usd or 0.0,  # $0 under sub but field is present
                    }

        if event_log_path:
            self._persist_events(event_log_path, events)

        return NodeResult(
            node_id=node.id, status="success",
            final_text=final_text, tool_calls=tool_calls,
            tokens_used=tokens, events=events, duration_ms=...,
        )
```

### 4. Tools — Claude Code's built-ins, isolated via `allowed_tools`

**Decision (2026-04-16):** we do NOT write custom Python tool implementations. Claude Code already owns tool execution (`Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, etc.) inside the SDK's managed loop. The Anthropic runner only *selects* which of those tools are available per node:

```yaml
nodes:
  deep_reason:
    runner: anthropic
    tools: [Read, Grep, Glob]       # Claude tool names (CamelCase) — no Bash here
  stealth_fix:
    runner: anthropic
    tools: [Read, Write, Edit, Bash] # full-access node
```

The runner maps `node.tools` → SDK `allowed_tools` at call time. Unsupported tool names (e.g., pi's lowercase `read`/`bash`) raise a parse error — the Anthropic runner speaks CamelCase.

**Damage-control**: Claude Code runs our existing `~/.claude/hooks/*` including the damage-control hook on every `Bash` tool call. We just need `setting_sources=["user"]` in `ClaudeAgentOptions` so the SDK loads those hooks. No extra work. If a command would be blocked in an interactive `claude` session, it's blocked in the workflow node too.

**Isolation from pi**: completely preserved. Pi keeps using pi's lowercase tools through the pi binary. The Anthropic runner talks to Claude's runtime via claude-agent-sdk. Zero shared tool code. Rolling back a node to pi means flipping `runner:` back and changing the tool names back to lowercase — one-line YAML edit, no Python changes.

**Why this is simpler than custom Python tools**: every hour we'd spend re-implementing `tool_bash` / `tool_read` / `tool_edit` is an hour we'd spend fighting Claude Code's real implementations that already work, handle edge cases, respect hooks, and get maintained upstream.

### 5. Event serialization

To keep `storage.py` / `workflow show <run_id> --events` working identically, SDK `Message` objects get translated into the same JSONL shape pi emits (`session`, `agent_start`, `turn_start`, `message_start`, `message_end`, `turn_end`, `agent_end`). A small translator lives in `runners/anthropic_events.py`:

| SDK object | Translated pi-shape event |
|---|---|
| Initial `SystemMessage(subtype=init)` | `session` + `agent_start` |
| `AssistantMessage` with text/tool_use blocks | `turn_start` + `message_end` (role=assistant) |
| `UserMessage` with tool_result blocks | `message_end` (role=user) |
| `StreamEvent` (partial messages) | discarded unless `--verbose` live stream needs them |
| `ResultMessage` | `turn_end` + `agent_end` (carries tokens + cost) |

Tools, thinking blocks, token usage all map one-to-one with pi's schema. Portal and `workflow show` don't need to know which runner produced the events.

### 6. Output extraction

Unchanged. `outputs.extract_outputs(events, output_specs)` operates on the unified event shape; extractors don't care which runner emitted it.

### 7. Observability — MVP scope

**Decision (2026-04-16):** the basic observability wins are trivial because the SDK already emits structured events. Pull the first four features into MVP; defer the harder ones.

Shipped in MVP (week 1-2):

| Feature | How |
|---|---|
| Structured Python logging per node | Standard `logging` with run_id / node_id in extra |
| Live event callback — every tool call, thinking block, text chunk | SDK streams them; expose as `AnthropicRunner(on_event=callback)` |
| `--verbose` CLI flag: print events live as they happen | Hook the callback; `cli.py` already has the flag for pi, extend to Anthropic |
| Token + cost accumulator per run | `ResultMessage.usage.{input,output}_tokens` summed across nodes. Subscription runs report `total_cost_usd=0` — we track tokens as the primary metric and surface cost when present (non-sub auth). |

Stretch for Phase 6 (week 4+, after canary):

| Feature | Scope |
|---|---|
| **Basic live UI for workflow events** | New portal endpoint — Server-Sent Events streaming the event callback to a simple HTML page that scrolls as the run progresses. **Text-only, no graph, no canvas.** Explicitly NOT the Phase 5 canvas. |

**Explicitly deferred — needs further design discussion before we commit:**

- **Mid-run control** (abort if cost > cap, switch model adaptively on retry, cancel cleanly) → Phase 6 stretch or Phase 4. Hard — decision points include: what triggers an abort? can a node be resumed? how does cost propagate to the workflow-level decision? User flagged these as needing more discussion before work starts.
- **Full portal canvas** with node-status colors, live DAG rendering, replay scrubber → **Phase 5**. The basic tailing UI above is intentionally not a stepping stone toward this — the canvas is a separate, richer product surface.

```python
class AnthropicRunner:
    def __init__(self, on_event: Callable[[dict], None] | None = None): ...

# Usage (from workflows/runner.py):
def _on_event(event):
    _log_structured(event, run_id=..., node_id=...)
    if cli_args.verbose:
        _print_event_line(event)
    _update_cost_tally(event)
```

### 8. Scheduler integration

Zero changes to `scheduler.py`. Workflow tasks still just call `run_workflow(wf, runs_dir, inputs)` — the runner selection happens entirely inside the workflow engine, invisible to the scheduler.

---

## Files to Change

| File | Change |
|---|---|
| `agentwire/workflows/runners/__init__.py` | New — runner registry |
| `agentwire/workflows/runners/pi.py` | New — thin wrapper around existing `pi_runner.py` (pi behaviour unchanged) |
| `agentwire/workflows/runners/anthropic.py` | New — SDK-backed node executor using `claude-agent-sdk` |
| `agentwire/workflows/runners/anthropic_events.py` *(now `agentwire/sdk/events.py`, relocated v1.24.0)* | New — translate SDK `Message` objects → pi-shaped JSONL |
| `agentwire/workflows/runners/anthropic_capabilities.py` *(now `agentwire/sdk/capabilities.py`, relocated v1.24.0)* | New — model → supported settings table; used by validator and runtime |
| `agentwire/workflows/pi_runner.py` | Leave in place as a shim for one release cycle, then delete |
| `agentwire/workflows/node.py` | Add `runner`, `effort`, `max_thinking_tokens`, `max_budget_usd`, `task_budget` fields on `ActionNode` |
| `agentwire/workflows/definitions.py` | Parse top-level `runner:` + per-node `runner:` + new SDK settings; validate against registry |
| `agentwire/workflows/runner.py` | Resolve runner from registry per node; thread event callback |
| `agentwire/workflows/storage.py` | Record which runner produced each run in `metadata.json` |
| `pyproject.toml` | `claude-agent-sdk>=0.1.43` — already present; bump floor if newer surface needed |
| `docs/workflows.md` | New "Runners" section — when to pick which |
| `docs/missions/anthropic-sdk-runner.md` | This file (mission) |
| `.claude/skills/agentwire-workflows/SKILL.md` | Mention per-node `runner:` field |
| `tests/unit/test_workflows.py` | Runner registry, validation, default-propagation |
| `tests/integration/test_anthropic_runner.py` | New — smoke-test against real SDK with a trivial prompt |

---

## Success Criteria

- [ ] Existing 7 example workflows run identically under the pi runner — zero YAML edits required, zero pi behavioural changes
- [ ] A new `workflows/examples/claude-reasoning.yaml` showcases the Anthropic runner with `runner: anthropic`
- [ ] `workflow show <run_id>` output is visually identical whether the run was pi or anthropic (modulo provider/model names in the metadata)
- [ ] Portal morning report surfaces which runner each workflow used (small badge next to `workflow:` label)
- [ ] Pi's tool path stays completely untouched — pi binary still runs its own tools, no shared runtime
- [ ] **`daily-book-report.compose_and_send` converted to `runner: anthropic`** (canary — the heaviest reasoning node on a live scheduled task) and running via manual invocations to compare Opus 4.7 output quality vs glm-5.1; no production frequency requirement, this is a quality test
- [ ] Basic observability landed in MVP: structured logging, live event callback, `--verbose` live event stream, per-run cost accumulator
- [ ] Comparison happens naturally across weeks of real usage — user feedback captured in this doc, no formal A/B benchmark workflow needed

---

## Testing Plan

### Unit
- Runner registry: unknown name raises, registered names resolve
- `ActionNode.runner` default cascade: per-node > workflow > global default "pi"
- Event translator: golden-file comparison between pi JSONL and translated Anthropic JSONL for the same prompt (ignore timestamps / IDs)
- Tool implementations: bash damage-control coverage (assert `rm -rf /` is blocked, assert allowed commands succeed)
- Shared output extractors unchanged — already covered by existing tests

### Integration (requires working `claude` CLI subscription auth on the host)
- `run_workflow(hello-world, runner: anthropic)` → success, final_text populated
- Tool-use roundtrip: node that must use `Read` — verify tool_use appears in events, tool_result is fed back
- Retry on transient 529/overloaded — retries trigger, final attempt recorded
- Thinking control: `thinking: { type: adaptive }` produces thinking blocks; `thinking: { type: disabled }` does not
- Token accumulator across a 3-node workflow matches sum of per-node usage
- Damage-control hook fires: `Bash("rm -rf /tmp/…")` blocked by `~/.claude/hooks/damage-control/*`

### Manual QA
- **First canary**: flip `daily-book-report.compose_and_send` to `runner: anthropic` once MVP feature parity is reached. It's the heaviest reasoning node on a live scheduled task, so quality differences between Opus 4.7 and glm-5.1 show up clearly in the output.
- Test via manual invocations (`agentwire workflow run daily-book-report`) rather than waiting on the scheduled 13:30 runs — frequency doesn't matter for quality comparison.
- Capture notes here on: output quality vs glm-5.1, latency, tokens consumed, any reliability issues. No formal benchmark — natural comparison across runs.

---

## Locked decisions (not open — user-directed 2026-04-16)

- **Model strings**: always use the full proper Anthropic model ID — `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5-20251001`. No alias table, no `sonnet`/`opus`/`haiku` shorthand. Full strings in YAML, config, code, docs. Explicit, easy to update, no ambiguity about which exact model ran.
- **Node settings on Anthropic-runner nodes** (fail-fast, no magic fallbacks). The `claude-agent-sdk==0.1.43` surface exposes these as first-class `ClaudeAgentOptions` fields:
  - `thinking: ThinkingConfigAdaptive | ThinkingConfigEnabled | ThinkingConfigDisabled | None` — adaptive is the only valid mode on Opus 4.7; enabled/budget_tokens is pre-4.6 only.
  - `effort: "low" | "medium" | "high" | "max" | None` — **the SDK 0.1.43 enum does NOT include `"xhigh"`.** To use `xhigh` (Opus 4.7 default in Claude Code) we either pass it through `extra_args={"effort": "xhigh"}` (untyped passthrough) or wait for a newer SDK. MVP: support the four typed values; add `xhigh` via `extra_args` once validated.
  - `max_thinking_tokens: int | None` — per-node thinking cap (in addition to adaptive mode).
  - `max_budget_usd: float | None` — per-run dollar cap enforced by the SDK. Works under subscription auth too (reports $0 spent but still enforces if set).
  - `task_budget` (beta) is NOT a first-class SDK field; if we want the Messages API `task_budget` semantics we pass it via `extra_args={"task-budget-tokens": N}` and declare the beta header. Treated as Opus-4.7-only.

  **YAML schema for Anthropic-runner nodes**:

  ```yaml
  nodes:
    deep_reason:
      runner: anthropic
      model: claude-opus-4-7             # full proper string — no aliases
      tools: [Read, Grep, Glob, Bash]    # Claude tool names — CamelCase

      # All settings below are optional. Omitted = SDK default.
      thinking: { type: adaptive }       # or { type: disabled } | { type: enabled, budget_tokens: N } (pre-4.6)
      effort: high                       # low | medium | high | max (xhigh via extra_args — see notes)
      max_thinking_tokens: 16000         # optional thinking-only cap
      max_budget_usd: 5.00               # hard cost ceiling for this node's run
      task_budget_tokens: 40000          # Opus 4.7 only, min 20000 (beta — passes through extra_args)
  ```

  **Validation policy — strict, at parse time.** Bad combinations are caught at `agentwire workflow validate` and at `agentwire scheduler board` load, before a single node runs. No silent coercion, no "warn and continue" — errors surface with the exact setting that's wrong and why. A small capability table at `agentwire/sdk/capabilities.py` (was `agentwire/workflows/runners/anthropic_capabilities.py` pre-v1.24.0) is consulted by both the validator and the runtime:

  | Setting | Requires | Error if violated |
  |---|---|---|
  | `effort: max` | Opus-tier (`claude-opus-*`) | `"effort: max requires claude-opus-*, got {model}"` |
  | `effort: xhigh` (via extra_args) | Opus 4.7 specifically | `"effort: xhigh requires claude-opus-4-7, got {model}"` |
  | `effort: any` | Not Haiku 4.5, not Sonnet 4.5 | `"effort param not supported on {model}, omit it"` |
  | `task_budget_tokens` | Opus 4.7 | `"task_budget_tokens requires claude-opus-4-7, got {model}"` |
  | `task_budget_tokens < 20000` | Always | `"task_budget_tokens minimum is 20000, got {N}"` |
  | `thinking: {type: enabled, budget_tokens: N}` | Pre-4.6 models only | `"budget_tokens removed on {model}, use thinking: {type: adaptive} + effort instead"` |
  | `tools: [<lowercase>]` | Never (pi uses lowercase) | `"anthropic runner expects CamelCase tools: Read/Write/Edit/Bash/Grep/Glob, got {tool}"` |

  **Runtime behaviour**: if a validation rule missed something and the SDK/API rejects the call, the `NodeResult.error` carries the verbatim error. No retry for deterministic 400s. User sees a clean stack: "YAML → validator → SDK/API error → fix YAML."

  **Pi-side `thinking: medium` strings are ignored by the Anthropic runner** — no translation attempted. If a workflow wants effort control on a Claude node, it declares `effort:` explicitly. Pi's short strings stay pi-only.

  **Why strict over lenient**: silent "warn and drop unsupported settings" is worse at scale — users who *thought* they enabled `effort: xhigh` and got free high-quality runs get surprised when they learn it was ignored. Errors at validation time (cheap, instant) are always better than errors at runtime (expensive, post-partial-work, maybe mid-DAG). The validator is the right place to catch this.
- **No kill-switch config**. Rollback path is: flip the offending node's `runner:` field back to `pi` (or delete it — `pi` is the default). One-line YAML edit. That's the whole rollback story.
- **Authentication**: `claude-agent-sdk` spawns the `claude` CLI under the hood and inherits its subscription auth from `~/.claude/.credentials.json`. No `ANTHROPIC_API_KEY` env var, no config entry, no design decision. Week 1 scaffolding verifies auth flows end-to-end with a hello-world node — no code change expected.

## Open Questions

- **Agent SDK package name / version**: pin to the right `anthropic>=x.y.z` once we pick a first target version.
- **Retry on rate limit**: retry vs bail-out policy might differ from pi's. Start with pi's behaviour and tune from data.
- **Tool whitelist scoping per-session-type**: pi-zai-restricted / pi-zai-readonly map to pi's `--tools`. For the Anthropic runner, we apply the same whitelist at tool-registration time.
- **Pricing transparency**: Anthropic SDK usage reports tokens natively. Subscription-covered runs should still show tokens used (even if cost is $0) so we can reason about throughput. Surface in the morning report cost column alongside pi's.

---

## Risk Mitigation

- **SDK churn**: `claude-agent-sdk` tracks Claude Code's release cadence. Pin floor, watch breaking-change announcements, keep an integration test that verifies streaming semantics.
- **Subscription quota**: headless workflow runs consume the same 5-hour sub window as interactive Claude Code. If the canary eats too much quota, rollback the node's `runner:` to pi.
- **Bash damage-control**: inherited from `~/.claude/hooks` via `setting_sources=["user"]` — no custom Python tool impl means no new escape surface. Still: integration test must prove the hook fires on a blocked command.
- **Observability overload**: Live event streams can swamp the portal. Start with server-side buffering + client-side throttling (same play we'd use in Phase 5 anyway).
- **Subscription policy reversal**: If Anthropic ever walks back subscription SDK usage, the `pi` path is still there. Rollback = flip workflows' `runner:` field back to `pi`.

---

## Rollout

1. **Week 1** — Scaffolding: runner registry, move `pi_runner.py` behind `runners/pi.py` shim, ship tests proving zero pi-behaviour change
2. **Week 1-2** — MVP Anthropic runner: single node, 4 tools, events translated to pi-shaped JSONL, hello-world passes. **Observability basics** land here: structured logging, live event callback, `--verbose` stream, cost accumulator
3. **Week 2-3** — Feature parity: retries on rate limits, thinking budgets mapped, output extraction hooked in, storage records `runner` field, tool whitelist honored for pi-zai-restricted/readonly equivalents
4. **Week 3-4** — Integration polish: morning report shows runner badge per row, `workflow show` surfaces runner, `agentwire workflow run <name> --runner anthropic` CLI override for testing
5. **Week 4+** — **Canary**: flip `daily-book-report.compose_and_send` to `runner: anthropic`. Run ≥2 weeks. Capture findings in this doc.
6. **Week 5-6 (stretch)** — Basic live UI: SSE endpoint + scrolling event page in the portal. Text-only, intentionally minimal — NOT the Phase 5 canvas.
7. **Week 6+** — Decide on Phase 4 and Phase 5 restart based on what we learned. Mid-run control + full canvas get their own design passes before work starts.

**Guardrails while this phase is in flight:**

- Phase 4 and Phase 5 work is paused until Phase 6 hits feature parity with pi
- Pi path is byte-for-byte unchanged the entire time
- Rollback for any production issue = flip the offending workflow's `runner:` field back to `pi` (one-line YAML edit — `pi` is the default runner so even deletion works)
- If the Anthropic path underdelivers, we keep pi as the default indefinitely — no forced migration

---

## Canary Results — `daily-book-report` (flipped 2026-04-17)

**Both nodes flipped** (not just `compose_and_send`). The pi version of `fetch` had been truncating its JSON re-emission of the large NYT bash output — `thinking: off` + GLM-5.1 ran out of output budget mid-string, blocking the DAG. Cleaner canary was to flip both nodes at once.

| Node | Model | Thinking | Tools | Duration | Tokens in/out |
|---|---|---|---|---|---|
| `fetch` | `claude-sonnet-4-6` | disabled | `[Bash]` | 18s | 3 / 1610 |
| `compose_and_send` | `claude-opus-4-7` | adaptive, `effort: high` | `[Bash, Write, Read]` | 114s | 9 / 9565 |

**Subscription-covered** — SDK reports nominal API-rate cost ($0.12 + $0.50 = $0.62/run) but actual billing is $0. All runs land under `~/.claude/.credentials.json` auth, same quota pool as interactive Claude Code.

**Output quality (first canary run, 2026-04-17, delivered to test recipient):**
- Clean HTML email, dark theme, three top-5 bestseller tables (Fiction / Nonfiction / Advice) with rank, movement, weeks
- Five Writing Articles with proper hyperlinks + dates, one Trending entry
- "Daily Writing Spark" tied to a real pattern in *this week's* data (three new debuts in top-5 Fiction) with a concrete 15-minute micro-goal, signed off as Echo
- Markdown report saved to `/Users/dotdev/reports/book-sales/YYYY-MM-DD.md`

**Live `--verbose` output worked end-to-end** — per-event stream showed Bash tool calls, tool results, text fragments, token counts, agent-end timings for both nodes.

**Observations to watch over the ≥2 week canary window:**
- Rate-limit behavior under the 5-hour subscription quota when this runs alongside interactive Claude Code sessions
- Consistency of the "Daily Writing Spark" across days — does Opus 4.7 always tie it to a concrete data observation?
- Any `transient:` or `permanent:` error prefixes surfacing in morning reports
- Output token variance — first run hit 9565 out; flag if we drift toward context-window limits

**Rollback path**: `~/.agentwire/workflows/defs/daily-book-report.yaml` — delete the `runner:`/`model:`/`effort:`/`thinking_config:` lines and replace `tools: [Bash, Write, Read]` → `tools: [bash, write, read]` and add back `thinking: medium`. `pi` is the default runner, so field absence reverts.

---

## Closeout (2026-04-21)

### What shipped beyond the original scope

The MVP in the mission scope covered runner abstraction, the Anthropic runner, event translation, storage v2, CLI override (`--runner`), and the canary. All landed. A handful of additional deliverables emerged from real usage during the 5-day build and are now part of Phase 6:

| Addition | Why it emerged | PR |
|---|---|---|
| **Portal workflow history window** — sidebar section + detail window, reads runs from disk | The original "SSE live UI" stretch item morphed — email + CLI already covered live observation; what was missing was a portal view of *past* runs for diagnosis | #102 |
| **`agentwire brave` CLI** — Brave Search API wrapped in an LLM-optimized command | Three-way A/B (2026-04-21) showed the SDK's built-in `WebSearch` tool was the research-quality bottleneck. Swapping to Brave via bash made Opus 4.7 the clear best. Codified as reusable tooling for every research workflow going forward. | #105 |
| **Multi-recipient email** (`--to` accepts list/comma-split/repeat) | Stargazing was silently sending to only one of two recipients because `--to` was scalar argparse | #101 |
| **Tmux env-inject sweep** — `tmux new-session -e K=V` replaces post-hoc `set-environment` across all local + remote session-creation paths | Discovered while spawning pi-zai interactive sessions; the old post-hoc approach left the initial shell without the API key | #99, #100 |
| **Permission-hook bypass for auto/bypass modes** — hook short-circuits when `permission_mode` is `bypassPermissions`/`auto` | The damage-control hook was still prompting in bypass sessions | #98 |
| **Sidebar UI polish** — thin/blended scrollbar, removed nested-scrollbar | Noticed once the workflow history window started rendering 10+ runs | #103, #104 |

### What the mission-doc said was open, and why each is now closed

| Original "still open" item | Resolution |
|---|---|
| Morning-report runner badge (portal) | **Closed by decision.** User doesn't use the portal morning-report view for diagnosis; CLI `workflow history` already shows runner per row. No real user need. |
| SSE live UI for in-flight runs | **Closed by decision.** Email-first delivery + `agentwire workflow run -v` in terminal cover live observability. Portal history window (shipped) handles the "what happened" case. Revisit only if real friction appears. |
| Mid-run control (cost caps, adaptive model switching, clean cancel) | **Deferred.** Subscription-covered runs mean no cost pressure. No concrete use case yet. Moved to "revisit if Phase 4 surfaces a specific trigger." |

### Canary findings after 5 days of production use

Runs executing daily since 2026-04-17:

- **`daily-book-report`** (Opus 4.7, daily 13:30) — zero failures; output quality consistently high; "Daily Writing Spark" section tied to current-week data on every run
- **`stargazing-forecast`** (Opus 4.7 + Brave, daily 12:00) — retooled to use `agentwire brave` + curl on 2026-04-21; delivers structured forecasts with hour-by-hour tables and source-agreement sections
- **`ai-morning-briefing` × 3 variants** (Opus+WebSearch, Opus+Brave, GLM+pi+Brave, all at 08:00–08:10) — A/B running. First head-to-head (2026-04-21) showed **Opus+Brave decisively best**; SDK WebSearch tool is the weak link, not the model. Finding saved in user memory as the default pattern for future research workflows.
- **`roblox-digest`** (Opus 4.7 + Brave, daily 13:00 to 4 family recipients) — family-facing workflow added 2026-04-21 as further validation of the Brave pattern on a new domain

No reliability issues, no rate-limit hits against the 5-hour subscription window, no output-token overflow. The `--runner` override flag was never needed in anger — YAML declarations proved sufficient.

### Lessons carried forward

- **Research tools matter more than model choice.** For any workflow that fetches fresh web data, default to `runner: anthropic` + `tools: [Bash]` + `agentwire brave` — never the SDK's `WebSearch`. See `docs/workflows.md` → "Research tools".
- **Storage independence from notifications** is valuable. The `examples/silent-save.yaml` pattern + portal history view means workflows can be action-only without losing auditability.
- **The engine's extensibility carried most of the weight.** Once the runner abstraction + event translator landed, everything else was mechanical — five distinct new workflows (briefing × 3, roblox, stargazing-retool) shipped in a week with no engine changes.

### Migration state of the scheduler

As of 2026-04-21 the scheduler's enabled tasks are all workflow-backed:

| Task | Schedule | Runner |
|---|---|---|
| ai-morning-briefing | 08:00 daily | anthropic (Opus+WebSearch — the loser, kept for ≥3 more days of data) |
| ai-morning-briefing-opus-brave | 08:05 daily | anthropic (Opus+Brave — current best) |
| ai-morning-briefing-zai | 08:10 daily | pi (GLM+Brave) |
| stargazing-forecast | 12:00 daily | anthropic (Opus+Brave, retooled) |
| roblox-digest | 13:00 daily | anthropic (Opus+Brave) |
| daily-book-report | 13:30 daily | anthropic (Opus+Brave) |

Legacy `task:` scheduler entries (direct tmux+prompt) still exist in `scheduler.yaml` for agentwire-website / agentwire-social and wiki-ingest. Migrating them is deferred as a future cleanup — not part of Phase 6.
