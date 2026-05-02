# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Shareable conversation handoffs** — `agentwire handoff` produces two artifacts from one conversation: `ai-handoff.md` (XML-tagged markdown a teammate can paste into another LLM) and `show-the-story.html` (single-file presentation with tabs and scroll-slides) — issue [#157](https://github.com/dotdevdotdev/agentwire-dev/issues/157)
  - In-conversation agent does the distillation (free — uses existing context, no fresh LLM call); CLI/MCP just renders deterministically via Jinja2
  - `/handoff` slash command walks the agent through filling the template, picking a vibe-matched theme (palette, fonts, motion), and rendering
  - Bundle is portable across machines: full CLAUDE.md / rules / memory chain inlined into `<instructions>` so the receiver doesn't need the original cwd
  - Subcommands: `agentwire handoff init [--title]`, `agentwire handoff render <bundle-dir> [--story]`, `agentwire handoff list`
  - MCP wrappers: `mcp__agentwire__handoff_init`, `mcp__agentwire__handoff_render`, `mcp__agentwire__handoff_list`
  - Outputs land in `~/.agentwire/artifacts/handoff-<timestamp>-<slug>/`
  - 42 new unit tests cover parser (valid/malformed/diff-pollution regressions), renderer (theme variations, tabs, self-containedness), git-state capture, and CLAUDE.md chain enumeration
- **Anthropic workflow runner** — `runner: anthropic` alongside the existing pi runner (Phase 6, PRs 1–6)
  - Pluggable runner registry (`agentwire/workflows/runners/`) with a shared `NodeRunner` Protocol; pi kept byte-for-byte identical behind a thin shim
  - `AnthropicRunner` uses `claude-agent-sdk>=0.1.43` with subscription auth — no `ANTHROPIC_API_KEY` required, inherits `~/.claude/.credentials.json` (subscription-covered, no per-run billing)
  - Tool execution is owned by Claude Code itself: `setting_sources=["user"]` loads `~/.claude/settings.json` so AgentWire's damage-control PreToolUse hooks fire on every Bash call (verified end-to-end by a live integration test that chmod 777's a sentinel file — hook blocks, file mode unchanged)
  - Per-runner tool namespaces: pi stays lowercase (`read`, `bash`, `edit`, …), anthropic uses CamelCase (`Read`, `Bash`, `Edit`, …); parse-time validation rejects cross-namespace mixups
  - Anthropic-only fields validated at parse time: `model` (required), `effort` (low|medium|high|max|xhigh — with Opus/Opus-4.7 gating), `thinking_config` (adaptive|enabled|disabled), `task_budget_tokens` (Opus 4.7 beta, min 20000), `max_thinking_tokens`, `max_budget_usd`
  - Error classification: anthropic runner tags `NodeResult.error` with `transient:` / `permanent:` / `invalid:` / `error:` prefixes so rate-limited runs are distinguishable from genuine bugs
- **Live event streaming under `--verbose`** — `agentwire workflow run … -v` now streams per-event output for anthropic nodes: `[node] → tool_use Read …`, `← tool_result (ok/err)`, `▓ text`, `✓ turn 42+18 tok`, `■ agent_end 3.1s`
- **Runner recorded in metadata** — `metadata.json` bumped to `schema_version: 2` with run-level and per-node `runner` fields; `workflow show` renders a `Runner:` line + per-node tag + `Totals:` aggregation; `workflow history` gains a `runner` column
- **`--runner {pi,anthropic}` CLI override** — `agentwire workflow run` accepts `--runner` to flip every node's runner for one invocation; runner/field mismatches surface as normal validation errors
- **Canary live** — `daily-book-report` (daily 13:30) flipped to anthropic on 2026-04-17 (Sonnet 4.6 for fetch, Opus 4.7 for compose_and_send). Findings tracked in `docs/missions/anthropic-sdk-runner.md`
- **Damage-control honors session bypass modes** — `permission_mode: "bypassPermissions"` and `"auto"` now skip `ask:true` escalations for write-tier commands; hard blocks still fire. Fixes `--dangerously-skip-permissions` and autonomous-mode sessions being prompted per Bash call

### Documentation

- New "Runners" section in `docs/workflows.md` covering per-runner fields, `--runner` usage, and live-event output
- `agentwire-workflows` skill updated — no longer claims pi-only; links to full Runners reference

## [1.23.0] - 2026-04-16

### Added

