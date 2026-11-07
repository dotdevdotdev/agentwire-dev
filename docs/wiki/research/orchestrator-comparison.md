# How AgentWire differs from other multi-agent coding orchestrators

> Research note for [#1294](https://github.com/dotdevdotdev/agentwire-dev/issues/1294), from the post-1.79 brainstorm ([#1275](https://github.com/dotdevdotdev/agentwire-dev/issues/1275)). Sourced from each project's own repository and docs, read 2026-11-05. Tools move fast: re-check before quoting. **Anything marked _unverified_ was not stated on a primary page.** This is a survey, not a roadmap; the borrow candidates at the end are suggestions, not work done.

## Summary table

| | Form | License | Platforms | Isolation |
|---|---|---|---|---|
| **AgentWire** | CLI + tmux + web portal + MCP | Apache-2.0 ([`LICENSE`](../../../LICENSE)) | macOS/Linux + tmux ([quickstart](../quickstart.md)) | git worktree per session, plus no-isolation helper sessions ([worktree sessions](../sessions/worktree-sessions.md), [helper sessions](../sessions/helper-sessions.md)) |
| [Claude Squad](https://github.com/smtg-ai/claude-squad) | terminal TUI | AGPL-3.0 ([repo](https://github.com/smtg-ai/claude-squad)) | needs tmux and `gh`; OS list _unverified_ ([README](https://github.com/smtg-ai/claude-squad/blob/main/README.md)) | "isolated git workspace" per task; the source tree has [`session/git/worktree*.go`](https://github.com/smtg-ai/claude-squad/tree/main/session/git) |
| [Conductor](https://www.conductor.build/) | desktop app | _unverified_ (not stated on the [homepage](https://www.conductor.build/) or [docs](https://www.conductor.build/docs/)) | Mac ("Run parallel coding agents on your Mac") | "own workspace, branch, files, terminal, diff, and review path"; mechanism (worktree or not) _unverified_ |
| [Sculptor](https://github.com/imbue-ai/sculptor) | desktop app | MIT per the GitHub license field ([`LICENSE.md`](https://github.com/imbue-ai/sculptor/blob/main/LICENSE.md)); README says it is not yet truly open source and takes few outside contributions | Mac (Apple Silicon), Linux, Linux ARM64 ([README](https://github.com/imbue-ai/sculptor)) | default is a git worktree; clone and container modes are experimental ([workspaces](https://github.com/imbue-ai/sculptor/blob/main/docs/help/workspaces.md), [container backend](https://github.com/imbue-ai/sculptor/blob/main/docs/help/experimental/container_backend.md)) |
| [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams) | built into Claude Code | Claude Code's own terms; not an independent project | Any terminal for in-process; split panes need tmux or iTerm2 | none for teammates; the docs say "Break the work so each teammate owns a different set of files". Worktrees are a separate feature ([worktrees](https://code.claude.com/docs/en/worktrees.md)) |
| [Multica](https://github.com/multica-ai/multica) | self-hostable web workspace + daemon + CLI | Apache-2.0 plus extra conditions (no hosted-service use, branding retention) ([`LICENSE`](https://github.com/multica-ai/multica/blob/main/LICENSE)); the README calls it "source-available" | web, desktop (macOS/Windows/Linux), mobile ([README](https://github.com/multica-ai/multica)) | agents run on "a runtime you control" (a daemon on your machine); per-task isolation mechanism _unverified_ |

## Per tool

### Claude Squad
- **What:** "a terminal app that manages multiple Claude Code, Codex, Gemini (and other local agents including Aider) in separate workspaces" ([README](https://github.com/smtg-ai/claude-squad/blob/main/README.md)). Built on tmux and `gh`. Highlights: background tasks including auto-accept, and "Review changes before applying them, checkout changes before pushing them".
- **Does that AgentWire does not:** a single-window TUI that lists every task and reviews its changes in place. Per the README, review happens inside the app.
- **AgentWire does that it does not:** no sign in its README of voice, a web UI, remote machines, scheduled runs, a parent/child session model, report-back messaging. _Absence on a README page is not proof of absence in the code._

### Conductor
- **What:** "Run parallel coding agents on your Mac" with Claude Code, Codex and Cursor in isolated workspaces ([homepage](https://www.conductor.build/)). The [docs](https://www.conductor.build/docs/) describe a flow of workspace, diff viewer, checks, pull request, merge, archive.
- **Does that AgentWire does not:** an integrated diff viewer and checks step per workspace, in a native app.
- **AgentWire does that it does not:** runs off the Mac (Linux, remote machines over SSH), headless and scheduled work, and an agent-facing API (MCP). Pricing, license and the isolation mechanism were not stated on the pages read: _unverified_.

### Sculptor (Imbue)
- **What:** "a desktop app for running coding agents in parallel" ([README](https://github.com/imbue-ai/sculptor)). Workspace is "by default ... a git worktree"; harnesses are Claude Code and Pi, plus "any terminal-based agents". Experimental research preview.
- **Does that AgentWire does not:** a Files panel with Browse, Changes and Commits tabs and per-file diffs ([changes](https://github.com/imbue-ai/sculptor/blob/main/docs/help/changes.md)); in-app PR opening and status tracking ([README doc index](https://github.com/imbue-ai/sculptor)); an experimental container or remote backend.
- **AgentWire does that it does not:** tmux-resident sessions that outlive the UI, voice, channels, and the scheduler. Whether Sculptor has equivalents of the last three is _unverified_.

### Claude Code agent teams
- **What:** "Experimental and disabled by default" (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`). One lead session, teammates that are separate Claude Code instances with a shared task list and a mailbox ([docs](https://code.claude.com/docs/en/agent-teams)).
- **Does that AgentWire does not:** teammates message each other directly and self-claim tasks from a shared list with dependency tracking; hooks (`TeammateIdle`, `TaskCreated`, `TaskCompleted`) gate quality.
- **AgentWire does that it does not:** per its stated limitations, there is "No session resumption with in-process teammates", "One team per session", "No nested teams", and teammates are not worktree-isolated. AgentWire sessions are durable tmux sessions that survive the parent, nest arbitrarily, and each worktree session has its own branch. In exchange, agent teams are first-party and need no extra install.

### Multica
- **What:** "a source-available workspace where you assign work to AI coding agents the way you'd assign it to a teammate": issue board, agents as assignees, Squads, Skills, Autopilots (cron), execution logs, usage analytics, review gates ([README](https://github.com/multica-ai/multica)). A different category: a project-management product with agents, not a terminal orchestrator.
- **Does that AgentWire does not:** a board, issue assignment, multi-user workspaces and roles, per-run cost analytics, chat integrations (Slack and others), mobile apps. AgentWire ships no PM product by design.
- **AgentWire does that it does not:** local-first terminal sessions you can attach to, and voice. Whether Multica has an equivalent command guard is _unverified_.

## Where AgentWire sits

Taken together, the sourced differences are:

- **Substrate:** AgentWire's unit is a tmux session addressable from CLI, portal, MCP and remote machines ([concepts](../concepts.md)). The desktop apps (Conductor, Sculptor) and the TUI (Claude Squad) are the UI.
- **Topology:** parent/child sessions with report-back, fan-out cohorts and a fleet view ([fan-out cohorts](../sessions/fan-out-cohorts.md), [fleet](../sessions/fleet.md)). Claude Squad, Conductor and Sculptor present flat lists of workspaces in their READMEs and homepages; Multica's Squads are leader-routed agent groups.
- **Safety:** agentwire is not a guard; screening is the separate `damage-control-dev` project.
- **What AgentWire is weaker at:** no first-party diff-review UI (below), no native desktop app, and a tmux dependency.

## Worth borrowing (suggested issues, not work done)

1. **Worktree diff-review panel in the portal.** Conductor (diff viewer + checks) and Sculptor (Changes tab with per-file diffs and a Commits tab) both put review next to the agent; Claude Squad reviews in-app too. AgentWire has the `agentwire diff -s <session> [--base] [--json]` CLI command but no portal panel. A per-session panel that calls it, with the Changes/Commits split Sculptor uses, would close the gap.
2. **Checks surfaced beside the diff** (Conductor's docs list a "Checks" step after the diff viewer; contents _unverified_). Worth filing only if the panel above lands and CI status is not already visible there.

Not on the list: carrying gitignored files into worktrees. Claude Code's [`.worktreeinclude`](https://code.claude.com/docs/en/worktrees.md#copy-gitignored-files-into-worktrees) does this and AgentWire already has `projects.worktrees.copy_files` ([architecture](../architecture.md)).
