> Living document. Update this, don't create new versions.

# Mission: Agentwire REPL — SDK-Based Interactive Harness

A clean-room interactive harness built on `claude-agent-sdk`, living inside agentwire as a session type. Complementary to Claude Code, not a replacement. The differentiator is **agentwire-native integration** — MCP baked in, workflow bridging, voice, damage control, role layering — things Anthropic has no reason to build into Claude Code because they're long-tail specialized cases.

**Phase of:** own mission (peer to `pi-harness-overview.md`)
**Status:** **Phases 1-5 shipped (2026-04-25).** Phase 6+ is trigger-driven. See "Shipping log" below.
**Depends on:** Phase 6 Anthropic SDK workflow runner — complete
**Blocks:** nothing

## Shipping log

| Phase | What landed | PRs |
|---|---|---|
| 1 | MVP loop, print mode, sdk-* session types, banner, model + adaptive thinking + effort defaults | merged earlier |
| 2 PR 1 | `/help`, `/exit`, `/clear`, `/cost`, `/tools`, `/model` | #111 |
| 2 PR 2 | Transcript persistence + `/save` + `/resume` | #112 |
| 2 PR 3 | Multi-line input + `@`-mention expansion (with TTY fallback) | #113 |
| 2 PR 4 | Cost-line bottom toolbar + `/effort` + `/thinking` + sdk-prompted inline y/n/a + classify | #114 |
| 3 PR 1 | Agentwire MCP server auto-attached (~87 tools first-class) | #115 |
| 3 PR 2 | Python damage control via SDK PreToolUse hook (mirror of shell hook patterns) | #116 |
| 3 PR 3 | `.agentwire.yml` roles + voice + `--role` + `/say` | #117 |
| 3 PR 4 | Portal sidebar SDK type-tag (teal) + artifact hint | #118 |
| 4 | `human_gate` workflow runner + `/run-workflow` slash command + `seed_message` | #119 |
| 5 | Scheduler accepts sdk-* task type; overnight resume injection guarded to claude-* (also fixed a latent rfind bug) | (this PR) |

## Why build this

- Claude Code is Anthropic's flagship, closed-source, optimized for the general case.
- Agentwire has ~87 MCP tools, damage control, voice, scheduler, workflow engine, multi-session coordination. That's integration surface Anthropic has no reason to target.
- Pi covers the Z.AI + cheap-model + standalone-binary space. It shouldn't go deep on agentwire internals.
- A Python REPL on `claude-agent-sdk`, living inside agentwire, can do things neither Claude Code nor pi can — on the same Anthropic subscription.
- Positioning: **complementary to Claude Code, specialized for agentwire-network workloads.**

## Architecture at a glance

