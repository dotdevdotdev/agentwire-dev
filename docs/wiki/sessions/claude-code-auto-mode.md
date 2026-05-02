> Living document. Update this, don't create new versions.

# Claude Code Auto Mode

Released March 24, 2026. Auto mode is a permission level between
`claude-prompted` (manual approval) and `claude-bypass` (no checks).
Before each tool call, a separate Claude Sonnet 4.6 classifier reviews the transcript
and decides whether to approve or block the action automatically.

See also: `../../missions/completed/feature-claude-auto-session-type.md` — original feature spec (shipped).

---

## How It Works

**Two-layer safety:**
1. **Input guard** — scans tool outputs (file reads, shell results, web fetches) for
   prompt injection before Claude sees them
2. **Output classifier** — evaluates each proposed tool call using:
   - Stage 1: fast single-token approve/block decision
   - Stage 2: detailed reasoning if Stage 1 flags it

The classifier receives: user messages + tool calls + CLAUDE.md content.
It deliberately does NOT see Claude's reasoning or tool outputs (prevents manipulation).

**Decision order:**
1. Explicit `allow`/`deny` rules resolve immediately (zero classifier cost)
2. Read-only actions → auto-approved (zero classifier cost)
3. File edits inside the working directory → auto-approved (zero classifier cost)
4. Everything else → classifier

**Auto-blocked examples:**
- `curl | bash`, wget piped to execution
- Pushing directly to `main` or force-pushing
- Mass deletion on cloud storage
- Granting IAM or repo permissions
- Production deploys and migrations
- Sending data to external endpoints

**Fallback:** if the classifier blocks 3 consecutive actions or 20 total in a session,
auto mode pauses and falls back to manual prompts. In headless/`-p` mode, repeated
blocks abort the session. AgentWire's `idle_timeout` already catches this — a stalled
agent waiting for a prompt it will never get goes idle.

