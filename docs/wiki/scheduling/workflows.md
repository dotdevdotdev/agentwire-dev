> Living document. Update this, don't create new versions.

# Pi Workflows

Programmable automation for the pi coding agent. A workflow is a YAML file that chains one or more pi invocations into a DAG with templated prompts, structured inputs, and output extraction between nodes.

**When to reach for workflows**

| Use case | Tool |
|---|---|
| One-off interactive prompt | `claude` or `pi -p` |
| Recurring prompt on a schedule | `agentwire scheduler` with a `task:` (single Claude prompt) — see `agentwire-scheduler` skill |
| Recurring multi-step DAG on a schedule | `agentwire scheduler` with a `workflow:` reference — the scheduler dispatches the workflow in-process |
| Multi-step logic with conditional branches, variables flowing between steps, retries | **workflows** |

Workflows compose *small reliable nodes* instead of praying a single giant prompt does the right thing.

---

## Anatomy of a workflow YAML

```yaml
name: my-workflow                    # unique name, falls back to filename stem
description: Short human summary.
version: 1                           # schema version, always 1 today

inputs:                              # CLI-supplied variables, optional
  target:
    type: string                     # string | int | float | bool | json
    required: true
    description: What we're targeting
  verbose:
    type: bool
    default: false

nodes:                               # at least one required
  analyze:
    runner: pi                       # pi | anthropic (default: pi). See "Runners" below.
    prompt: |                        # Jinja2 template — required
      Look at {{ inputs.target }}. Return JSON: {"issues": [...]}
    # provider + model are optional — default to zai + glm-5.1
    # set explicitly only when you need a different model/provider
    model: glm-5.1                   # default: glm-5.1 (pi); anthropic needs e.g. claude-opus-4-7
    provider: zai                    # default: zai (pi-only)
    tools: [read, grep]              # pi: lowercase {read, bash, edit, write, grep, find, ls}
    thinking: "low"                  # pi: off|minimal|low|medium|high|xhigh
    timeout: 300                     # seconds per attempt (default 300)
    retries: 2                       # extra attempts on failure|timeout (default 0)
    retry_delay: 10                  # seconds between attempts (default 10)
    on_error: continue               # fail|continue|branch (default fail)
    outputs:                         # extract named vars for downstream nodes
      - name: issues
        source: jsonpath
        pattern: $.issues[*]

  report:
    depends_on: [analyze]            # DAG edge — can be string or list
    when: "analyze.issues|length > 0"  # Jinja expression; skip if false
    prompt: |
      Found these issues:
      {% for issue in analyze.issues %}  - {{ issue }}
      {% endfor %}
      Write a 3-bullet summary.
```

Bundled examples live in `agentwire/workflows/examples/` and are always discoverable. User-authored workflows go in `~/.agentwire/workflows/defs/` and override examples of the same name.

---

## Runners

Each node runs against one of two backends, selected per-node via `runner:`. Both runners return the same `NodeResult` shape — the rest of the workflow engine (DAG, templating, outputs, retries, `on_error`, scheduler hookup) is identical.

| Runner | Default model | Process model | Tool namespace | When to pick |
|---|---|---|---|---|
| `pi` (default) | `glm-5.1` on Z.AI | Subprocess per node (`pi -p --mode json`) | lowercase: `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls` | Cheap, fast, deterministic for one-shot extraction/transform nodes. Flash-tier (`glm-4.7-flash`) is free. |
| `anthropic` | (none — required) | In-process via `claude-agent-sdk`, subscription auth | CamelCase: `Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`, `WebFetch`, `WebSearch` | Quality-sensitive reasoning, Claude's richer toolset, anywhere the pi subprocess overhead dominates. Events stream live under `--verbose`. |

### Anthropic-only fields

| Field | Values | Notes |
|---|---|---|
| `model` | `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-sonnet-4-5`, `claude-haiku-4-5`, … | Required. No default. |
| `effort` | `low`, `medium`, `high`, `max`, `xhigh` | `max` = Opus-only. `xhigh` = Opus 4.7-only. Not valid on Haiku 4.5 or Sonnet 4.5. |
| `thinking_config` | `{type: adaptive}` (recommended), `{type: disabled}`, `{type: enabled, budget_tokens: N}` | `enabled` removed on 4.6/4.7 — use `adaptive` + `effort`. |
| `task_budget_tokens` | int ≥ 20000 | Opus 4.7 only. Beta. |
| `max_thinking_tokens`, `max_budget_usd` | int / float | Passed through to the SDK. |

Pi ignores these; setting them on a pi node is a parse-time validation error.

### Pi-only fields