| Dimension | Decision |
|---|---|
| Process model | Python REPL running in a tmux pane (same lifecycle as `pi-zai`; different from removed portal-hosted `sdk-*`) |
| SDK | `claude-agent-sdk` — already in use by the workflow `anthropic` runner |
| Auth | Subscription (`~/.claude/.credentials.json`). No API keys. |
| Providers | Anthropic only, architecturally. Not a v2 question. |
| Tool surface | Claude Code CamelCase set: `Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`, `WebFetch`, `WebSearch` |
| Default model | `claude-opus-4-7` + `thinking: {type: adaptive}` + `effort: xhigh` |
| MCP | Agentwire MCP server auto-connected; ~87 tools available first-class |
| Damage control | Python-level pre-tool-call check (shell hooks won't fire for SDK tool calls) |
| System prompt | Layered: base + role(s) + CLAUDE.md + AGENTS.md + `.agentwire.yml` project config |

## Session type variants (parallel to `claude-*`)

| Type | Permission mode | Tools |
|---|---|---|
| `sdk-bypass` | bypass (run tools without asking) | full |
| `sdk-prompted` | ask before each tool call | full |
| `sdk-restricted` | plan / read-only | `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch` |

## Clean-room departures from pi (what we intentionally don't copy)

- **Tool surface**: pi's minimal 4-tool set was a compromise for small-model context. Opus handles Claude Code's full set fine.
- **Process model**: pi is subprocess-per-invocation. Ours is long-running Python REPL → tighter agentwire integration, shared state, direct MCP calls.
- **Provider abstraction**: pi supports multiple providers. We're Anthropic-only by design.
- **Binary distribution**: pi is a standalone npm-global. Ours is a Python module inside agentwire — versioned together, no separate upgrade path.
- **RPC mode**: deferred indefinitely (pi has `--mode rpc`; we may never need it given MCP covers programmatic control).

## Phases

### Phase 1 — MVP interactive loop (target: 2-3 weeks)

Prove the core loop works end-to-end. Everything minimal.

- Python entry point callable from `agentwire new -s <name> --type sdk-bypass`
- `cmd_new` + `build_agent_command` dispatch on `sdk-*` prefix
- `_build_tmux_env_flags` pattern used for any required env at session start
- REPL shell (TUI library TBD — see open questions)
- `claude-agent-sdk` client wired with subscription auth
- Streaming event handling reused from `workflows/runners/anthropic.py` and `anthropic_events.py`
- Terminal rendering of text + tool_use + tool_result + thinking events (markdown + syntax highlight)
- Full CamelCase tool set via SDK `allowed_tools`
- `sdk-bypass` → `permission_mode: bypassPermissions`; `sdk-prompted` → `default`; `sdk-restricted` → `plan`
- Print mode (`-p "prompt"`) for one-shot invocations
- CLAUDE.md + AGENTS.md auto-discovery from cwd, injected via system prompt
- Default `claude-opus-4-7` + adaptive thinking + effort `xhigh`
- Ctrl+C cancels current turn; Ctrl+D exits

**Success criteria:** user can `agentwire new -s test --type sdk-bypass`, see the pane in tmux, chat with Opus 4.7, watch Read/Bash/Edit tool calls stream, and exit cleanly. Print mode matches Claude Code `-p` shape.

### Phase 2 — Interactive polish (target: 2-3 weeks)

Close the usability gap with Claude Code for users who'd otherwise reach for it.

- Slash commands: `/help`, `/clear`, `/model`, `/cost`, `/tools`, `/save`, `/resume`, `/exit`, `/thinking`, `/effort`
- Multi-line input (shift+enter or heredoc)
- Transcript persistence to `~/.agentwire/sessions/<name>/transcript.jsonl` (schema mirrors `workflows/storage.py` events)
- `/resume` loads prior transcript
- `@`-mention for file inclusion (`@path/to/file.py` expands inline)
- Cost + token running total on a status line
- Permission UX for `sdk-prompted`: inline allow / deny / always-allow
- Error classification reused from `runners/anthropic.py._classify()`

**Success criteria:** daily-driver-grade terminal UX. Power users don't miss Claude Code for this surface.

### Phase 3 — Agentwire-native integration (target: 3-4 weeks) — **the differentiator**

Where this harness earns its keep.

- **MCP client baked in**: REPL auto-connects to agentwire MCP server at startup, all ~87 tools exposed natively (no configuration, no per-session wiring)
- **Damage control**: Python pre-tool-call check reads `~/.agentwire/damage-control/` rules + `.agentwire.yml` `safety.allowed_paths`; blocks matches with the same UX as shell hooks
- **Voice**: session can TTS responses via `agentwire say`; optional STT input via `agentwire listen`
- **Session notifications**: idle-handler pattern applied; portal + queue-processor see the session like any other
- **Role layering**: `parse_role_file()` + `merge_roles()` reused as-is; system prompt composition matches `build_agent_command` layering
- **`.agentwire.yml` awareness**: project config, `allowed_workers`, `voice:`, role selection all honored
- **Portal visibility**: session appears in sidebar with `sdk-*` badge; runner-style color coding (distinct from anthropic workflow orange)
- **Artifact hooks**: REPL can write HTML artifacts via existing `~/.agentwire/artifacts/` + artifact window pattern

**Success criteria:** the REPL can do something *no other harness can* in a single session — e.g. `pane_spawn` a worker, then monitor its output, then `say` a summary, then trigger a scheduled workflow — all as first-class tool calls.

### Phase 4 — Workflow integration (target: 2-3 weeks) — **mid-workflow human gates**

The "REPL as building block" the user called out.

- New node type: `type: human_gate` (name TBD) in workflow YAML
- On execution: workflow spawns an `sdk-bypass` session pre-loaded with:
  - All upstream node outputs as context
  - Node prompt as opening user message
  - Role / tools from node config
- Workflow pauses until the session signals completion (mechanism TBD — see open questions)
- Session output becomes the node's `NodeResult.output` and feeds downstream nodes
- Use cases: review-before-destructive-action, manual research step, approval gate, interactive triage
- Reverse direction: inside a REPL session, `/run-workflow <name>` spawns a workflow with current session's context as inputs

**Success criteria:** at least one production workflow uses a `human_gate` node in real daily use.

### Phase 5 — Scheduler integration (target: ~1 week)

- Scheduled `task:` entries accept `type: sdk-bypass` (works identically to existing `claude-bypass`)
- Overnight queue supports `sdk-*` types
- Scheduler doesn't need to understand REPL specifics — just another session type

**Success criteria:** at least one scheduled task uses `sdk-bypass`; one overnight-queue entry uses it.

### Phase 6+ — As-needed (no ETA; trigger-driven)

- JSON event mode (`--mode json`) — only if a programmatic consumer asks
- Custom slash commands via `.agentwire/slash/` directory
- Plugin system for per-project tool additions
- Session hierarchy (parent/child) matching old SDK implementation — only if `pane_spawn` proves insufficient
- Multi-agent coordination primitives beyond what MCP gives us today

## Customizability dimensions (the flexible surface we prototype through)

Not all answered in Phase 1. These are *where* differentiation emerges as real usage shapes it:

1. **System prompt composition**: base + role(s) + CLAUDE.md + AGENTS.md + `.agentwire.yml` + per-session overrides. Order and precedence TBD by experience.
2. **Tool set per variant**: beyond the 3 fixed variants, should projects be able to define their own via `.agentwire.yml`?
3. **Custom slash commands**: project-level (`.agentwire/slash/`) and user-level (`~/.agentwire/slash/`)?
4. **Hook points**: pre-turn, post-turn, pre-tool, post-tool, pre-exit — for voice/notification/audit/logging.
5. **MCP tool filtering**: allow projects to restrict which MCP tools are visible to the REPL?
6. **Workflow bridge direction**: REPL spawning workflows vs. workflows spawning REPLs vs. both — which earns use?

These stay open deliberately. Prototype first, decide from real usage.

## Open questions (to be answered during implementation)

- **TUI library**: `prompt_toolkit` (mature) vs. `textual` (richer, heavier) vs. `rich`-only with custom input loop. Decide at Phase 1 kickoff.
- **Workflow pause/resume mechanics**: marker file vs. named pipe vs. `/resume-workflow` slash command vs. dedicated MCP tool invoked from inside the REPL. Decide at Phase 4.
- **Session hierarchy**: the old removed `SdkSession` had parent/child tracking with auto-kill. MCP's `pane_spawn` covers most of what that enabled. Revisit only if gaps appear.
- **Damage control reuse**: ideally extract shared rules from `~/.agentwire/hooks/damage-control/` so shell hooks and Python check share one source of truth. Shape TBD.
- **Transcript format**: mirror `workflows/storage.py` events JSONL exactly, or define a REPL-specific schema? Mirroring buys us the portal history window for free.
- **Cost accounting**: aggregate with workflow cost totals, per-session only, or both surfaces?
- **Subscription rate-limit handling**: queue, backoff, or error-and-tell-user? Not a blocker for MVP but will bite in Phase 3+.

## Non-goals (permanent)

- Other providers (Z.AI, OpenAI, local models) — architectural, not deferred. Use `pi-zai` for Z.AI.
- Replacing Claude Code — it remains flagship for general Anthropic-subscription work.
- Standalone binary / distribution outside agentwire.
- GUI frontend — portal surfaces it like any other session; no separate UI.
- Collaborative / multi-user editing.
- Mobile.

## Revival note (important context for future readers)

The session type names `sdk-bypass`, `sdk-prompted`, `sdk-restricted` existed before 2026-04-12 and were removed along with `agentwire/agents/sdk.py` (607 LOC) when we believed subscription+SDK was disallowed. Anthropic publicly confirmed subscription use of the Agent SDK is allowed. Names are being revived for naming continuity with `claude-*` counterparts, but the **architecture is different**:

| Aspect | Old `sdk-*` (removed 2026-04-12) | New `sdk-*` (this mission) |
|---|---|---|
| Process | Portal-hosted Python async client | Tmux-pane Python REPL |
| Visibility | Portal sessions list only | Tmux + portal + session tree |
| Hierarchy | Parent/child `SdkSession` objects | Stateless; use `pane_spawn` if needed |
| Integration | Limited; MCP via portal proxy | Native MCP, damage control, voice, scheduler |

Old code in git history is **reference only**, not a blueprint.

## Dependencies

- `claude-agent-sdk>=0.1.0` (already in `pyproject.toml`)
- TUI library (TBD; `prompt_toolkit` most likely)
- Existing agentwire: sessions model, MCP server, damage control framework, roles, scheduler, workflow engine, portal

## Code references (study / possibly reuse — not copy blindly)

- `agentwire/workflows/runners/anthropic.py` — SDK init, streaming pattern, options building
- `agentwire/sdk/events.py` — SDK event → JSONL translation (reusable; relocated from `workflows/runners/anthropic_events.py` in v1.24.0)
- `agentwire/sdk/capabilities.py` — model / effort / thinking validation (relocated from `workflows/runners/anthropic_capabilities.py` in v1.24.0)
- `agentwire/workflows/storage.py` — transcript JSONL + metadata pattern
- `agentwire/workflows/cli.py` `_make_verbose_printer()` — event-to-terminal printer prototype
- `agentwire/roles/__init__.py` — role parsing + merging (universal, works as-is)
- `agentwire/cli_safety.py` — damage control rules + pattern matching
- `agentwire/project_config.py` — `SessionType` enum, `SafetyConfig`
- `agentwire/__main__.py:160-270` — `build_agent_command` dispatch (add `sdk-*` branch)
- `agentwire/__main__.py:87-114` — `_build_tmux_env_flags` canonical env-injection
- `agentwire/search.py` — Brave Search helper (expose as tool or reference for Bash+curl patterns)
- Git history `agentwire/agents/sdk.py` pre-2026-04-12 — **reference only**, architecture is different this time

## Success criteria (aggregated)

- **Phase 1**: Interactive Opus 4.7 session via `agentwire new --type sdk-bypass` works end-to-end; print mode matches Claude Code `-p`
- **Phase 2**: Daily-driver UX — no feature regressions vs. Claude Code for this surface
- **Phase 3**: Demonstrable task that's easier or only-possible in our REPL (e.g., MCP `pane_spawn` + `say` + `scheduler_run` all in one session)
- **Phase 4**: One production workflow uses `human_gate` daily
- **Phase 5**: One scheduled task runs on `sdk-bypass`

## Pitfalls to watch for

- **Over-specifying while prototyping**. If a decision reads load-bearing in this doc but hasn't been validated, move it to open questions — don't cement it.
- **Phase bloat**. Every phase past 1 must have its own PR, its own trigger. This doc must not imply one-big-commit.
- **Repeating the claudeGLM mistake**. That wrapper went to `main` without a real end-to-end check; we paid for it in silent breakage. Phase 1's success criterion is explicit: can it chat + use tools end-to-end? Nothing ships until that's green.
- **Confusing revival with restoration**. Old `sdk-*` is gone for good; we're reusing the names, not the code.
- **Damage control gap**. SDK tool calls bypass shell hooks. If Phase 3 ships without Python-level damage control, we've regressed safety. Treat this as blocking, not optional.

## Revisit checklist

Every ~30 days of production REPL usage, re-read this doc and:

- Which phases have shipped? Update status.
- Which open questions are now answered? Record the decision inline.
- Which customizability dimensions turned out to matter? Promote them from "open" to scoped features.
- Which Phase-6+ items now have triggers? Move to scoped work.
