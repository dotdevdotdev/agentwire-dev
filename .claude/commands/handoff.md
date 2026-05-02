---
description: Distill this conversation into a shareable handoff bundle (ai-handoff.md + show-the-story.html)
---

# /handoff

You are about to compile this conversation into a **shareable handoff bundle**. The output is two artifacts:

1. **`ai-handoff.md`** — XML-tagged markdown a teammate can paste into another LLM (Opus 4.7) to roughly continue the conversation.
2. **`show-the-story.html`** — single-file presentation a human can open in a browser.

You are uniquely positioned to do this well: you already have full conversation context. Don't ask the user clarifying questions unless something is genuinely ambiguous — distill what you know.

## Workflow

### 1. Initialize the bundle

Call the MCP tool to create the bundle dir and pre-fill the template with git state and the CLAUDE.md/rules/memory chain:

```
mcp__agentwire__handoff_init(title="<short slug>")
```

The tool returns a `bundle_dir` and an `ai_handoff_path`. The pre-filled template at `ai_handoff_path` already has:
- `<metadata>` populated with cwd, branch, commit, repo url
- `<instructions>` populated with the full CLAUDE.md chain (verbatim)
- `<project_state>` populated with `git status`, `git log`, and uncommitted diff

You **must not touch** the `<instructions>` section unless redacting something sensitive — it's what makes the bundle portable across machines.

### 2. Read the pre-filled template

Use Read on `ai_handoff_path` to see exactly what got pre-filled.

### 3. Fill in the rest

Use Write (or Edit) on `ai_handoff_path` to replace every `{{ ... }}` placeholder with real content. Specifically:

#### `<title>`
One short line summarizing the session — what it was about, not what was decided. Roughly 4-8 words.

#### `<metadata>` extras
Fill `started_at` / `ended_at` if you can estimate them, `user_identity` (if mentioned), and `mcp_servers` (the ones actually used in this conversation).

#### `<environment>`
What the receiver can't see from `cwd` alone. Active panes, channels (Slack/Discord/email), scheduler state if relevant. If nothing notable, write a one-line "no special environment" note.

#### `<conversation_summary>`
This is the most load-bearing section. Be dense and structured.

- `<goal>` — one sentence: what this session set out to do.
- `<tldr>` — one paragraph the receiver reads first. Convey decisions made, current state, and what's next.
- `<decisions>` — every meaningful decision. For each: `<title>` (short name), `<rationale>` (why), `<alternatives>` (what was considered and not picked). If a user message confirmed a choice, that's a decision.
- `<dead_ends>` — things tried and rejected. Critical: this saves the receiver from retracing your steps.
- `<open_threads>` — what's unresolved. Be specific. "tests/handoff/test_parser.py needs malformed-input cases" beats "tests pending".
- `<stats>` — turns, files_touched, tools_used, duration_minutes. Estimates are fine.

#### `<journey>`
3-7 narrative beats — the *story* of the session, not the transcript. For each beat:
- `<beat title="...">` — short headline.
- `<quote>...</quote>` — optional verbatim line from the conversation that captured the turn (user or assistant).
- `<what_happened>...</what_happened>` — what actually changed.

This drives the visual presentation in show-the-story.html. Aim for memorable beats: turning points, surprises, decisions, blockers.

#### `<recent_turns>`
The last ~10-20 turns, **filtered**:
- Drop tool noise (file reads, searches) unless the result drove a decision.
- Keep all user turns verbatim — they're the signal of intent.
- Keep assistant turns that made decisions, asked questions, or summarized.
- Use markdown: `**user:**` / `**assistant:**` labels, blockquotes for quoted content.

#### `<handoff>`
Direct instructions to the next agent.
- `<one_sentence>` — what they should do first.
- `<resume_at>` — concrete file path / TODO id / step number.
- `<caveats>` — permission boundaries ("don't push", "don't edit X without asking"), env-specific notes, anything the receiver could trip on.

#### `<theme>`
Pick a palette and vibe that matches the **emotional tone** of the session. A debugging crisis at 2am gets different colors than a clean greenfield design pass. Be honest in `mood`. The shape:

```json
{
  "name": "short-slug",
  "mood": "honest read of the session's emotional tone — 1 short sentence",
  "palette": {
    "bg": "#hex",
    "surface": "#hex",
    "fg": "#hex",
    "muted": "#hex",
    "accent": "#hex",
    "accent_2": "#hex",
    "border": "#hex"
  },
  "fonts": {
    "heading": "css font-family stack",
    "body": "css font-family stack"
  },
  "motion": "subtle"
}
```

Tips:
- Dark backgrounds work better in this template — `bg` should be deep.
- `accent` is the dominant interactive color (tabs, headlines, highlights). Pick something that signals the mood.
- `accent_2` is for warnings / dead-ends. Often a warm color.
- Keep `fonts.heading` monospace-leaning, `fonts.body` sans-serif. Override only if the mood justifies it.
- `motion`: `"subtle"` (default), `"playful"` (longer transitions), `"none"` (instant).

### 4. Render the HTML

After writing `ai-handoff.md`, render the HTML:

```
mcp__agentwire__handoff_render(bundle_dir="<from step 1>", story=true)
```

If render fails, the parser will tell you which tag is missing or malformed — fix and re-run.

### 5. Report

Give the user:
- The bundle dir path
- A one-sentence summary of what you produced
- An explicit pointer to open `show-the-story.html` in a browser

Don't paste the full ai-handoff.md back — it's long. Just confirm it's saved and mention the highlights you captured (number of decisions, dead ends, journey beats, theme name/mood).

## Quality bar

The receiver's first impression is everything. A great handoff:
- Reads cold — the receiver doesn't need to be in this folder.
- Is honest about dead ends. Hiding what didn't work wastes the receiver's tokens.
- Names specific files and line numbers, not vague pointers.
- Picks a theme that captures the session's actual feel. Don't pick "professional blue" for a chaotic debug session.

## Don't

- Don't ask the user "should I include X?" — make the call yourself.
- Don't paste the entire conversation as `<recent_turns>` — filter aggressively.
- Don't over-design the theme. The structure is fixed; you're picking colors and vibes, not a layout.
- Don't edit `<instructions>` (the CLAUDE.md chain) unless redacting secrets.