- **Workflow-backed scheduler tasks** — scheduler can now dispatch a pi workflow DAG in-process instead of shelling out to `agentwire ensure` (Phase 3 of the pi workflow roadmap)
  - New `workflow:` + `inputs:` fields on scheduler tasks in `~/.agentwire/scheduler.yaml` (mutually exclusive with `task:`)
  - `dispatch_task()` routes automatically: ensure path unchanged, new `_dispatch_workflow_task` calls `run_workflow()` in-process — no tmux, no Claude Code subprocess
  - Status mapping: workflow `success→complete`, `partial→incomplete`, `failure→failed`
  - Scheduler `{{ task }}`, `{{ project }}`, `{{ session }}`, `{{ workflow }}` variables expand in string `inputs:` values
  - `agentwire scheduler run <name> --dry-run` prints the workflow plan without touching state
  - `task_completed` events now carry `workflow`, `run_id`, and per-node `nodes[]` when the task is workflow-backed
  - Morning report (`agentwire scheduler report --artifact`) renders per-node status badges + run-id breadcrumb for workflow rows
  - `agentwire scheduler history --json` includes the `workflow` name per task
  - Full reference: `docs/workflows.md` → "Scheduler integration"; compare/contrast: `docs/scheduled-workloads.md`
- **Kokoro TTS engine** — CPU-only ultra-lightweight backend (`kokoro`)
  - Kokoro 82M ONNX model via `kokoro-onnx`, auto-downloads ~170 MB from HuggingFace on first use
  - No GPU required — pure ONNX CPU inference, near real-time on Apple Silicon / modern Intel CPU
  - 30+ preset voices across 8 languages (English, Spanish, French, Hindi, Italian, Japanese, Portuguese, Chinese); `af_heart` is the default and highest quality voice
  - Streaming support via `create_stream()`
  - Runs in dedicated `.venv-kokoro` with CPU-only PyTorch (~250 MB vs 2 GB+ CUDA builds)

### Fixed

- `cmd_scheduler_report` was calling `read_events(limit=500)` with the wrong kwarg name; the `except` silently caught the `TypeError` so morning reports quietly returned 0 events. Now calls `read_events(tail=500)`.

## [1.9.0] - 2026-03-13

### Added

- **Zonos TTS engine** — Zyphra Zonos v0.1 Transformer and Hybrid backends (`zonos-transformer`, `zonos-hybrid`)
  - Zero-shot voice cloning from 10–30s reference audio
  - Fine-grained emotion control: 7 independent sliders (happiness, sadness, disgust, fear, surprise, anger, other); neutral auto-fills remainder automatically
  - 5 language support: English, Japanese, Chinese, French, German
  - <4 GB VRAM; runs in dedicated `.venv-zonos`
- **Full emotion API on `TTSRequest`** — `emotion_happiness`, `emotion_sadness`, `emotion_disgust`, `emotion_fear`, `emotion_surprise`, `emotion_anger`, `emotion_other` (all `float = 0.0`)
- **Speaking characteristics** — `speaking_rate` and `pitch_std` on `TTSRequest` (Zonos)
- **`zonos` venv family** — wired through `_get_venv_for_backend` and `BACKEND_FAMILIES` with hot-swap support

### Changed

- `tts.backend` config now accepts `zonos-transformer` and `zonos-hybrid`

## [1.3.0] - 2026-02-10

### Added

- Drag-to-tile window management for side-by-side session workflows
- Auto-chunk long TTS messages into separate audio segments for sequential playback
- Redesigned onboarding flow that asks 3 questions then spawns Claude for setup

### Fixed

- Chunk pasted terminal input to prevent PTY buffer flooding and session freezes
- Poll summary file directly instead of relying on two-idle completion signal
- Move TTS chunker to utils to avoid torch import in MCP server
- Namespace task summary files by session to prevent cross-session collisions
- Don't clear task context on ensure timeout (race condition)
- Stale lock detection in `--wait-lock` + add `--skip-if-locked`
- Use STT server when configured instead of always falling back to WhisperKit

## [1.2.0] - 2026-02-03

### Added

- Persistent STT server (`agentwire stt start`) to eliminate cold start delays - transcriptions now complete in ~0.3-0.5s instead of 3-5s
- STT server uses faster-whisper with openai-whisper fallback, supports model selection via `--model` flag
- `listen.py` now tries STT server first, falls back to whisperkit-cli if unavailable

### Changed

- Default STT port changed from 8100 to 8101 to avoid conflict with TTS server

### Fixed