| Field | Values | Notes |
|---|---|---|
| `thinking` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh` | String. Default `medium`. Use `off` for flash-tier cheap nodes. |
| `provider` | `zai` (default) | Leave default unless routing through a different Z.AI deployment. |

Anthropic uses `thinking_config` (dict) instead; the pi string `thinking:` field is simply ignored when `runner: anthropic`. No auto-translation — if you switch a node from pi to anthropic, rewrite the thinking field too.

### `--runner` override

To run an entire workflow on the other runner without editing YAML:

```bash
# Force every node to anthropic (YAML declarations ignored)
agentwire workflow run daily-book-report --runner anthropic

# Or force to pi
agentwire workflow run claude-tool-use --runner pi
```

Override applies to **every node** in the workflow. Field/runner mismatches surface as normal validation errors — e.g. overriding an anthropic workflow to pi rejects any `effort:`, `task_budget_tokens:`, or `thinking_config:` fields. Fix the YAML or pick a different override.

This is the canary / A-B flag: "run the same workflow on both runners, see which wins." Past runs record which runner executed them — `agentwire workflow show <run-id>` prints a `Runner:` line, and `workflow history` has a `runner` column — so comparing outcomes after the fact is direct.

### Live event output (`--verbose`)

`agentwire workflow run … -v` streams a one-line summary per event as nodes execute. Anthropic nodes emit live tool calls, tool results, text fragments, token counts, and agent-end timings:

```
[summarize] → tool_use Read file_path='README.md'
[summarize] ← tool_result (ok) # AgentWire - Voice interface for…
[summarize] ▓ AgentWire is a voice interface for AI coding agents…
[summarize] ✓ turn 1342+187 tok
[summarize] ■ agent_end 4.2s
```

Pi nodes are silent under `--verbose` (pi parses stdout after the subprocess exits — retrofitting live streaming is deferred).

---

## Research tools — `agentwire brave`

For workflows that need fresh web data (news digests, trend scans, "what happened today" reports), **don't use the SDK's built-in WebSearch/WebFetch tools**. A three-way A/B on `ai-morning-briefing` (2026-04-21) showed they were the quality bottleneck — even Opus 4.7 produced repetitive, thin output when the research came from WebSearch. The same model produced dramatically better output when research went through Brave Search API.

The `agentwire brave` CLI wraps the Brave API and emits results in an LLM-friendly compact format. Use it from any workflow via the `Bash` tool:

```bash
# Default: compact pipe-separated output (title | url | age | description)
agentwire brave "GPT-5 release"

# Tighter result set, wider time window
agentwire brave "open source LLM release" --count 5 --freshness pw
# --freshness: pd=past day (default), pw=past week, pm=past month, py=past year

# Raw JSON when you need structured parsing
agentwire brave "Anthropic funding" --json
```

Add `BRAVE_SEARCH_API_KEY` to `~/.agentwire/.env` (free tier: $5 credits/month ≈ 1000 searches).

Pattern for a research workflow node:

```yaml
nodes:
  research_and_send:
    runner: anthropic
    model: claude-opus-4-7
    effort: high
    thinking_config: { type: adaptive }
    tools: [Bash, Write]     # NO WebSearch/WebFetch
    prompt: |
      Run 5-7 queries via `agentwire brave "..." --count 5` to cover your
      topics. Compose from the compact output. Fetch specific URLs with
      `curl -sL | python3 -c "strip html"` only if Brave's description
      field is too thin.
```

The pattern is deliberately runner-agnostic — pi workflows can call `agentwire brave` through their `bash` tool identically.

---

## History, persistence, and notifications

Every run persists **independently of notifications**. Whether the workflow emails, posts to Slack, or does nothing at all, you always get:

```
~/.agentwire/workflows/runs/<run-id>/
├── metadata.json                  # run manifest (workflow, status, runner, costs, inputs)
├── context.json                   # final Context (inputs + per-node extracted outputs)
└── nodes/
    └── <node-id>.events.jsonl     # full event stream per node (tool calls, results, text)
```

**Notification is a prompt-level choice, not an engine feature.** If a prompt calls `agentwire email` or `agentwire webhook send` or `agentwire quo send`, that channel fires. If it doesn't, nothing goes out — but history is still there. See `agentwire/workflows/examples/silent-save.yaml` for an action-only workflow that produces a markdown file and zero notifications.

You can inspect past runs three ways:

1. **CLI** — `agentwire workflow history` lists recent runs (status, runner, duration, cost). `agentwire workflow show <run-id>` drills into a single run.
2. **Portal** — open the sidebar, expand **Workflows**, click a run. Shows metadata, per-node tool calls, tokens, and final text.
3. **Raw files** — `cat ~/.agentwire/workflows/runs/<run-id>/nodes/<node-id>.events.jsonl` gives you every event the runner produced.

---

## CLI reference

```bash
# Discover
agentwire workflow list
agentwire workflow list --json

