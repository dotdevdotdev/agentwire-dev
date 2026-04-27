> Living document. Update this, don't create new versions.

# Mission: Agentwire REPL — Textual TUI Rewrite

A from-scratch rendering layer for `agentwire repl` built on [Textual](https://github.com/textualize/textual). The current `prompt_toolkit` line-oriented loop hit its ceiling: there's no way to keep streaming partial output visible without flooding the chat history, no proportional layout, no borders/titles, no docked status line, no scrollable subregion for "what claude is doing right now". Textual gives all of that out of the box.

**Phase of:** own mission (sibling of `agentwire-repl.md`)
**Status:** **Mission complete (2026-04-26).** Phase 1A → 3E shipped, plus the default-flip cleanup (#140) and brand theming (#141, #142) — `_run_interactive`, `prompt_toolkit`, `AGENTWIRE_REPL_TUI` env flag, and the dispatch fork are all gone. `agentwire repl` is the Textual TUI; print mode (`-p`) keeps the stdout single-shot path. See `docs/repl-tui.md` for the user-facing walkthrough.
**Follow-on:** the streaming engine that powers this REPL is being extracted into reusable primitives — see `agentwire-sdk-primitives.md` for that scope (fan-out N-column view, portal watch-mode, etc.).
**Depends on:** `agentwire-repl.md` Phases 1-5 (complete) + streaming-visibility quick fixes (#123-#125 shipped 2026-04-25)
**Blocks:** future REPL feature work that needs richer layout (mode badges, modal permission prompts, inline waveform for /say, etc.)

## Why this rewrite

The user's framing (2026-04-25): *"we can stream the partials, but we need a way to keep them contained, they should get only some percentage of total height lines we have in the current display, so the input is always ideal near the bottom (with maybe a status line under it if we add one) and the chat history above and all the way to the top. But we could add the 'current action' as like 20% of the height we have to work with and have the text scroll there like in a subpane."*

That's a layout requirement, not a rendering tweak. `prompt_toolkit` *can* express it — `HSplit([history, Frame(action, title="..."), input], weights=[6, 2, 1])` — but our current code uses prompt_toolkit as a line-oriented prompt only (events go to raw stdout, prompt_toolkit owns just the input buffer). Adding a real layout means rewriting the rendering loop end-to-end. If we're rewriting it anyway, Textual is the better target:

| | prompt_toolkit (current) | Textual (proposed) |
|---|---|---|
| Layout primitives | low-level (HSplit/VSplit/Window) | declarative (Vertical/Horizontal/Grid + CSS) |
| Borders + titles | `Frame` widget | `Static`/`Container` + CSS border |
| Scrollable subregion | manual | `RichLog` built for it |
| Status line | manual | `Footer` + dock="bottom" |
| Modals (e.g. permission prompts) | hand-rolled | `Screen.push_screen` |
| Async event integration | yes | first-class |
| Dev tools | none | `textual console`, snapshot tests |
| Stars / momentum | 9k, mature | 25k, very active |
| Cost of "do nothing fancy" | high | medium |

Textual loses terminal scrollback by default (it's a full-screen app), but inside a tmux pane that doesn't matter — and the in-app history widget is its own infinite scroll. We gain way more than we lose.

## Architecture sketch

```
┌─ AgentwireREPL (Textual App) ─────────────────────────────┐
│ ┌─ Header ───────────────────────────────────────────────┐ │
│ │ agentwire repl · sdk-bypass · claude-opus-4-7 · roles… │ │
│ └────────────────────────────────────────────────────────┘ │
│ ┌─ ChatHistory (RichLog, fr=6) ──────────────────────────┐ │
│ │ > previous user turn                                   │ │
│ │ assistant final text                                   │ │
│ │ [tool: Bash → ls /tmp]                                 │ │
│ │ [tool result: ...]                                     │ │
│ │ assistant final text                                   │ │
│ │ ↑ scrolls; pinned to bottom on new content             │ │
│ └────────────────────────────────────────────────────────┘ │
│ ┌─ CurrentAction (RichLog, fr=2, border, title) ─────────┐ │
│ │ ╭─ Current action ─────────────────────────────────╮   │ │
│ │ │ thinking: planning the file structure...        │   │ │
│ │ │ thinking: deciding section ordering...          │   │ │
│ │ │ tool input (Write fanfic.html):                  │   │ │
│ │ │   <!doctype html><html><head><meta charset...   │   │ │
│ │ │   <style>body { font-family: ... }</style>      │   │ │
│ │ │   ↑ streams in real time, scrolls within frame  │   │ │
│ │ ╰──────────────────────────────────────────────────╯   │ │
│ └────────────────────────────────────────────────────────┘ │
│ ┌─ Input (TextArea, dock=bottom) ────────────────────────┐ │
│ │ > tell me about prompt caching                         │ │
│ └────────────────────────────────────────────────────────┘ │
│ ┌─ StatusLine (Footer, dock=bottom) ─────────────────────┐ │
│ │ 3 turns · 1.2k tok · $0.04 · effort=high · af_heart    │ │
│ └────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

- **ChatHistory** — finalized turns. Tool calls collapse to one-line summaries.
- **CurrentAction** — live partial-message stream. Cleared at end of turn (the assistant block + tool result get appended to ChatHistory in collapsed form).
- **Input** — multi-line `TextArea`, Alt+Enter newline, Enter submit. `@`-mention auto-complete from `Glob` against cwd would be a nice phase-2 add.
- **StatusLine** — running totals, mode, role, voice. Replaces the bottom_toolbar.
- **Modals** — `sdk-prompted` permission prompts pop a `ModalScreen` instead of inline y/n/a.

## Phases

### Phase 1 — Parity (target: 1 wk)

Reach feature parity with the current prompt_toolkit REPL.

- Textual app skeleton (`agentwire/repl/textual_app.py`)
- ChatHistory + Input wired; SDK client streams events into ChatHistory
- All slash commands work (`/help`, `/cost`, `/tools`, `/model`, `/clear`, `/save`, `/resume`, `/effort`, `/thinking`, `/say`, `/run-workflow`, `/exit`)
- @-mention expansion (existing `agentwire.repl.mentions` reused)
- Transcript persistence (existing `agentwire.repl.persistence` reused)
- MCP, damage-control, role layering, voice (all already in `build_options`, no changes)
- Print mode (`-p`) keeps the existing single-shot path — Textual app only fires for interactive
- TTY detection: non-TTY (piped stdin, scheduler) falls back to a non-Textual path that mimics today's behavior. **Required** — otherwise we break workflow `human_gate` and the print-mode contract.
- **Success criteria:** every test in `tests/unit/test_repl_*.py` still passes (with adjusted fixtures); the seeded smoke test (echo prompt | agentwire repl) prints assistant content; an interactive Opus 4.7 session feels at least as good as today's.

### Phase 2 — Layout features (target: 1 wk)

What this rewrite was actually for.

- **CurrentAction RichLog** with border + title — partial messages stream here in real time, cleared on turn complete
- **Proportional weights** — chat=6 / action=2 / input=1 by default, configurable via `/layout` slash command
- **Header** — session metadata pinned at top
- **StatusLine (Footer)** — running totals, refreshed every event
- **Tool-call collapse** — finished tool calls in ChatHistory show `[Bash · ls -la · 12 files]` not the full streamed input
- **Permission prompts as modal** — `sdk-prompted` pops `ModalScreen` with allow/deny/always buttons
- **Color theming** — pull `tts.theme` (or new `repl.theme`) from `~/.agentwire/config.yaml`
- **Success criteria:** the 120-second silent gap that prompted this mission is gone; user always knows what claude is doing

### Phase 3 — Polish (target: ~1 wk)

- @-mention autocomplete (Glob against cwd, dropdown overlay)
- Slash-command palette (`Ctrl+P` opens fuzzy command picker)
- Inline cost graph (sparkline of per-turn cost)
- Transcript scrubber (jump to any prior turn)
- Snapshot tests using Textual's snapshot framework
- Doc: short `docs/repl-tui.md` walkthrough with screenshots
- **Success criteria:** the REPL is good enough to use over Claude Code for agentwire-network-flavored work — that's the original mission's bar

### Phase 4+ — Trigger-driven

Same posture as `agentwire-repl.md` Phase 6+. Don't pre-build:

- Multiplexer view (multiple SDK sessions in one Textual app)
- Workflow-run inline (run a workflow inside the REPL with live node-state cards)
- Inline waveform for /say
- Plugin widgets

## Open questions

- **Migration strategy:** ship Textual behind a flag (`AGENTWIRE_REPL_TUI=textual`) and run side-by-side until parity is proven, then flip default? Or branch-and-replace? Leaning side-by-side — the current REPL is good enough that we don't need to remove it on day 1.
- **Print mode:** stays on the prompt_toolkit / stdout path? It has no UX requirements that need Textual. Likely yes — print mode is fire-and-forget, no benefit from a TUI.
- **Workflow human_gate:** does it spawn the Textual app or stay on the simple line-mode? Probably needs Textual for parity once Textual is the default — humans reviewing a gate want the same UX they get in standalone REPL. But Textual-from-inside-a-workflow has tmux/TTY constraints to think through.
- **Resume / scroll-back of long transcripts:** `RichLog` handles this natively; verify with a 10K-line transcript.
- **Snapshot tests:** Textual has `pytest-textual-snapshot`. Worth adding to the test stack as part of Phase 3.

## Non-goals

- Replacing `pi` or `claude` REPLs — agentwire repl stays the agentwire-native harness; the others stay as they are.
- Cross-platform Windows-native polish — best-effort, but Linux + macOS terminals are the target.
- Rewriting the workflow `human_gate` runner — it just calls `run_repl(seed_message=...)`, which will route to whichever REPL implementation is active.

## Code references (for the rewrite)

- `agentwire/repl/app.py` — current entry point. The `_run_interactive` function is what gets replaced; everything from `build_options` downward is reused.
- `agentwire/repl/state.py` — `ReplState` dataclass; carries through unchanged.
- `agentwire/repl/commands.py` — slash commands; reused unchanged.
- `agentwire/repl/persistence.py` — transcript JSONL; reused unchanged.
- `agentwire/repl/mentions.py` — @-mention expansion; reused unchanged.
- `agentwire/sdk/damage_control.py` — `make_pre_tool_hook`; reused unchanged.
- `agentwire/repl/context.py` — role/voice loading; reused unchanged.

The rewrite is the rendering layer only. Everything below is intact.

## Dependencies

- `textual >= 0.80` (latest stable as of 2026-04)
- `pytest-textual-snapshot` (test-only, Phase 3)

---

# Phase 1 — Implementation Plan: Parity

> **Status:** ready to execute (planned 2026-04-25). Behind `AGENTWIRE_REPL_TUI=textual` flag for the entire phase. Default-flip is a follow-up PR after a parity-soak window.

## PR breakdown

1. **PR 1A — Plumbing**: feature flag, dispatcher fork, TTY detection, dependency add. No Textual code yet; just the entry-point branch + a `textual_app.run_textual_repl(...)` stub that raises `NotImplementedError`.
2. **PR 1B — Skeleton app**: Textual `App` with `RichLog` + `TextArea` + `Footer`, SDK worker pumping messages into the log via `app.post_message`, slash dispatch wired through. No persistence, no mentions, no permissions yet.
3. **PR 1C — Parity wiring**: transcript persistence, `@`-mentions, `seed_message`, `/clear` + `/resume` lifecycle (close/reopen client), `sdk-prompted` inline y/n/a, `track_result`/`track_system_init`, banner.
4. **PR 1D — Test pass + flag-flip readiness**: unit tests via Textual's `Pilot` headless runner, manual smoke verification, this plan's "Phase 1 — Implementation Plan" section becomes "shipped".

Each PR is independently reviewable and shippable; the flag stays `off` until a separate default-flip PR after Phase 1+2 prove out.

## PR 1A — Plumbing (entry-point fork)

**Goal.** Add `AGENTWIRE_REPL_TUI=textual` feature flag and a TTY-aware dispatcher in `run_repl`.

**Files.**
- `pyproject.toml` — add `textual>=0.80` to dependencies.
- `agentwire/repl/app.py` — add `_should_use_textual()` and dispatch fork.
- `agentwire/repl/textual_app.py` *(new)* — stub with `async def run_textual_repl(...)` raising `NotImplementedError`.

**Approach.** Decision rule, in order:
1. `print_prompt is not None` → existing `_run_print_mode` (Textual is **never** used in print mode — print stays a single-shot stdout pipe; that contract is load-bearing for `agentwire workflow run`, scheduler tasks, and `human_gate`).
2. Not TTY (`sys.stdin.isatty() and sys.stdout.isatty()` False) → existing `_run_interactive`. Protects piped stdin, scheduler, captured stdout, `human_gate` non-interactive seeds.
3. Flag set + `import textual` succeeds → `run_textual_repl(...)`.
4. Else → existing `_run_interactive`.

Lazy `import textual` inside the dispatcher keeps it optional during rollout — if the user has the flag set but no `textual` installed, fall back rather than crash.

**Test plan.** New `tests/unit/test_repl_dispatch.py`: flag off → `_run_interactive`; flag on + TTY mocked True → `run_textual_repl`; flag on + non-TTY → `_run_interactive`; print mode short-circuits regardless.

**Success criteria.** `pytest` green; unset flag = identical behavior; `AGENTWIRE_REPL_TUI=textual agentwire repl` raises `NotImplementedError` from the stub (proves dispatch).

## PR 1B — Textual skeleton with streaming SDK worker

**Goal.** Replace stub with a working app: RichLog + TextArea + Footer, SDK worker pipes events into log, slash commands work. No persistence/mentions/permissions yet.

**Files.**
- `agentwire/repl/textual_app.py` — full implementation.

**Event flow.**
```
User types → TextArea Submitted → app.action_send(text) → app.run_worker(_run_turn(text), exclusive=True)
                                                            ↓
                                                       client.query(text)
                                                       async for msg in _heartbeat_iter(client.receive_response()):
                                                           self.post_message(SdkEvent(msg))   # thread-safe
                                                           
App.on_sdk_event(SdkEvent) → render_message(msg, ..., out=_RichLogSink) on UI thread
```

`SdkEvent` is a Textual `Message` subclass. All rendering on UI thread; workers never touch widgets directly. `exclusive=True` cancels any in-flight turn (matches today's Ctrl+C).

**Streaming adapter — `_RichLogSink`.** Buffers writes until `\n`, then `RichLog.write(Text.from_ansi(line))`. Existing `_styled()` ANSI emits are parsed faithfully — Rich markup translates 1:1. `_RichLogSink.isatty() → True` so `_styled()` keeps emitting ANSI.

The byte-counter `\r\033[K` rewrite needs special handling: when buffered content begins with `\r\033[K`, pop the last RichLog line before writing the new one. Verify against installed Textual; fall back to "every tick is a new line" if `RichLog.lines.pop()` is unsupported.

**Layout (Phase 1 — minimal).**
```python
CSS = """
Screen { layout: vertical; }
#chat { height: 1fr; border: tall $accent; }
#input { dock: bottom; height: 5; }
Footer { dock: bottom; }
"""
```
`TextArea` (not `Input`) so Alt+Enter newline + Enter submit work. Phase 2 splits chat into chat + current-action with proportional weights.

**Slash dispatch.** `TextArea.Submitted` (or our action) checks for `/` prefix, calls existing `dispatch_command(text, state, sink)`. `commands.py` writes to `out: TextIO` — our `_RichLogSink` quacks fine. Zero changes to `commands.py`.

**Heartbeat.** Phase 1 keeps heartbeats in the same `RichLog`; Phase 2 docks them in CurrentAction.

**Test plan.** `tests/unit/test_repl_textual_app.py` using Textual's `App.run_test()` async pilot:
```python
async with AgentwireREPL(...mock client...).run_test() as pilot:
    await pilot.press(*"hi"); await pilot.press("enter")
    assert "hi" in app.query_one("#chat", RichLog).lines[-1].text
```
Mock `claude_agent_sdk.ClaudeSDKClient` with a fake whose `receive_response()` yields a recorded sequence. Slash test: submit `/help`, assert "Available commands" in chat. ANSI parse test: write `\x1b[1mhello\x1b[0m\n` to sink, assert bold style on appended line.

**Pitfalls.**
- Workers MUST use `self.post_message(...)` — never call `RichLog.write` from a worker.
- Cancellation: wrap `_run_turn` body in `try/except asyncio.CancelledError` to print `[turn cancelled]` matching today's Ctrl+C.
- `\r\033[K` byte-counter rewrite: see Streaming adapter above.
- `TextArea` key bindings: `Enter` submits, `Alt+Enter`/`Ctrl+J` insert newline. Match today's UX.

## PR 1C — Persistence, mentions, prompted-mode, lifecycle

**Goal.** Full feature parity with `_run_sdk_session`.

**Files.** Modify `agentwire/repl/textual_app.py`.

**What gets wired in.**
1. **Transcript persistence.** `persistence.create_session(...)` in `on_mount`. Each rendered SDK message → `_persist_sdk_message(...)`. Each user turn → `user_input` event before `client.query(...)`. `on_unmount` → `persistence.finalize(transcript, state)` + `transcript.close()`.
2. **`@`-mentions.** In input handler, after slash branch, call `expand_mentions(text, cwd=Path.cwd())`. Print `[expanded N mention(s)]` notice via sink. Pass expanded text to worker; record raw + expanded in transcript event (mirrors `app.py` lines 786-806).
3. **`/clear`, `/resume`, `/effort`, `/thinking` lifecycle.** These return `RESTART`/`RESUME` from `dispatch_command`. Wrap in `_restart_session()` worker:
   - Read + clear `state.pending_resume_sdk_session_id`.
   - `await self._client_ctx.__aexit__(None, None, None)`.
   - Rebuild options with new `resume_sdk_session_id` / `effort` / `thinking_mode`.
   - Open new client.
   - Write `{"type": "restart", ...}` event.
   - `reset_for_restart(state)`.
   This is the outer `while True` loop in `_run_interactive` lines 681-716, lifted into a method.
4. **`sdk-prompted` permission prompt — Phase 1 placeholder.** Inline RichLog y/n/a (Phase 2 → ModalScreen). `state._pending_permission: asyncio.Future` on the app; the SDK's `can_use_tool` callback awaits the future, the input handler resolves it (this branch comes BEFORE slash/turn branches).
5. **Banner.** Mirror `app.py` lines 605-622 — write into RichLog on mount before any turn.

**Pitfalls.**
- Manual `__aenter__`/`__aexit__` for `ClaudeSDKClient` (Textual lifecycle is `on_mount`/`on_unmount`, not `async with`). Wrap unmount in try/except — never raise during shutdown.
- `CLAUDECODE` env var unset/restore (`app.py` lines 674, 718-719). SDK refuses to nest without it.
- `can_use_tool` reentrancy: handle PERMISSION-pending state in input router BEFORE slash/turn branches.
- Worker cancellation must drain the heartbeat iter cleanly — `_heartbeat_iter`'s `finally` already handles it; verify.

## PR 1D — Test pass + pre-flip verification

**Goal.** Round out tests, manual smoke, mark Phase 1 shipped.

**Test patterns.**
- `App.run_test()` returns a `Pilot`. Use `pilot.press(...)`, `pilot.click(...)`, `pilot.pause()`.
- Inspect via `app.query_one("#chat", RichLog).lines` (each entry's `.text` is readable).
- Mock SDK by patching `claude_agent_sdk.ClaudeSDKClient` to return a fake whose `receive_response()` is a pre-recorded async iterator (same fake-class pattern as `test_repl_sdk.py::FakeOptions`).
- Async tests under `pytest-asyncio`'s `asyncio_mode = auto`.

**Coverage targets.**
- Persistence: golden-test transcript shape vs current REPL (diff empty).
- `@`-mention expansion + transcript event with `mentions[]`.
- `/clear` increments `state.restart_count`, writes `restart` event.
- `sdk-prompted` placeholder: mock tool-use, pilot answers `y`, assert allow.
- Manual smoke: 5KB Write tool input, observe byte-counter ticking and tool-call summary; compare with-flag vs without-flag for behavior parity.

**Don't add `pytest-textual-snapshot` here** — that's Phase 3. Snapshot maintenance overhead before parity is shipped is premature.

## Phase 1 ship status (2026-04-25)

Phase 1 is feature-complete behind the flag. To flip the default in Phase 2D, the soak window must validate:

- [ ] No regressions vs legacy REPL across a 1-week daily-driver window
- [ ] `transcript.jsonl` shape matches legacy (verified via `diff` of golden runs)
- [ ] `human_gate` non-TTY callers stay on the line-mode path (test_repl_dispatch covers this)
- [ ] Multi-line input (Alt+Enter) works — currently `Input` is single-line only; Phase 2A swaps to `TextArea` when redesigning the input region
- [ ] Live partial streaming reads cleanly — currently choppy; Phase 2A's CurrentAction subpane fixes this

When all green: Phase 2D removes the `AGENTWIRE_REPL_TUI` env check, makes `textual` a hard dep, deletes `_run_interactive` (keeping `_run_print_mode` since print mode never moves to Textual).

## What stays unchanged across all of Phase 1

These files are imported and reused as-is — no edits:
- `agentwire/repl/state.py` (`ReplState`)
- `agentwire/repl/commands.py` (slash handlers — `out: TextIO` signature already compatible with `_RichLogSink`)
- `agentwire/repl/persistence.py` (transcript JSONL)
- `agentwire/repl/mentions.py` (`@path` expansion)
- `agentwire/sdk/damage_control.py` (`make_pre_tool_hook`)
- `agentwire/repl/context.py` (role/voice loading)
- `agentwire/__main__.py::cmd_repl` and `repl_parser` (no CLI changes; flag is purely env)
- `_run_print_mode`, `_run_interactive`, `build_options`, `render_message`, `_StreamRenderState`, `_heartbeat_iter` — remain in `app.py`, imported by both paths.

---

# Phase 2 — Implementation Plan: Layout features

> **Status:** planned (2026-04-25). Begins after Phase 1 ships and the flag-default flip lands. Where this rewrite earns its keep.

## PR breakdown

1. **PR 2A — Layout split**: introduce CurrentAction subpane (`fr=2`), chat pane (`fr=6`), input (3 lines). Partials route to CurrentAction; finalized turns route to chat.
2. **PR 2B — Header + StatusLine**: session metadata Header pinned top; custom StatusLine Footer reading running totals from `state` every event.
3. **PR 2C — Tool-call collapse + permission ModalScreen**: finalized tool calls in chat collapse to one-liners (`[Bash · ls -la · 12 files]`); `sdk-prompted` permission prompts replace inline placeholder with a centered ModalScreen.
4. **PR 2D — Theming + `/layout` slash command + flag default flip**: pull `repl.theme` from `~/.agentwire/config.yaml`; new `/layout` slash command tweaks `chat=N action=M`; flip `AGENTWIRE_REPL_TUI=textual` default ON, make textual a hard runtime dep, delete `_run_interactive`.

## PR 2A — Layout split (CurrentAction subpane)

**Goal.** Partial-message stream lives in its own scrollable bordered subpane; chat history shows finalized turns only.

**Files.**
- `agentwire/repl/textual_app.py` — split layout; route partials to CurrentAction widget.
- `agentwire/repl/textual_render.py` *(new — extract from `app.py`)* — owns the routing decision (partial vs finalized, which sink to write to).

**Layout.**
```css
#chat { height: 6fr; border: tall $accent; border-title-color: $text 50%; }
#action { height: 2fr; border: tall $warning; border-title: "Current action"; }
#input { dock: bottom; height: 3; }
Header { dock: top; }
Footer { dock: bottom; }
```

**Routing rule.** `_StreamRenderState` partial events → `_action_sink` (CurrentAction RichLog). Finalized snapshot Messages (`AssistantMessage`, `UserMessage`, `ResultMessage`, `SystemMessage`) → `_chat_sink`. On `content_block_stop` for `text` block: action pane gets a fade marker, chat pane gets the final snapshot rendering.

**Action pane lifecycle.** Cleared on `ResultMessage` (turn complete). Tool-input byte counter, thinking, partial assistant text all live here. Heartbeat docks here too (no longer in chat).

**`render_message` signature change.** Accept `chat_out` and `action_out` instead of single `out`. Call sites in `_run_interactive` (existing path) pass the same sink for both — no behavior change for the existing path. `textual_app.py` passes the two distinct sinks.

**Test plan.** Pilot tests: partial event → asserts `query_one("#action", RichLog).lines` non-empty, `#chat` empty. Finalize → asserts `#chat` has snapshot, `#action` cleared.

**Success criteria.** The 120-second silent gap is gone — partials are always visible in CurrentAction; chat shows only the settled, scannable turn history.

## PR 2B — Header + StatusLine

**Goal.** Session metadata at top; running totals at bottom; both refresh on every SDK event.

**Files.**
- `agentwire/repl/textual_app.py` — `Header` content + custom `StatusLine` widget.

**Header.** `agentwire repl · {mode} · {model} · roles: {a, b} · voice: {v}`. Static after mount unless `/model` / `/effort` / role swap. Refresh hook: state-changing slash commands call `app.refresh_header()`.

**StatusLine.** Right-aligned: `{turns} turns · {tok} tok · ${cost:.4f} · effort={e} · thinking={t}`. Reads `state` directly each render. Subscribed to `SdkEvent` so it refreshes on `ResultMessage` (cost/tok update) and on slash-command state changes (`/effort`, `/thinking`).

**Implementation.** Custom `Static` widget docked below `Footer`; `app.bell()` is unaffected. Footer keeps Textual's binding hints — StatusLine sits above it.

**Test plan.** Pilot: simulate ResultMessage with usage → assert StatusLine text matches expected. Submit `/effort low` → assert StatusLine reflects `effort=low`.

## PR 2C — Tool-call collapse + permission ModalScreen

**Goal.** Two independent UX wins.

**Tool-call collapse.** Finalized `[→ Tool {summary}]` lines in chat compress further: `Bash`/`Read`/`Edit`/`Write` get one-line summaries with key arg. The renderer already produces concise summaries via `_format_tool_input`; this PR adds a *result-aware* finalization where `[← result: ...]` immediately following a tool_use gets folded into the same line: `[Bash · ls -la · 12 files]`. Implementation: in `textual_render.py` buffer the last tool_use line; on the matching tool_result, replace the tool_use line with the merged version.

**Permission ModalScreen.** Replace Phase 1's inline y/n/a placeholder with a centered modal:
```python
class PermissionPrompt(ModalScreen[str]):
    BINDINGS = [Binding("y", "decide('allow')"), Binding("n", "decide('deny')"), Binding("a", "decide('always')")]
    def compose(self): yield Static(self.message); yield Horizontal(Button("Allow", id="allow"), Button("Deny"), Button("Always allow"))
```
SDK `can_use_tool` callback `await self.push_screen_wait(PermissionPrompt(...))`. Decision threads back through the SDK's `permissionDecision`. Removes `state._pending_permission` future.

**Test plan.** Pilot: trigger tool use, assert ModalScreen pushed. `pilot.press("y")` → assert allow returned.

**Success criteria.** Tool calls are skim-friendly one-liners; permission prompts are obviously centered modals with keyboard + button paths.

## PR 2D — Theming + /layout + default flip

**Goal.** User customization + retire the legacy path.

**Theming.** Read `repl.theme` from `~/.agentwire/config.yaml`. Map to Textual's design tokens (`$accent`, `$warning`, `$text`). Fall back to default. Tie to existing `tts.theme` if not separately defined.

**`/layout`.** New slash command: `/layout chat=8 action=1` adjusts proportional weights at runtime. Persists for the session only; written to transcript metadata. Validates that weights sum > 0.

**Flag default flip.**
- Default `AGENTWIRE_REPL_TUI=textual` (or remove the env check entirely; flag stays as opt-OUT for legacy path).
- Make `textual>=0.80` a hard runtime dep.
- Delete `_run_interactive` from `app.py`. Keep `_run_print_mode` (print mode never moves to Textual). Migrate the few tests that hit `_run_interactive` directly to the Textual test patterns.
- Document migration in `docs/missions/agentwire-repl-textual.md` ("Phase 2 shipped 2026-XX-XX").

**Pitfalls.** Don't flip the default until Phase 1 + 2A-C have been daily-driven for at least a week. The flip is destructive (deletes `_run_interactive`); a separate PR after a soak window is the right unit.

---

# Phase 3 — Implementation Plan: Polish

> **Status:** planned (2026-04-25). Trigger-driven within the phase — the order below is suggested, not mandatory. Each PR is independent and can ship when it's ready.

## PR breakdown

1. **PR 3A — Snapshot test infra**: add `pytest-textual-snapshot` to dev deps; baseline snapshots for chat / action / status states; CI runs them.
2. **PR 3B — `@`-mention autocomplete**: as the user types `@`, dropdown overlay shows matching files (Glob against cwd).
3. **PR 3C — Slash command palette (`Ctrl+P`)**: fuzzy command picker overlay.
4. **PR 3D — Cost sparkline + transcript scrubber**: per-turn cost mini-graph in StatusLine; jump-to-prior-turn UX.
5. **PR 3E — `docs/repl-tui.md` walkthrough**: short doc with screenshots.

## PR 3A — Snapshot test infrastructure

**Files.** `pyproject.toml` (dev dep `pytest-textual-snapshot`); `tests/snapshot/` directory; baseline `.svg` snapshots committed.

**Approach.** One snapshot test per major UI state: empty REPL boot, mid-turn (partials in CurrentAction), finished turn (settled in chat, cost in StatusLine), permission modal pushed, transcript-scrubbed view (Phase 3D adds the latter).

**CI.** Add `pytest --snapshot-update=no` to existing test run; failures emit a diff page that the dev opens in browser.

## PR 3B — `@`-mention autocomplete

**Files.** `agentwire/repl/textual_app.py` adds `@MentionDropdown` widget (a `OptionList` floated near cursor).

**Approach.** Hook `TextArea.on_change`; when current word starts with `@`, glob `**/{rest}*` from cwd, populate dropdown with top 10 matches sorted by mtime. `Tab`/Enter accepts; Esc dismisses. Reuses `mentions.expand_mentions()` for the actual expansion at submit time — autocomplete is purely UI sugar.

**Test plan.** Pilot: type `@RE`, assert dropdown shows `README.md`. `pilot.press("tab")` → assert TextArea contains `@README.md `.

## PR 3C — Slash command palette (Ctrl+P)

**Files.** `agentwire/repl/textual_app.py` adds `CommandPalette` modal.

**Approach.** Lift command list from `commands.py::COMMANDS` (already a dict). `Ctrl+P` pushes `CommandPalette` modal; fuzzy-filter on input; Enter submits the selected command. Same dispatch path as typed slash command.

**Pitfall.** Don't conflict with Textual's own command palette (Ctrl+\ by default). Use `Ctrl+P` exclusively.

## PR 3D — Cost sparkline + transcript scrubber

**Cost sparkline.** Last N turns' cost as a unicode-block sparkline rendered into StatusLine. `▁▃▅▇█▇▅▃` etc. Updates on `ResultMessage`. Caps at 20 turns of history; older drops off.

**Transcript scrubber.** New slash command `/scrub` opens a `ListView` of prior turns from the current session's transcript JSONL. Selecting a turn jumps the chat scroll position. Read-only — doesn't mutate state.

**Test plan.** Cost: assert sparkline character count == turn count. Scrubber: assert ListView populated from transcript file.

## PR 3E — Walkthrough doc

**Files.** `docs/repl-tui.md` *(new)* — short page with the layout diagram (lifted from this mission), GIFs of: boot, streaming partial in CurrentAction, modal permission prompt, command palette, mention autocomplete, transcript scrubber.

**Capture.** Use `agentwire pane gif` (existing tool) or `asciinema` for the recordings. GIFs live under `docs/assets/repl-tui/`.

---

# Living checklist (update as we ship)

- [x] **Phase 1A** — flag + dispatcher (#127, 2026-04-25)
- [x] **Phase 1B** — Textual skeleton (#128, 2026-04-25; uses `Input` widget — TextArea/multi-line punted to 2A; sink streams partials choppily, clean on close — proper live streaming arrives with the CurrentAction widget in 2A)
- [x] **Phase 1C** — persistence, mentions, prompted mode, lifecycle (#129, 2026-04-25)
- [x] **Phase 1D** — tests + manual smoke (#130, 2026-04-25; 17 textual tests; ready for soak before flag-flip in Phase 2D)
- [x] **Phase 2A** — CurrentAction subpane + proportional weights (#131, 2026-04-25)
- [x] **Phase 2B** — Header + StatusLine (#132, 2026-04-25)
- [x] **Phase 2C** — tool-call collapse + permission ModalScreen (#133, 2026-04-25)
- [x] **Phase 2D** — theming + `/layout` (#134, 2026-04-25)
- [x] **Default flip** — `AGENTWIRE_REPL_TUI` env flag, dispatcher, `_run_interactive`, `_run_sdk_session`, `_make_bottom_toolbar`, `_make_can_use_tool`, `_prompt_sync`, and the `prompt_toolkit` dep all deleted (#140, 2026-04-26)
- [x] **Phase 3A** — snapshot test infra (#135, 2026-04-25; opt-in via `pytest -m snapshots` because the framework leaves asyncio state polluted across the rest of the suite)
- [x] **Phase 3B** — `@`-mention autocomplete (#136, 2026-04-25; live preview in action pane + Tab to complete to top match)
- [x] **Phase 3C** — slash command palette (#137, 2026-04-25; Ctrl+P opens fuzzy picker)
- [x] **Phase 3D** — cost sparkline + transcript scrubber (#138, 2026-04-25)
- [x] **Phase 3E** — walkthrough doc (#139, 2026-04-25; `docs/repl-tui.md`. GIFs deferred until daily-driving captures good demo footage)