- Email body text contrast improved (`#d0d0d0` → `#e8e8e8`) for better readability on dark backgrounds

## [1.1.0] - 2026-02-01

### Added

- MCP server for external agent integration with tools for sessions, machines, and transcription
- Scheduled workloads with `ensure` and `exit_on_complete` options and lock management CLI
- Email notifications via Resend with branded templates and banner header
- TTS improvements: queued audio playback, orphaned task handling, model-specific roles
- Standalone voice role for non-orchestration use; support `voice: random` in project config
- Progressive loading for faster UI feedback and real-time session updates
- UI enhancements: combo button in projects list, delete project action, reusable ListCard component

### Changed

- Roles refactored for consistency; leader role made agent-agnostic; delegation roles authoritative
- Roles and docs updated to favor MCP tools over direct CLI commands
- Updated assets and splash screens with layered foreground images

### Fixed

- Numerous CLI and portal fixes: correct `--type` handling for remote sessions; proper session removal broadcasts; correct sessions data access in monitor
- Health/exit behavior for scheduled tasks; TTS error messages surfaced; directory auto-creation on custom paths
- Email template layout fixes (full-width banner, proper aspect ratios) and HTML detection to prevent escaping
- Worktree guidance and examples corrected; GLM-only delegation role enforced; pane spawn examples require explicit `pane_type`

### Documentation

- Expanded docs: scheduled workloads spec, lock management commands, email notifications, MCP tools and roles
- Added brainstorm docs (context compression, transcripts, worker streaming, ambient context, audio cues)
- Updated project URLs and YouTube channel descriptions; clarified MCP tools worktree limitations

### Chore

- Asset cleanup and refresh (logos, splash images, transparent and black-bg variants)
- Git hygiene and ignore updates; example/demo script additions

## [1.0.0] - 2026-01-19

Initial public release of AgentWire.

### Added

- **Desktop Control Center** - WinBox-powered window management with draggable/resizable session windows
- **Session Windows** - Monitor mode (read-only output) or Terminal mode (full xterm.js) per session
- **Push-to-Talk Voice** - Hold to speak, release to send transcription from any device
- **TTS Playback** - Agent responses spoken back via browser audio with smart routing
- **Multi-Device Access** - Control sessions from phone, tablet, or laptop on your network
- **Git Worktrees** - Multiple agents work the same project in parallel on separate branches
- **Remote Machines** - Orchestrate Claude Code sessions on remote servers via SSH
- **Safety Hooks** - 300+ dangerous command patterns blocked (rm -rf, git push --force, secret exposure)
- **Session Roles** - Orchestrator sessions coordinate voice, workers execute focused tasks
- **Permission Hooks** - Claude Code integration for permission dialogs in the portal

### CLI Commands

- `agentwire init` - Interactive setup wizard
- `agentwire portal start/stop/status` - Portal management
- `agentwire tts start/stop/status` - TTS server management
- `agentwire stt start/stop/status` - STT server management
- `agentwire new/list/kill/send/output` - Session management
- `agentwire spawn/split/detach/jump` - Pane management
- `agentwire say` - TTS with smart audio routing
- `agentwire safety check/status/logs` - Security diagnostics
- `agentwire machine add/remove/list` - Remote machine management
- `agentwire tunnels up/down/status` - SSH tunnel management
- `agentwire history list/show/resume` - Session history
- `agentwire doctor` - Auto-diagnose and fix issues
- `agentwire generate-certs` - SSL certificate generation

### Security

- Damage control hooks protecting against 300+ dangerous command patterns
- Zero-access paths for credentials, SSH keys, and API tokens
- Read-only paths for system configs
- No-delete paths for session and mission files
- Audit logging for all security decisions

### Documentation

- Comprehensive README with platform-specific installation instructions
- Architecture documentation
- Troubleshooting guide
- TTS setup guide
- Remote machines guide
- Security documentation

[1.0.0]: https://github.com/dotdevdotdev/agentwire-dev/releases/tag/v1.0.0

[1.1.0]: https://github.com/dotdevdotdev/agentwire-dev/compare/v1.0.0...v1.1.0
[1.2.0]: https://github.com/dotdevdotdev/agentwire-dev/compare/v1.1.0...v1.2.0
[1.3.0]: https://github.com/dotdevdotdev/agentwire-dev/compare/v1.2.0...v1.3.0
[Unreleased]: https://github.com/dotdevdotdev/agentwire-dev/compare/v1.3.0...HEAD