# Validate without running
agentwire workflow validate my-workflow
agentwire workflow validate /path/to/custom.yaml

# Execute
agentwire workflow run my-workflow
agentwire workflow run my-workflow --input target=src/api.ts
agentwire workflow run my-workflow --input target=src/api.ts --input verbose=true
agentwire workflow run my-workflow --input-file inputs.json
agentwire workflow run my-workflow --runner anthropic   # override runner for every node
agentwire workflow run my-workflow --dry-run            # plan only, no pi calls
agentwire workflow run my-workflow --verbose            # show plan + live events
agentwire workflow run my-workflow --json               # structured result

# Inspect past runs
agentwire workflow history
agentwire workflow history --workflow my-workflow --limit 5
agentwire workflow history --json

agentwire workflow show <run-id>                        # summary
agentwire workflow show <run-id> --node analyze         # just that node's events
agentwire workflow show <run-id> --events               # all nodes' events, prefixed
agentwire workflow show <run-id> --json                 # raw metadata
```

`--input KEY=VALUE` is repeatable. `--input` wins over `--input-file` on conflicts.

`workflow run` exits **0** on `success` or `partial`, **1** on `failure`.

---

## MCP tools

Agents running inside agentwire sessions can drive workflows via MCP:

| Tool | Purpose |
|---|---|
| `workflow_list` | List discoverable workflows |
| `workflow_validate(name_or_path)` | Parse + validate YAML |
| `workflow_run(name, inputs={}, dry_run=False)` | Execute; returns full run result dict |
| `workflow_history(workflow=None, limit=20)` | List past runs |
| `workflow_show(run_id)` | Fetch one run's metadata |

`workflow_run` uses a 600s subprocess timeout — plenty for most pipelines. If yours exceeds that, split it into multiple workflows.

---

## Templating

Prompts and node fields are rendered as [Jinja2](https://jinja.palletsprojects.com/) templates with `StrictUndefined` — typos raise `UndefinedError` loudly instead of becoming silent empty strings.

Available in the namespace:
- `inputs.X` — workflow-level inputs
- `<node_id>.<output_name>` — extracted outputs of any upstream node

```jinja
{{ inputs.file }}
{{ analyze.issues[0] }}
{% for issue in analyze.issues %}  - {{ issue }}
{% endfor %}
{% if verify.status == 'pass' %}✓ passed{% endif %}
```

If a node doesn't declare explicit `outputs:`, its entire final assistant message is exposed as `{{ node_id.text }}`.

---

## Output extraction

Each node can declare `outputs:` that extract named values from its final assistant message. Three sources are supported:

### `source: text`
```yaml
outputs:
  - name: reply
    source: text                    # whole final message, trimmed
```

### `source: regex`
```yaml
outputs:
  - name: ticket
    source: regex
    pattern: "TICKET-(\\d+)"        # group 1 if present, else full match
```

### `source: jsonpath`
Expects the node's final message to be a JSON object (with or without a ```json fence).

```yaml
outputs:
  - name: issues
    source: jsonpath
    pattern: $.issues[*]            # supports $.a, $.a.b, $.a[*], $.a[*].b, $.a[0]
```

Set `required: false` to keep the node successful even if extraction fails (the extracted value becomes `None` and surfaces in `NodeResult.error` as a soft failure).

---

## Conditionals: `when:`

A node's `when:` is a Jinja **expression** (no `{{ }}` braces). It's evaluated against the current context in a sandboxed environment. Falsy → the node is skipped, and *all its transitive dependents are skipped too*.

```yaml
restate:
  depends_on: [verify]
  when: "verify.status == 'pass'"   # note: no braces

rollback:
  depends_on: [verify]
  when: "verify.status == 'fail'"
```

Both branches evaluate independently; one skips, the other runs.

An undefined variable in `when:` raises, same as in `prompt:`. Use `|default(...)` filters for optional paths.

---

## Retries + error handling

```yaml
flaky:
  retries: 2            # up to 3 attempts total
  retry_delay: 10       # seconds between attempts
  on_error: continue    # fail | continue | branch
```

`retries` triggers on status in `{failure, timeout}`. Template and extraction errors are deterministic and not retried.

`on_error` runs *after retries are exhausted*:

