> Living document. Update this, don't create new versions.

# Mission: Agentwire SDK Primitives + Composite Views

Extract the streaming SDK plumbing already proven in two surfaces (`runners/anthropic.py` headless + `repl/textual_app.py` interactive) into a reusable set of primitives, then compose those primitives into new views (fan-out, watch-mode, diff, conversation-tree). The Textual REPL and SDK workflow runner become the first two consumers of the same engine; everything else after that is `client + sink(s)`.

**Status:** Phase 1 shipped (2026-04-26) — `agentwire/sdk/` package now hosts the shared streaming engine. Phase 2-4 pending.
**Depends on:**
- `agentwire-repl-textual.md` (complete) — Textual REPL ships with all the streaming logic this mission extracts
- `pi-harness-overview.md` Phase 6 (complete) — `runners/anthropic.py` is the other proof point
**Blocks:** rich multi-view experiences (fan-out, watch-mode, conversation-tree), portal live-watch of any session, voice-only sinks, channel sinks.

## Why this mission

Today the SDK plumbing exists in two places that solved their own problem and stopped:

| Surface | What it does | Where the plumbing lives |
|---|---|---|
| Textual REPL | Interactive: streams events into RichLog widgets, handles permissions, persists transcripts | `agentwire/repl/textual_app.py` + `agentwire/repl/app.py` (`_StreamRenderState`, `render_message`) |
| Anthropic workflow runner | Headless: streams events into JSONL for the workflow engine | `agentwire/workflows/runners/anthropic.py` + `anthropic_events.py` |

