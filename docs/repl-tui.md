# Agentwire REPL — Textual TUI walkthrough

> The Textual TUI for `agentwire repl`. See `docs/missions/agentwire-repl-textual.md`
> for design and ship history.

## Quick start

```bash
agentwire repl
```

That's it. Print mode (`-p PROMPT`) keeps a stdout single-shot pipe — the
TUI is for interactive sessions only.

## Layout

```
┌─ Header ──────────────────────────────────────────────────┐
│  agentwire repl — bypass · opus-4-7                       │
├─ ChatLog (RichLog, fr=1) ─────────────────────────────────┤
│  - agent started · claude-opus-4-7 · session 0657172a     │
│  > previous user turn                                     │
│  - thinking: planning the file structure                  │
│    · deciding section ordering                            │
│  - Write fanfic.html                                      │
│    · result: file written (4.2 KB)                        │
│  assistant text streamed live, line by line               │
│  - Bash ls /tmp                                           │
│    · result: 12 files in /tmp                             │
│  - done · 7+9086 tok · $0.4499 · 106.7s                   │
├─ Input (dock=bottom) ─────────────────────────────────────┤
│  > tell me about prompt caching                           │
├─ StatusLine ──────────────────────────────────────────────┤
│  3 turns ▁▃█ · 350 tok · $0.0421 · effort=high · thinking=adaptive │
└─ Footer ──────────────────────────────────────────────────┘
```

Everything streams into one ChatLog as a flat hierarchy of bullets:

- `- top-level` lines mark major events: agent_started, thinking,
  tool calls, assistant text, done.
- `  · child` indents are details that hang off the bullet above:
  thinking continuations, tool results.

The previous CurrentAction subpane was removed in favour of this single
chat with bullet hierarchy — partial-message streaming and snapshot
turns share the same surface, and the indent gives visual structure
even when content streams in fast.

- **StatusLine** — running totals + sparkline of last-20-turn cost.
- **Footer** — Textual binding hints (Ctrl+P, Ctrl+D, Ctrl+C).

## Slash commands

Standard commands work identically to the line-mode REPL:

| Command | What it does |
|---|---|
| `/help` | List all commands |
| `/clear` | Reset conversation (fresh context) |
| `/cost` | Show session token + cost totals |
| `/tools` | List allowed tools |
| `/model` | Show current model + session id |
| `/save` | Show transcript path + resume hint |
| `/resume <name>` | Resume a saved session |
| `/effort <level>` | Set thinking effort (low/medium/high/xhigh/max) |
| `/thinking <mode>` | Set thinking display (adaptive/summarized/off) |
| `/say <text>` | Speak text via `agentwire say` |
| `/run-workflow <name>` | Run an agentwire workflow |
| `/exit` (alias `/quit`) | Exit |

Textual-only additions:

| Command | What it does |
|---|---|
| `/layout chat=N action=M` | Adjust pane proportional weights |
| `/layout` | Show current weights |
| `/theme <name>` | Switch Textual theme |
| `/theme` | Show current + list available |
| `/scrub` | Open transcript scrubber (read-only viewer) |

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Enter` | Submit current input |
| `Tab` | Complete `@`-mention to top match |
| `Ctrl+P` | Open command palette |
| `Ctrl+C` | Cancel current turn |
| `Ctrl+D` | Exit |
| `Esc` | Dismiss modal / close palette |
| `y` / `n` / `a` | Permission prompt (allow / deny / always-allow) |

## `@`-mention autocomplete

As you type `@`, **Tab** completes the prefix to the top match. (The
live candidate preview that lived in the now-removed CurrentAction
pane went away with it; Tab still does the work.)

```
> summarize @REA█
```

After `Tab`:

```
> summarize @README.md █
```

The mention is then expanded into the full file contents at submit time
(same as the line-mode REPL).

## Permission prompts (sdk-prompted mode)

In `--mode prompted`, every tool call pops a centered modal:

```
        ╭─ Allow Bash? ──────────────────────────────╮
        │  Bash ls -la                              │
        │  y = allow once · n = deny · a = always   │
        │  [Allow (y)]  [Deny (n)]  [Always (a)]    │
        ╰────────────────────────────────────────────╯
```

Pressing `a` adds the tool name to the per-session always-allow set so
subsequent calls of the same tool skip the prompt. Reset on `/clear`.

## Command palette (Ctrl+P)

Fuzzy picker over the slash command registry. Opens centered, filters
live as you type. Selecting a command writes it into the input field
with a trailing space so you can add args before submitting.

```
        ╭─ /co_ ─────────────────────────────────╮
        │  /cost  —  Show session token + cost   │
        │  /clear —  Reset conversation          │
        ╰────────────────────────────────────────╯
