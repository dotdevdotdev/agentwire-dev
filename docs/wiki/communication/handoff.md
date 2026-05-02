# Shareable Conversation Handoffs

Distill a live agent conversation into a portable bundle a teammate can pick up async.

> **Why:** GitHub issue [#157](https://github.com/dotdevdotdev/agentwire-dev/issues/157) — the Slack bot's "shared session" model forces everyone into one live thread. Handoffs let work continue across teammates without that.

## What you get

Two artifacts in one bundle dir:

| File | Audience | Purpose |
|------|----------|---------|
| `ai-handoff.md` | Another LLM (Opus 4.7) | XML-tagged markdown, project-folder-independent. Paste as a single message to roughly continue the conversation. |
| `show-the-story.html` | Human | Single-file presentation: tabs (Overview, Goal, Journey, Decisions, Artifacts, Open Threads, Cast & Context, Instructions), scroll-snap slides, LLM-picked theme. Opens offline. |

The HTML also embeds the source `ai-handoff.md` in a `<template>` block, so dropping the HTML into an LLM works too.

Bundles land in `~/.agentwire/artifacts/handoff-<timestamp>-<slug>/`.

## Architecture

```
in-conversation agent
        │
        │ /handoff slash command  OR  mcp__agentwire__handoff_init
        ▼
agent fills ai-handoff.md (it has full context — no fresh LLM call needed)
agent picks a vibe-matched <theme> JSON block
        │
        ▼
agent calls: mcp__agentwire__handoff_render  OR  agentwire handoff render
        │
        ▼
CLI parses ai-handoff.md → renders show-the-story.html via Jinja2
```

Key idea: the agent in the running conversation is the only thing with full context, so it does the distillation in-context. The CLI/MCP layer is purely deterministic rendering — no second LLM call, no token cost beyond what the conversation already paid.

## Usage

### From inside a Claude Code session

```
/handoff
```

The slash command (`.claude/commands/handoff.md`) walks the agent through:
1. `mcp__agentwire__handoff_init` — creates the bundle dir and pre-fills `ai-handoff.md` with metadata, the full CLAUDE.md / rules / memory chain, and current git state
2. Agent edits the template to fill goal, decisions, dead ends, journey beats, theme, etc.
3. `mcp__agentwire__handoff_render` — produces `show-the-story.html`

### Manual CLI

```bash
# Initialize bundle (run inside the project of interest)
agentwire handoff init --title "fixing-auth-flow"

# Edit the printed ai-handoff.md path. Replace every {{ ... }} placeholder.

# Render the human presentation
agentwire handoff render <bundle-dir>

# List past bundles
agentwire handoff list
```

### From an agent via MCP

```
mcp__agentwire__handoff_init(title="short-slug")        → bundle_dir, ai_handoff_path
# (agent edits ai_handoff_path with Write tool)
mcp__agentwire__handoff_render(bundle_dir=..., story=true)
mcp__agentwire__handoff_list()
```

## Bundle structure

`ai-handoff.md` is XML-tagged markdown:

```
<session_bundle version="1">
  <title>...</title>
  <metadata>cwd, repo, branch, commit, model, started_at, mcp_servers</metadata>
  <environment>panes, channels, anything cwd alone can't reveal</environment>
  <instructions>
    <file path="~/.claude/CLAUDE.md" kind="claude_md">...</file>
    <file path="~/.claude/rules/*.md" kind="rule">...</file>
    <file path="./CLAUDE.md" kind="project_claude_md">...</file>
    <file path=".../memory/MEMORY.md" kind="memory">...</file>
  </instructions>
  <project_state>git status, log, diff, key files</project_state>
  <conversation_summary>
    <goal>...</goal>
    <tldr>...</tldr>
    <decisions>...</decisions>
    <dead_ends>...</dead_ends>
    <open_threads>...</open_threads>
    <stats>...</stats>
  </conversation_summary>
  <journey>
    <beat title="...">narrative beat with quote + what_happened</beat>
  </journey>
  <recent_turns>filtered last 10–20 turns</recent_turns>
  <handoff>one_sentence + resume_at + caveats</handoff>
  <theme>{ "name": "...", "mood": "...", "palette": {...}, ... }</theme>
</session_bundle>
```

The `<instructions>` block is what makes the bundle portable across machines — the receiver doesn't need to be in the original folder; the agent's behavioral context comes inlined.

## Themes

The agent picks a theme JSON block based on the session's emotional tone. The renderer translates the palette into CSS variables (`--bg`, `--fg`, `--accent`, etc.) and applies them to the static template. Same template, different vibes per bundle.

```json
{
  "name": "ship-it-evening",
  "mood": "focused build session, quiet satisfaction",
  "palette": {
    "bg": "#0c1014",
    "surface": "#141a21",
    "fg": "#d8e1ea",
    "muted": "#5b6b7c",
    "accent": "#7dd3fc",
    "accent_2": "#fb923c",
    "border": "#1f2933"
  },
  "fonts": {
    "heading": "ui-monospace, 'JetBrains Mono', monospace",
    "body": "ui-sans-serif, system-ui, sans-serif"
  },
  "motion": "subtle"
}
```

`motion`: `subtle` (default, 180ms transitions), `playful` (320ms), or `none` (instant).

## Implementation reference

- Module: `agentwire/handoff/`
  - `schema.py` — dataclasses (Bundle, Theme, Decision, JourneyBeat, Instruction, …)
  - `git_state.py` — `git status` / `diff` / `log` / `branch` / `commit` capture
  - `instructions.py` — enumerates `~/.claude/CLAUDE.md`, `~/.claude/rules/*.md`, project chain (walks up to `~`), `~/.claude/projects/<encoded>/memory/MEMORY.md` + linked files
  - `parser.py` — line-anchored regex extraction (so diff-quoted tags like `+<theme>` don't confuse extraction)
  - `renderer.py` — Jinja2 → single-file HTML
- Templates: `agentwire/templates/handoff/{show-the-story.html.j2, theme.css.j2}`
- CLI: `agentwire handoff {init,render,list}` in `agentwire/__main__.py`
- MCP: `handoff_init`, `handoff_render`, `handoff_list` in `agentwire/mcp_server.py`
- Slash command: `.claude/commands/handoff.md`

## Out of scope (today)

- Live regeneration / "living HTML presentation" session type — would watch the bundle dir and re-render as the source session adds turns. Future v2.
- Bundle delivery via Slack / Discord / email — the channels module would extend cleanly to accept a bundle dir; not done yet.
- Fork-from-bundle — receiver's edits stay separate, optional merge-back.
- Multi-session bundles (orchestrator + workers).