**Performance (Anthropic's internal tests):**
- False positive rate: ~0.4% (very few safe actions incorrectly blocked)
- False negative rate: ~6–17% (some unsafe actions get through — not a guarantee)
- Token overhead: ~20% for command-heavy tasks; zero for file-edit-heavy tasks

---

## CLI Flags

**Both flags are required:**
```bash
claude --enable-auto-mode --permission-mode auto [rest of args]
```

`--enable-auto-mode` makes auto mode available; `--permission-mode auto` activates it.
Without the first flag, Shift+Tab also can't cycle to auto mode.

Cycle through modes interactively with **Shift+Tab**:
`default → acceptEdits → plan → auto`

---

## Critical: Auto Mode Strips Broad Allow Rules

Auto mode **removes** any broad allow rules from the effective permissions:
- `Bash(*)` — blanket shell access removed
- `Bash(python*)`, `Bash(node*)`, `Bash(ruby*)` — wildcard interpreters removed
- Package-manager run commands removed
- `Agent` allow rules removed

**Specific allow rules survive:** `Bash(git *)`, `Bash(npm test)`, `Bash(make build)`, etc.

This means `claude-auto` sessions that relied on `Bash(*)` (as bypass sessions effectively
do) need explicit specific allow rules instead. AgentWire must inject core allows at
launch time.

---

## Allow Rule Architecture

Auto mode requires a deliberate three-layer allow strategy:

### Layer 1: AgentWire Core Allows (injected via `--allowedTools` at launch)

AgentWire should inject these whenever it creates a `claude-auto` session.
These are commands any agentwire-managed agent might need:

```
Bash(agentwire *)    Bash(tmux *)
Bash(git status*)    Bash(git rev-parse*)   Bash(git checkout*)
Bash(git branch*)    Bash(git add*)         Bash(git commit*)
Bash(git push*)      Bash(git pull*)        Bash(git log*)
Bash(git diff*)      Bash(git worktree*)
Bash(gh pr create*)  Bash(gh pr view*)
Read(*)  Edit(*)  Write(*)  Glob(*)  Grep(*)
```

These bypass the classifier entirely — zero token cost. Everything else goes through it.

**Implementation:** pass via `--allowedTools` CLI arg at session creation (cleanest, no
file mutation, overrides nothing user-configured).

### Layer 2: User-Level Allows (`~/.claude/settings.json`)

Standard shell utilities that apply across all projects. The user configures this once.
Example baseline:

```json
{
  "permissions": {
    "allow": [
      "Bash(git *)", "Bash(gh *)", "Bash(ls *)", "Bash(which *)",
      "Bash(cat *)", "Bash(wc *)", "Bash(sort *)", "Bash(head *)",
      "Bash(tail *)", "Bash(mkdir *)", "Bash(cp *)", "Bash(mv *)",
      "Bash(chmod *)", "Bash(find *)", "Bash(grep *)", "Bash(diff *)",
      "Bash(pwd)", "Bash(date *)"
    ],
    "deny": [
      "Bash(rm -rf /)*", "Bash(rm -rf ~)*",
      "Bash(env *)", "Bash(printenv*)", "Bash(export *)"
    ]
  }
}
```

No `rm` in allow (classifier handles case-by-case). No `curl`/`wget` (classifier
reviews network access). No `npm`/`node` (goes in per-project layer).

### Layer 3: Project-Level Allows (`<project>/.claude/settings.json`)

Project-specific tooling. User configures once per project:

```json
{
  "permissions": {
    "allow": [
      "Bash(npm test*)", "Bash(npm run lint*)", "Bash(npm run build*)",
      "Bash(npx jest*)", "Bash(npx eslint*)", "Bash(npx tsc*)"
    ]
  }
}
```

### How Layers Merge

```
AgentWire core allows (--allowedTools)
  + User-level allows (~/.claude/settings.json)
  + Project-level allows (<project>/.claude/settings.json)
  = Full allow list for session
```

Allowed actions execute immediately, zero classifier cost. Everything else: classifier.

---

## Comparison to Existing Modes

| | `claude-bypass` (current) | `claude-auto` (proposed) |
|-|--------------------------|--------------------------|
| Permission prompts | None | None (classifier decides) |
| Safety checks | **None** | AI classifier blocks dangerous actions |
| Mass file deletion | Allowed | **Blocked** |
| Credential exfiltration | Allowed | **Blocked** |
| Force push to main | Allowed | **Blocked** |
| Normal file edits | Allowed | Allowed (auto-approved, no classifier cost) |
| Git operations | Allowed | Allowed (auto-approved) |
| Bash commands | Allowed | Classifier reviews unless in allow list |
| Token overhead | None | ~20% for command-heavy tasks |
| Headless stall on block | N/A | idle_timeout catches it |

**Bottom line:** `claude-auto` does everything `claude-bypass` does for normal overnight
work, but prevents catastrophic failures at 3am when nobody's watching.

---

## Constraints

| Constraint | Detail |
|------------|--------|
| **Anthropic API only** | Classifier runs on Sonnet 4.6; requires Anthropic auth. Third-party API proxies cannot use auto mode. |
| **Plan required** | Team or Enterprise plan (research preview). Pro/Max individual plans not supported. Admin must enable in Claude Code admin settings first. |
| **Model requirement** | Session model must be Sonnet 4.6 or Opus 4.6. Not available on Haiku or claude-3. |
| **Not a safety guarantee** | 6–17% false negative rate. Not for production systems without backup strategy. |

---

## Using `claude-auto` in AgentWire

The session type is wired through every entry point — `.agentwire.yml`, the CLI, and MCP — and `agentwire/__main__.py` injects a core allowlist at launch so the classifier doesn't review routine git/agentwire/tmux ops.

**`.agentwire.yml`:**
```yaml
type: claude-auto
roles:
  - task-runner
```

**CLI:**
```bash
agentwire new myproject --type claude-auto
```

**MCP:**
```python
session_create(name="myproject", session_type="claude-auto")
```

The injected core allows cover `agentwire *`, `tmux *`, common `git *` / `gh pr *` operations, and the read/edit/write/glob/grep tools. Anything outside that list flows through the classifier (or, if it matches your user/project allowlists from Layer 2/3 above, runs without classifier cost).

---

## Edge Cases

**Classifier hits fallback (3+ consecutive blocks):**
Agent stalls waiting for a prompt that won't come. AgentWire's `idle_timeout` catches it
naturally. Task reports as `timeout` in scheduler history. Morning dashboard surfaces
which tasks blocked and what action triggered it.

**Mixed session types:**
Some tasks use `claude-auto` (production repos), others `claude-bypass` (sandboxed
experiments). Session type is per-session — both run simultaneously without conflict.

**Haiku incompatibility:**
Auto mode requires Sonnet 4.6 or Opus 4.6 and Anthropic auth. Tasks using Haiku
cannot use auto mode. AgentWire should warn at session creation time if the
combination is invalid.

---

## References

- [Anthropic Engineering: Claude Code Auto Mode](https://www.anthropic.com/engineering/claude-code-auto-mode)
- [Claude Blog: Auto Mode Announcement](https://claude.com/blog/auto-mode)
- [Simon Willison write-up](https://simonwillison.net/2026/Mar/24/auto-mode-for-claude-code/)
- `../../missions/completed/feature-claude-auto-session-type.md` — original feature request (archived)