```

Leading `/` on the query is normalized — both `/co` and `co` match
`/cost`.

## Cost sparkline

The StatusLine embeds a unicode-block sparkline of per-turn cost over
the last 20 turns. As the session grows you see at a glance which
turns were expensive:

```
3 turns ▁▃█ · 350 tok · $0.0421 · effort=high · thinking=adaptive
```

`▁` is the cheapest turn; `█` is the most expensive in the visible
window. Bars rescale relative to the current peak.

## Transcript scrubber

`/scrub` opens a read-only viewer listing every user turn this
session with a 100-char preview. Useful when scrollback gets long.
Esc / `q` to close.

## Theming

The default `agentwire` theme matches the dotdev/agentwire brand:

| Token | Color | Role |
|---|---|---|
| `primary` | `#00ff88` neon green | chat border, input border, modal labels |
| `secondary` / `accent` | `#00d4ff` neon cyan | action-pane border, status text, modal borders |
| `background` | `#000000` flat black | screen + chat |
| `surface` | `#0a0a0a` near-black | status line, modal interior, action pane |
| `foreground` | `#e2e8f0` near-white | main text |
| `success` | `#00ff88` | matches primary |
| `warning` | `#fbbf24` amber | in-progress markers |
| `error` | `#dc2626` red | destructive |

### Per-user overrides

Drop a `repl.theme` block into `~/.agentwire/config.yaml`:

```yaml
repl:
  theme:
    primary: "#ff00aa"        # override neon-green primary
    background: "#0d0d2a"     # tweak the flat-black background
    header-foreground: "#ffffff"  # variable-level override
```

Any palette key (`primary`, `secondary`, `accent`, `foreground`,
`background`, `surface`, `panel`, `success`, `warning`, `error`) and
any Textual variable (e.g. `header-foreground`, `footer-key-foreground`,
`border-blurred`) can be overridden independently. Missing keys keep
the brand defaults.

### Switching at runtime

Other Textual built-ins remain available:

```
> /theme
[theme: agentwire]
[available: agentwire, catppuccin-latte, catppuccin-mocha, dracula,
flexoki, gruvbox, monokai, nord, solarized-light, textual-dark,
textual-light, tokyo-night]

> /theme dracula
[theme set: dracula]
```

## Layout customization

The CurrentAction subpane was removed in the bullet-format redesign, so
there's only one pane to size now and it fills the body. `/layout`
remains as a stub command that just announces this — kept for habit
compatibility, not for tuning.

## Persistence

Identical to the line-mode REPL — `~/.agentwire/sessions/repl/<name>/`
contains:

- `metadata.json` — session config + running totals (updated on close)
- `transcript.jsonl` — one JSON object per line, event stream

Both files are byte-identical to what the line-mode path produces, so
`/resume` works across both implementations.

## Fan-out view (multi-generation A/B)

Run the same prompt across N independent SDK clients side by side:

```bash
# Fan-out 3 ways with the same model
agentwire repl --view fanout --cols 3

# Compare models side by side
agentwire repl --view fanout --cols 3 \
  --col-model 0=claude-opus-4-7 \
  --col-model 1=claude-sonnet-4-6 \
  --col-model 2=claude-haiku-4-5

# Compare effort levels
agentwire repl --view fanout --cols 2 \
  --col-effort 0=max --col-effort 1=high

# Compare roles (e.g. skeptic vs optimist)
agentwire repl --view fanout --cols 2 \
  --col-role 0=skeptic --col-role 1=optimist
```

```
┌─ col 1 · claude-opus-4-7 ─┬─ col 2 · claude-opus-4-7 ─┬─ col 3 · claude-opus-4-7 ─┐
│ [chat history]            │ [chat history]            │ [chat history]            │
│                           │                           │                           │
├─ current action ──────────┼─ current action ──────────┼─ current action ──────────┤
│ [thinking…]               │ [thinking…]               │ [thinking…]               │
├─ status ──────────────────┼─ status ──────────────────┼─ status ──────────────────┤
│ col 1 · 1 turn · 47 tok   │ col 2 · 1 turn · 51 tok   │ col 3 · 1 turn · 49 tok   │
├───────────────────────────┴───────────────────────────┴───────────────────────────┤
│ > master input — fans out to all columns                                          │
└───────────────────────────────────────────────────────────────────────────────────┘
```

Type into the master input — the prompt fans out to every column in
parallel. Each column streams its own response independently with its
own running totals (turns, tokens, cost). `Ctrl+C` cancels every
in-flight column at once.

Each column also has its own input field — type into one and the
prompt only goes to that column. Useful for redirecting a single
branch ("col 2 — try again with X") without affecting the others.

Use case: when you want multiple Opus 4.7 attempts on the same prompt to
pick the best one, or to compare model outputs (when per-column overrides
ship). `--cols 2-6` is supported; 3 is a good default for most terminal
widths.

## What's next

Trigger-driven: snapshot tests for new visual changes (Phase 3A laid the
infra; run via `pytest -m snapshots`).

See `docs/missions/agentwire-repl-textual.md` for the living checklist
and `docs/missions/agentwire-sdk-primitives.md` for the composite-views
roadmap.
