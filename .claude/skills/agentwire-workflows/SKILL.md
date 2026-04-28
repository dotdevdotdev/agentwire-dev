---
name: agentwire-workflows
description: Pi workflow engine — YAML-defined DAGs of pi invocations chained by Jinja2 templating + output extraction. Covers YAML anatomy (inputs, nodes, outputs, when, retries, on_error), CLI (`agentwire workflow list/validate/run/history/show`), MCP tools (`workflow_list/validate/run/history/show`), storage layout (metadata.json, context.json, per-node event logs), and debugging. Use when authoring a new workflow YAML, debugging a failed workflow run, explaining workflow concepts, or choosing between a workflow and a scheduler task. Full reference lives in docs/wiki/scheduling/workflows.md.
---

# Pi workflow engine — quick reference

Workflows live in `agentwire/workflows/examples/` (bundled) or `~/.agentwire/workflows/defs/` (user). Each node runs against one of two backends via `runner:` — `pi` (subprocess per node, default) or `anthropic` (`claude-agent-sdk` in-process, subscription auth). Both return the same `NodeResult` shape. See `docs/wiki/scheduling/workflows.md` → "Runners" for the full per-runner field table; quick reference below is pi-focused.

## Minimum viable YAML

```yaml
name: hello
nodes:
  greet:
    prompt: "Say hi in one word."
    # model defaults to glm-5.1 on provider zai — both optional
    tools: [read]
    thinking: "off"
```

## Everything the runner honors

- **Templating** — Jinja2 with `StrictUndefined` in `prompt:` and in workflow-level `when:` expressions (no `{{ }}` for `when`).
- **DAG** — `depends_on: [...]` (string or list). Topological sort + cycle detection at validate time.
- **Inputs** — declared under workflow-level `inputs:` with `type` (string|int|float|bool|json), `required`, `default`. Passed via `--input KEY=VAL` or `--input-file path.json`.
- **Output extraction** — per-node `outputs:` with `source: text | regex | jsonpath`. JSONPath subset: `$.a`, `$.a.b`, `$.a[*]`, `$.a[*].b`, `$.a[0]`. Nodes without declared outputs expose `{{ node_id.text }}`.
- **Retries** — `retries: N` (default 0), `retry_delay: S` (default 10). Triggered on status in `{failure, timeout}` only. Template/extraction errors never retry.
- **on_error** (after retries exhausted) — `fail` (halt) | `continue` (downstream gets `None` outputs, workflow → partial) | `branch` (requires `on_error_goto`; skip normal dependents, rescue target).
- **Skipping** — `when:` false OR upstream skipped/branched → node is `skipped`; dependents propagated unless rescued by a branch `on_error_goto`.
- **Tools (pi)** — subset of `{read, bash, edit, write, grep, find, ls}`. Default `[read, bash, edit, write]`. Empty list → `--no-tools`.
- **Thinking (pi)** — `off | minimal | low | medium | high | xhigh`. Default `medium`. Use `off` for flash-tier cheap nodes.
- **Anthropic runner** — uses `model` (required, e.g. `claude-opus-4-7`), `effort` (low|medium|high|max|xhigh), `thinking_config` (dict), CamelCase tools (`Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`, `WebFetch`, `WebSearch`). See `docs/wiki/scheduling/workflows.md` → Runners for the full table.

## CLI

```bash
agentwire workflow list [--json]
agentwire workflow validate <name-or-path>
agentwire workflow run <name> [--input KEY=VAL]... [--input-file FILE] [--runner pi|anthropic] [--dry-run] [--verbose] [--json]
agentwire workflow history [--workflow NAME] [--limit N] [--json]
agentwire workflow show <run-id> [--events] [--node ID] [--json]
```

`run` exits 0 on success|partial, 1 on failure.

## MCP

```
workflow_list()
workflow_validate(name_or_path)
workflow_run(name, inputs={}, dry_run=False)      # 600s timeout
workflow_history(workflow=None, limit=20)
workflow_show(run_id)
```

Agents should prefer MCP tools over shelling out.

## Storage

Runs live at `~/.agentwire/workflows/runs/<run-id>/`:
- `metadata.json` — workflow, status, inputs, node summaries (schema_version: 2 — includes `runner` at run level and per node)
- `context.json` — final inputs + per-node extracted outputs
- `nodes/<id>.events.jsonl` — raw event stream (pi JSONL for pi runner, pi-shaped translated events for anthropic)

Runs without `metadata.json` (crashed mid-run) are silently hidden from `history`.

## Common authoring pitfalls

- **Typo in `{{ analyze.issues }}`** → StrictUndefined raises. Check the node's `outputs:` names.
- **`source: jsonpath` but model emits fences** — extractor handles ` ```json ... ``` ` fences automatically; if extraction still fails, tighten the prompt ("no fences, no prose") and lower `thinking`.
- **on_error: branch without on_error_goto** — validate catches this. Target must exist as a node in the same workflow.
- **`when: "{{ x == 'y' }}"`** — wrong. `when:` is a Jinja *expression*, not a template. Write `when: "x == 'y'"`.
- **Adding MCP tool doesn't appear** — requires `agentwire rebuild && agentwire portal restart --dev` *plus* a Claude session restart.

## Scheduler integration

Workflows can be wired into `~/.agentwire/scheduler.yaml` with `workflow:` + `inputs:` fields (instead of `task:`). The scheduler dispatches them in-process via `run_workflow()` — no tmux, no ensure subprocess.

```yaml
tasks:
  nightly-doc-drift:
    schedule: { every: day, at: "23:00" }
    workflow: doc-drift-check
    inputs:
      paths: "docs/,{{ project }}"  # {{ task }}, {{ project }}, {{ session }}, {{ workflow }} expand
```

Status maps `success→complete`, `partial→incomplete`, `failure→failed`. `agentwire scheduler run <name> --dry-run` works for workflow tasks. The morning report renders per-node status badges.

Full reference in `docs/wiki/scheduling/workflows.md` → "Scheduler integration" section.

## When to pick which tool

| Need | Tool |
|---|---|
| Single interactive prompt | `claude` / `pi -p` |
| Recurring single Claude prompt on a schedule | `agentwire-scheduler` skill with `task:` |
| Recurring multi-step DAG on a schedule | `agentwire-scheduler` skill with `workflow:` (this skill for the DAG itself) |
| Multi-step logic, variables between steps, conditionals, retries | workflows |

## Full reference

`docs/wiki/scheduling/workflows.md` — concept intro, full YAML anatomy, per-field semantics, debugging guide, FAQ.

## Mission

Phase 2 status + roadmap lives in `docs/missions/pi-workflow-engine.md`. Sub-missions for later phases: `pi-scheduler-workflows.md` (Phase 3), `pi-workflow-advanced.md` (Phase 4), `pi-workflow-ui.md` (Phase 5).