Both wrap `ClaudeSDKClient`. Both translate the same event types (`AssistantMessage`, `UserMessage`, `ResultMessage`, `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, tool_result). Both carry running totals, classify errors, layer system prompts. The duplication is real but manageable today — the cost shows up the moment we want a *third* consumer (portal live-watch, voice-only sink, fan-out N-column view, etc.).

This mission says: pull the shared engine out, then prove it by adding new views that would have been infeasible (or sloppy duplication) under the old shape.

## Architecture at a glance

```
┌──────────────────────────────────────────────────────────────────────┐
│                       agentwire/sdk/ (new package)                    │
│                                                                       │
│  client.py     — ClaudeSDKClient wrapper w/ agentwire defaults        │
│                  (Opus 4.7, adaptive thinking, xhigh effort, MCP,     │
│                  CLAUDE.md/AGENTS.md auto-injection, role layering)   │
│                                                                       │
│  events.py     — typed event classification + tool-call matching      │
│                  (extracted from anthropic_events.py)                 │
│                                                                       │
│  state.py      — _StreamRenderState, sink-agnostic                    │
│                  (extracted from repl/app.py — the loop that turns    │
│                  the SDK event stream into "for-each-event do X")     │
│                                                                       │
│  sinks/                                                               │
│    base.py     — Sink protocol: on_text, on_thinking, on_tool_use,    │
│                  on_tool_result, on_result, on_error                  │
│    textual.py  — wraps existing _RichLogSink / _ActionSink            │
│    jsonl.py    — wraps existing workflow JSONL writer                 │
│    websocket.py — phase 3: pushes events to portal frontend            │
│    voice.py     — phase 4+: filters TextBlocks → agentwire say         │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
   Textual REPL            Workflow runner             Composite views
   (1 client + 1 sink)     (1 client + 1 sink)         (N clients + N sinks)
```

Composite views are what make this worth doing. Once you have *one* engine and a sink protocol, multiplexing N independent SDK conversations into one TUI screen — each with its own model, role, tools, thinking config — becomes a layout problem, not a plumbing problem.

## The first composite view (motivating example): fan-out

```
┌─────────┬─────────┬─────────┐
│ Col 1   │ Col 2   │ Col 3   │
│ ChatLog │ ChatLog │ ChatLog │
│ Action  │ Action  │ Action  │
│ [input] │ [input] │ [input] │  ← override one column
├─────────┴─────────┴─────────┤
│ > master input              │  ← fan-out to all N columns
└─────────────────────────────┘
```

User types into the master input. We `client.query(text)` on each of N independent `ClaudeSDKClient`s simultaneously. Each column streams events into its own column-scoped sink (its own ChatLog + CurrentAction widgets, its own StatusLine totals). The user can:

- Type into a single column's input to redirect just that branch
- Run different models / different roles / different effort per column
- Compare cost + quality side by side, pick the winner with `/promote N`
- Cancel one branch with focused Ctrl+C, all branches with master Ctrl+C

This is the canonical multi-generation A/B that the user described — and the architecture that makes it work cheaply is exactly the engine extraction in Phase 1.

## Phases

### Phase 1 — Primitives extraction (shipped 2026-04-26, PR #144)

Pure refactor. Zero behavior change. The Textual REPL, print mode, and SDK workflow runner all consume the new `agentwire/sdk/` package. All 1128 tests pass; `agentwire/repl/app.py` shrank from 947 → 171 LOC.

**New module: `agentwire/sdk/`**

- `agentwire/sdk/__init__.py` — public exports
- `agentwire/sdk/client.py`
  - `AgentwireSDKClient(model, effort, thinking, role, system_prompt_layers, mcp_enabled, allowed_tools, permission_mode)`
  - Defaults: `claude-opus-4-7`, `effort="xhigh"`, `thinking={type: "adaptive"}`
  - Auto-injects CLAUDE.md/AGENTS.md/`.agentwire.yml` system prompt layers
  - Wraps `ClaudeSDKClient.query()` and `.receive_response()` with a typed iterator
- `agentwire/sdk/events.py` — extracted from `workflows/runners/anthropic_events.py`. Each SDK message → typed `AgentwireEvent` (text-delta, thinking-delta, tool-use, tool-result, result, error). Tool-call matching (use ↔ result) lives here.
- `agentwire/sdk/state.py` — extracted from `repl/app.py:_StreamRenderState`. Sink-agnostic state machine that walks the event stream, hands each typed event to the sink(s).
- `agentwire/sdk/sinks/base.py`
  ```python
  class Sink(Protocol):
      def on_text(self, text: str, *, finalized: bool) -> None: ...
      def on_thinking(self, text: str) -> None: ...
      def on_tool_use(self, tool: ToolUse) -> None: ...
      def on_tool_result(self, result: ToolResult) -> None: ...
      def on_result(self, totals: SessionTotals) -> None: ...
      def on_error(self, err: SDKError) -> None: ...
  ```
- `agentwire/sdk/sinks/textual.py` — thin wrapper around the existing `_RichLogSink` + `_ActionSink` widgets (no behavior change, just rehoused)
- `agentwire/sdk/sinks/jsonl.py` — wraps the existing workflow JSONL writer

**Refactors (no behavior change)**

- `agentwire/repl/textual_app.py` consumes `AgentwireSDKClient` + `TextualSink`
- `agentwire/repl/app.py` print-mode consumes the same client (sink is a stdout writer)
- `agentwire/workflows/runners/anthropic.py` consumes `AgentwireSDKClient` + `JsonlSink`
- `anthropic_events.py` becomes a re-export shim or is deleted entirely (we don't need backwards-compat)

**Tests**

- All existing 61 textual REPL tests pass
- All existing workflow runner tests pass
- New: `tests/unit/test_sdk_*.py` covering client defaults, event classification, state machine transitions, sink protocol contract

**Success criteria**

- `agentwire repl` and a workflow run with `runner: anthropic` produce byte-identical output to before
- New `agentwire/sdk/` is the only place these concerns live
- Diffstat shows net code reduction (or close to neutral) once duplication is removed

### Phase 2 — Fan-out N-column view (target: 1-2 weeks)

The motivating composite view. Proves the primitives are actually reusable.

- New view module: `agentwire/repl/views/fanout.py`
- `/view fanout cols=3` slash command (default chat view is `/view chat`)
- N `AgentwireSDKClient` instances, each with own `_StreamRenderState`, own `TextualSink` pointing at its column's widgets
- Master input + per-column input
- Per-column config: `/col 1 model=claude-opus-4-7 effort=max`, `/col 2 model=claude-sonnet-4-6`, `/col 3 role=skeptic`
- Cancellation: master Ctrl+C cancels all N; focused-column Ctrl+C cancels one
- `/promote N` makes column N the primary; archives the others
- Cost ceiling: optional `--budget=$X` per master turn, auto-cancels straggler if winner is found
- Tests + snapshot

**Open questions for Phase 2**

- Hermetic columns vs. shared upstream context (e.g., shared Read results, independent reasoning)? Default hermetic.
- Permission prompts in `sdk-prompted` mode: consolidated queue, or per-column modal? Probably consolidated.
- Persistence: independent transcript per column (one session each)? Yes.

**Success criteria**

- User runs the same prompt across Opus 4.7 / Sonnet 4.6 / Opus 4.7+different-role in three columns and picks the best output
- Per-column cancellation works correctly under live streaming
- `/promote` cleanly migrates the winning column to a primary session

### Phase 3 — WebSocket sink + portal watch mode (target: 1-2 weeks)

The portal already lists sessions. Now any session's live SDK event stream can be watched in the browser.

- `agentwire/sdk/sinks/websocket.py` — pushes typed events over WS
- New portal endpoint: `GET /api/sessions/<id>/events` (SSE or WS) streams the same `AgentwireEvent` shapes
- Frontend renderer: same TextBlock / ThinkingBlock / ToolUseBlock semantics the Textual REPL renders, but in HTML/CSS
- Tail-mode: catch up from a transcript file, then stream live (rough analog to `tail -f`)
- Multi-tab safety: N watchers don't multiply load on the SDK client (one stream → fan-out at the sink)

**Open questions**

- Read-only watch vs. interactive control (the latter implies the watcher can submit a turn — a permission boundary). Default read-only for Phase 3.
- Auth: the portal already has a per-session auth model; reuse it.
- Backpressure: long thinking blocks shouldn't blow up the WS buffer. Drop to text-only summaries if a watcher is slow.

**Success criteria**

- A user opens portal, picks a running `agentwire repl` session, sees the same events the user-with-the-pane sees, in real time
- Tail-and-stream works across pane death (transcript file is the SSOT)
- Workflow runs are watchable too — same plumbing

### Phase 4+ — Additional views (trigger-driven)

Each is its own sub-PR. Ship the highest-value one first based on real usage from Phases 2-3.

| View | What it does | Trigger to build |
|---|---|---|
| Diff view | Two columns, same prompt, different model/role/effort, side-by-side comparison with shared StatusLine | We notice we use fan-out=2 for this exact pattern repeatedly |
| Multi-tool-pane | Main chat + filtered sinks per tool type ("currently running Bash" pane, "thinking trace" pane) | Long tool-heavy turns where chat scrollback hides what's running |
| Conversation-tree | Branch a turn N ways, explore alternatives without losing parent (parent-child with promote/discard) | We find ourselves wanting to try-and-revert mid-session |
| Workflow visualizer | Each workflow node = one column; live node states + outputs as DAG executes | Workflows grow complex enough that JSONL tail isn't enough |
| Voice sink | Filters TextBlocks → `agentwire say` | Someone asks for "read me the answer" mode |
| Channel sink | Batches finalized turns into Slack/Discord messages | A channel-bound workflow needs human-in-the-loop visibility |

These are deliberately listed without timelines. Phase 1 ships the engine; Phase 2 proves it; Phase 3 opens the portal; everything after is shaped by which views earn use.

## Customizability dimensions (the flexible surface we prototype through)

Not all answered in Phase 1. These are *where* differentiation emerges as real usage shapes it:

1. **Sink composition**: can a single client have multiple sinks (e.g., textual + jsonl simultaneously, so every interactive turn also persists to the workflow transcript format)?
2. **Per-column tool restriction in fan-out**: do columns share allowed_tools, or can column 1 be Read-only while column 2 has full Bash?
3. **System-prompt layering precedence**: base + role + CLAUDE.md + AGENTS.md + `.agentwire.yml` + per-session — what order, what wins on conflict?
4. **Hook points**: pre-turn / post-turn / pre-tool / post-tool / pre-exit for voice, notification, audit, logging.
5. **MCP tool filtering at the client level**: project-config-driven allow/deny list?

These stay open deliberately. Prototype first, decide from real usage.

## Open questions

- **Sink protocol shape**: pure callback (current sketch) vs. async iterator vs. observable. Decide at Phase 1 kickoff. Pure callback is simplest, matches existing code.
- **Multi-client orchestration in fan-out**: shared event bus (one `_StreamRenderState` per client) vs. one supervisor state machine that owns N children. Decide at Phase 2 kickoff.
- **WebSocket protocol**: framed JSON-per-event vs. SSE. Probably SSE — simpler, browser-native, the portal already speaks WS but SSE for events is cleaner.
- **Backpressure on slow sinks**: if the WS sink falls behind, do we drop frames, buffer, or pause the SDK stream? Almost certainly drop-with-summary.
- **Cost accounting across multiple clients**: per-client (existing), aggregated per-view, both? Probably both surfaces.

## Non-goals (permanent)

- Replacing `ClaudeSDKClient` itself — we wrap it, never reimplement
- Provider abstraction — Anthropic SDK only, by architecture (consistent with `agentwire-repl.md`)
- Plugin system for arbitrary user sinks — we ship the sinks we need; users can fork
- Distributed multi-machine fan-out — Phase 2 is single-process; multi-machine is a separate mission if it earns it
- Full conversation-graph DB — transcripts stay flat JSONL per session; tree views are derived

## Dependencies

- `claude-agent-sdk>=0.1.0` (already in `pyproject.toml`)
- `textual>=0.80` (already)
- `rich>=13.0` (already)
- Portal frontend (Phase 3 only)

## Code references (study / refactor — not copy blindly)

- `agentwire/workflows/runners/anthropic.py` — current SDK init + streaming pattern
- `agentwire/workflows/runners/anthropic_events.py` — current event classification
- `agentwire/workflows/runners/anthropic_capabilities.py` — model/effort/thinking validation (stays as-is)
- `agentwire/repl/textual_app.py` — current `_RichLogSink`, `_ActionSink`, `_StreamRenderState` integration
- `agentwire/repl/app.py` — print mode + render helpers
- `agentwire/workflows/storage.py` — transcript JSONL pattern (sink target)
- `agentwire/server.py` — portal endpoints (Phase 3 target)

## Success criteria (aggregated)

- **Phase 1**: REPL + workflow runner both consume `agentwire/sdk/`; existing tests green; net LOC reduction
- **Phase 2**: Fan-out 3-column view shipped, daily-driver-grade for multi-generation A/B prompting
- **Phase 3**: Portal can live-watch any running SDK session; demo-able to a non-user in <30 seconds
- **Phase 4+**: at least one view beyond fan-out earns daily use, validating the composability premise

## Pitfalls

- **Premature abstraction**. The Sink protocol must serve the views we're actually building. If Phase 1's protocol shape doesn't fit Phase 2's fan-out cleanly, *change the protocol* in Phase 2 — don't bend Phase 2 to fit it.
- **Refactor scope creep**. Phase 1 is a refactor with zero behavior change. If a tempting feature appears mid-extraction ("while I'm here, let me also..."), defer to Phase 2+.
- **Test coverage regression**. The Textual REPL has 61 tests; the workflow runner has its own. Phase 1 must not lose any. Add new tests for the new modules; don't delete existing ones.
- **Multi-client lifecycle bugs in fan-out**. N concurrent SDK clients = N concurrent event loops = N places to leak. Use Textual's `run_worker(exclusive=False)` per column, with explicit cleanup on view exit.
- **Permission UX in fan-out**. Modal-per-column gets noisy fast. If we ship `sdk-prompted` fan-out, consolidate prompts.
- **WebSocket fan-out load**. One SDK stream per session, N watchers. Don't accidentally make the SDK do N×work.

## Revisit checklist

Every ~30 days of production usage of Phase 2+ views:

- Which views earned daily use? Which didn't?
- Which open questions are now answered? Record decisions inline.
- Which Phase 4+ items now have triggers? Move to scoped work.
- Did the Sink protocol need to change? Why?

## File plan for the mission-doc PR (this PR)

| File | Change |
|---|---|
| `docs/missions/agentwire-sdk-primitives.md` | **NEW** — the mission scope above |
| `docs/missions/agentwire-repl-textual.md` | Cross-link: "follow-on work consolidating the streaming engine lives in `agentwire-sdk-primitives.md`" |
| `docs/missions/pi-harness-overview.md` | Cross-link: "Phase 6's `runners/anthropic.py` is one of two consumers feeding `agentwire-sdk-primitives.md`" |
