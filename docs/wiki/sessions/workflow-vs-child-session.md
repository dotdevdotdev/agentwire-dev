# Dynamic workflow vs child session

A Claude Code dynamic workflow is a JS script of subagents that runs inside **one** session. It is a leaf a session may run, never a session-to-session primitive. agentwire sessions stay the unit of lifecycle: branch, worktree, PR, portal window, voice, report-back. (#1332)

Full research, with sources and the documented/observed/inferred tags: `~/.agentwire/wiki/wiki/research/claude-code-dynamic-workflows.md`. This page is the decision summary; it links rather than copies.

## Which one

| Need | Use |
|---|---|
| Own branch, PR, portal window or voice | Child session (`agentwire worktree`) |
| Must outlive the parent session | Child session (a workflow dies with its session) |
| Report-back, prompt routing, restart `--resume`, cross-machine | Child session |
| N homogeneous, independent items inside one task (review N files, N checks, N-way research), no per-item PR | Workflow |
| Adversarial verify of findings (review, then refute) | Workflow |
| Sequential or tightly coupled edits | Neither: just do it in the session |
| Mid-run input or approval | Child session (workflows have no mid-run input) |

A child session may run a workflow internally; that is the intended composition. One cohort member = one session that runs a workflow, and the parent waits on that single child.

## Rules for a session that runs one

- Keep it within the configured `workflowSizeGuideline` (small = fewer than 5 agents). It is advice to Claude, not a cap.
- Collect the result BEFORE reporting; the session still owns the branch, the draft PR and `notify_parent`.
- Bypass posture never prompts for launch. Under other postures in `-p` mode a `Workflow` allow rule is needed.
- agentwire usage-limit recovery stays authoritative: workflow auto-pause does not apply to background or `-p` sessions.

## Leaves: the `aw-*` subagents

Hand each workflow agent a purpose-built definition via `agent(prompt, {agentType: 'aw-reviewer'})` rather than a generic prompt: `aw-reviewer`, `aw-verifier`, `aw-researcher`, `aw-implementer`, `aw-test-runner`, `aw-browser`. Installed by `agentwire hooks install`. Purpose, tools, model, return shape and what is documented vs observed: [workflow-subagents](workflow-subagents.md). (#1335)

## Scheduled tasks

The `ultracode` keyword does not fire from scheduled or `-p` prompts. A task that wants a workflow must say "use a workflow" in prose, or invoke a saved `/<name>`. See [scheduled workloads](../scheduling/scheduled-workloads.md#workflows-in-scheduled-tasks).

## DO NOT BUILD

Each of these couples agentwire to an undocumented, fast-moving research-preview surface:

- a workflow-to-cohort bridge or ledger
- a portal workflow viewer (Claude's `/workflows` is richer; the data format is undocumented)
- replacing worktree sessions with workflows
- a wrapper that authors workflow scripts

## Open risks (from the research, unverified)

- `agentwire idle-gate` (#1103) must treat a live workflow like a background task, or a worker is reaped mid-run.
- Whether user-scope hooks fire inside workflow agents is inferred by the subagent analogy, not observed.
