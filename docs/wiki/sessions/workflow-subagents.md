# Subagent definitions for workflow fan-out

Six Claude Code subagent definitions, prefixed `aw-` so they never collide with the owner's own. A session that fans out (see [workflow-vs-child-session](workflow-vs-child-session.md)) hands each workflow agent one of these instead of a generic prompt, so the parent's context stays clean and each leaf has the right tools, model and rails. (#1335)

Source: `agentwire/subagents/aw-*.md` (packaged in the wheel). `agentwire hooks install` links each into `~/.claude/agents/` through `hooks_cli._link_into_claude` (same provenance rules as slash commands and skills); `agentwire doctor` flags a stale or missing one.

## The set

| Agent | Purpose | Tools | Model | Returns |
|---|---|---|---|---|
| `aw-reviewer` | Read-only review of ONE angle (correctness, security, tests, simplicity) of a diff | Read, Grep, Glob, Bash | inherit | `{angle, findings[{file, line, severity, summary, failure_scenario}]}` |
| `aw-verifier` | Adversarial: tries to refute ONE finding against the code | Read, Grep, Glob, Bash | inherit | `{verdict: confirmed\|refuted, evidence, note}` |
| `aw-researcher` | Read-only investigator (code, docs, web) | Read, Grep, Glob, Bash, WebSearch, WebFetch | sonnet | `{answer, claims[{claim, source, basis}], gaps}` |
| `aw-implementer` | One scoped edit in an isolated worktree, runs the slice's fast tests | Read, Grep, Glob, Edit, Write, Bash | inherit | `{status, files_changed, tests_run, worktree, notes}` |
| `aw-test-runner` | Runs one named test set, returns only failures | Read, Grep, Glob, Bash | sonnet | `{command, passed, failed, failures[{test, output}]}` |
| `aw-browser` | Chrome-extension e2e check of the portal, one tab, closes it | Read + 12 `mcp__claude-in-chrome__*` tools | sonnet | `{url, observations, console_errors, passed, tab_id, tab_closed}` |

Names kept as the issue proposed. `aw-implementer` is the only one with `isolation: worktree` in its frontmatter.

Every definition says what it must not do: no `notify_parent`, no agentwire session or pane spawning, no merging, no PRs. The parent session owns the branch, the PR and the report. The `tools` allowlists back that up (none holds `Agent`, `Workflow` or `mcp__agentwire__*`), and a test fails if one does.

## How a workflow selects them

Use it in a workflow script as `agent(prompt, {agentType: 'aw-reviewer', schema: ...})`.

| Claim | Basis |
|---|---|
| `agent()` accepts `agentType`, "resolved from the same registry as the Agent tool", and composes with `schema` (a StructuredOutput instruction is appended to the agent's system prompt) | **Documented** (`workflow-authoring` skill) |
| The registry includes `~/.claude/agents/*.md`; precedence is managed > `--agents` > project `.claude/agents/` > user `~/.claude/agents/` > plugin; same name: higher priority wins | **Documented** (sub-agents docs) |
| `name` and `description` are required; `tools`, `model`, `isolation` are honoured when a subagent runs; unknown frontmatter fields are ignored without error | **Documented** (sub-agents docs) |
| A workflow agent with `agentType` actually receives that definition's `tools`, `model` and `isolation` | **Not documented, not observed.** Needs the owner's login; see below |
| Symlinked agent files load | **Not documented, not observed** |
| User-scope hooks fire inside workflow agents | **Inferred** by the subagent analogy |
| Frontmatter `hooks` do NOT fire for subagents | **Observed** (damage-control-dev research, one run). No definition here uses frontmatter hooks |

Because honouring is unverified, a script should not rely on the definition alone for the things that matter: pass `isolation: 'worktree'` on the `agent()` call for `aw-implementer`, and pass `model` explicitly where cost matters. Both are documented `agent()` options.

`aw-` agents are named, so a plugin or project agent of the same name would shadow or be shadowed by priority; the prefix makes that a non-issue in practice.

## Open verification (owner login required)

A scratch `CLAUDE_CONFIG_DIR` has no login, and the real `~/.claude` / `~/.claude.json` were deliberately not touched while building this. The three unverified rows above (definition honoured inside a workflow agent, symlinked definitions load, user-scope hooks fire) are settled by the owner running `~/.agentwire/artifacts/subagent-defs-owner-verify.sh` (commands only; run after `agentwire hooks install` from the installed tool).

## Related

- [workflow-vs-child-session](workflow-vs-child-session.md): when to run a workflow at all.
- Research: `~/.agentwire/wiki/wiki/research/claude-code-dynamic-workflows.md`.