- **`fail` (default)** — halt the workflow. All later nodes appear as `skipped (not reached)`.
- **`continue`** — the node's outputs become `None` (or `{text: ""}` if none declared). Dependents still run and see those Nones. Workflow ends in `partial` status.
- **`branch`** — skip normal dependents, but `on_error_goto` target still runs.
  ```yaml
  try_thing:
    on_error: branch
    on_error_goto: rollback       # must reference an existing node
  ```

Workflow status:
- `success` — every node succeeded
- `partial` — ≥1 node skipped or failed-continue, nothing halted
- `failure` — a node with `on_error: fail` failed (execution halted)

`partial` exits 0. `failure` exits 1.

---

## Storage layout

Every run leaves a directory under `~/.agentwire/workflows/runs/<run-id>/`:

```
verify-or-rollback-20260415T...-abc/
├── metadata.json               # workflow, status, inputs, node summaries
├── context.json                # final Context: inputs + per-node outputs
└── nodes/
    ├── verify.events.jsonl     # raw pi JSONL stream for that node
    ├── restate.events.jsonl
    └── rollback.events.jsonl
```

`metadata.json` is the source of truth for `workflow history` and `workflow show`. Runs missing `metadata.json` (crashed mid-execution) are silently hidden from listings.

```json
{
  "schema_version": 1,
  "workflow": "verify-or-rollback",
  "run_id": "verify-or-rollback-20260415T080302-e7e2ff62",
  "status": "partial",
  "started_at": 1776254582.37,
  "duration_ms": 4203,
  "error": null,
  "inputs": {"target": "fail"},
  "nodes": [
    {"id": "verify", "status": "success", "attempts": 1,
     "duration_ms": 2701, "tokens": {...}, "error": null},
    ...
  ]
}
```

No retention policy yet — if `runs/` grows too large, clear it manually.

---

## Scheduler integration

A scheduler task in `~/.agentwire/scheduler.yaml` can reference a workflow with `workflow:` + `inputs:` instead of declaring a `task:` that runs via `agentwire ensure`. The scheduler dispatches the workflow **in-process** — no tmux session, no Claude Code subprocess, no project required (unless you want git gates or auto-commit).

### Minimum example

```yaml
# ~/.agentwire/scheduler.yaml
tasks:
  nightly-doc-drift:
    schedule:
      every: day
      at: "23:00"
    workflow: doc-drift-check        # name from `agentwire workflow list` OR absolute YAML path
    inputs:
      paths: "docs/,agentwire/"
```

### Ensure tasks vs workflow tasks

| | ensure task | workflow task |
|---|---|---|
| Field used | `task: <name-from-.agentwire.yml>` | `workflow: <name-or-path>` |
| Dispatch | `agentwire ensure` subprocess in a tmux session | `run_workflow()` in-process, no tmux |
| Session field | required (`session:`) | omitted (no session is created) |
| Project field | required | optional; required only if you use git gates or want auto-commit |
| Model | Claude (whatever the session type is) | Whatever each workflow node specifies — per-node pi models |
| Lifecycle hooks | `.agentwire.yml` pre/prompt/post | workflow DAG nodes with retries, branching, outputs |

A task must set **either** `task:` **or** `workflow:` — not both. `inputs:` is only valid with `workflow:`.

### Input templating

String values in `inputs:` expand four built-in variables from the scheduler's task context:

```yaml
tasks:
  my-wf:
    schedule: { every: 4h }
    workflow: summarize-project
    inputs:
      who: "{{ task }}"              # → "my-wf"
      where: "{{ project }}"         # → expanded project path (if set)
      session_hint: "{{ session }}"  # → session name (if set)
      self: "{{ workflow }}"         # → "summarize-project"
```

Unknown variables pass through untouched so workflow-node Jinja (`{{ inputs.x }}`, `{{ upstream.y }}`) still works. This is deliberately simpler than the full Jinja engine used inside workflow nodes — `{{ task }}`, `{{ project }}`, `{{ session }}`, `{{ workflow }}` are the only scheduler-context vars recognised at this layer.

### Status mapping

The workflow engine's three statuses map to scheduler status:

| Workflow | Scheduler | Exit intent |
|---|---|---|
| `success` | `complete` | Everything ran cleanly |
| `partial` | `incomplete` | ≥1 node skipped or failed-continue; workflow didn't halt |
| `failure` | `failed` | A node with `on_error: fail` failed and halted the workflow |

Workflow-load failures (bad YAML, missing workflow) also record as `failed` with a single-line blocker in the event log.

### Dry-run a workflow task

```bash
agentwire scheduler run my-wf --dry-run
```

Resolves the workflow, renders inputs, and prints the execution plan — no pi calls, no state update. Works for workflow tasks only (ensure tasks exit with a message).

### What lands in the morning report

Every `task_completed` event from a workflow task carries the workflow name, run id, and a compact node list:

```json
{
  "event": "task_completed",
  "task": "nightly-doc-drift",
  "status": "complete",
  "workflow": "doc-drift-check",
  "run_id": "doc-drift-check-20260416T230000-abc...",
  "nodes": [
    {"id": "scan", "status": "success", "duration_ms": 4201, "attempts": 1},
    {"id": "report", "status": "success", "duration_ms": 2104, "attempts": 1}
  ],
  ...
}
```

`agentwire scheduler report --artifact` renders per-node status badges alongside each workflow row, plus a run-id breadcrumb you can pass to `agentwire workflow show <run_id>` for the full event drill-down.

### Gates, priority, max_runs, once

All scheduler primitives work identically for workflow tasks. A workflow task can:
- Use `gate: { git_diff: [paths...] }` (requires `project:`)
- Set `priority`, `cooldown`, `not_before`/`not_after`
- Auto-disable with `max_runs: N` or `once: true`

### Auto-commit caveat

The scheduler's auto-commit step only runs for workflow tasks when `project:` is set. Workflow tasks without a project skip auto-commit; the expectation is that any filesystem mutations the workflow made are either intentional (pi's `bash` / `edit` / `write` tools) and will be committed by the workflow itself, or are transient and don't need capture.

### End-to-end smoke

```bash
# 1. Add a workflow task to ~/.agentwire/scheduler.yaml (see minimum example above)
# 2. Verify board parses
agentwire scheduler board

# 3. Dry-run to see the plan
agentwire scheduler run nightly-doc-drift --dry-run

# 4. Fire it
agentwire scheduler run nightly-doc-drift

# 5. Check the run
agentwire workflow history --limit 1
agentwire workflow show <run_id>

# 6. Morning report
agentwire scheduler report --since 1h --artifact
```

---

## Writing good pi prompts

Pi is a one-shot, stateless agent per node. These tips keep nodes cheap and reliable:

- **Constrain tools.** If a node only reads files, set `tools: [read]`. Excess tools invite unnecessary calls.
- **Dial thinking.** `thinking: "off"` for structured/obvious tasks, `"medium"` for analysis, `"high"+` only for deep reasoning. Each level up costs more tokens.
- **Ask for JSON explicitly.** "Return ONLY a JSON object (no prose, no code fences) in this shape: …" works far better than assuming a schema.
- **Default model is `glm-5.1` on provider `zai`** — you don't need to declare `model:` or `provider:` unless you want a different one. Override per-node for experiments (`model: glm-4.7-flash` for cheap throwaway work, etc.).
- **Set timeouts for network-bound nodes.** `timeout: 60` for a `gh` command; default 300 for thinking tasks.

---

## Debugging

### A node fails or produces the wrong output

```bash
# See the raw pi event stream for one node
agentwire workflow show <run-id> --node analyze

# See everything with node prefixes
agentwire workflow show <run-id> --events
```

### Downstream template raises UndefinedError

`StrictUndefined` surfaces typos. Check the failing node's declared `outputs:` names match what the downstream template references. The run's `context.json` shows what was actually in scope.

### Output extraction failed

If you declared `source: jsonpath` but the model wrapped its response in ```json fences, the extractor handles that — but if the JSON itself is malformed, extraction fails. Lower `thinking` and tighten the prompt to make the output more deterministic.

### Retries never fired

`retries` trigger on `failure` or `timeout` only. Template errors and output extraction errors are deterministic and won't retry — fix the YAML or the prompt.

### MCP tool doesn't appear

MCP tools ship in the same package. After any code change:
```bash
agentwire rebuild && agentwire portal restart --dev
```
Then restart your Claude session — the MCP server runs as a separate process started by Claude Code.

---

## FAQ

**Can I run workflows in parallel?** Not yet. Nodes run sequentially in topological order. Parallel fan-out is Phase 4.

**Can one workflow trigger another?** Not directly. You can invoke `agentwire workflow run` from a `bash`-tool prompt inside a node, but that's a composition hack. First-class sub-workflows are deferred.

**What about untrusted workflow YAMLs?** The `when:` sandbox is `ImmutableSandboxedEnvironment` — dunders and most dangerous builtins are blocked — but workflow YAMLs are currently treated as trusted local code. Don't run YAMLs you haven't reviewed.

**Can I see per-attempt events when a node retries?** Not yet. Only the final attempt's event log is kept on disk. If you need per-attempt observability, log from inside your prompt (pi will echo).

**How do I share workflows across machines?** Copy the YAML file into `~/.agentwire/workflows/defs/` on each machine. No sync yet.
