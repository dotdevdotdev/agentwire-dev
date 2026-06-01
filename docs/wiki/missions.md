# Missions

> Living document. Update this, don't create new versions.

Missions are AgentWire's third dispatch surface, sibling to Sessions and Tasks/Workflows. A mission is the **issue → branch → draft PR → review → merge** loop: a GitHub issue labeled `agent-ready` becomes a worker session in an isolated worktree, the worker opens a draft PR, PR-review feedback gets routed back into the worker's session, and the whole thing gets garbage-collected when the PR closes.

Stateless orchestrators (launchd) tick the GitHub issue board. Persistent worker sessions (claude-bypass) do the actual engineering work. Local state is small and JSON.

## When to use missions vs. scheduler tasks

| Use case | Surface |
|---|---|
| "Fix bug #195" (one engineering cycle → PR) | **Mission** |
| "Run prod metrics export every night" | Scheduler task |
| "Plot agent activity hourly" (no PR involved) | Scheduler task |

Missions are *engineering work* with a PR as the unit of delivery. Scheduler tasks are *automation runs* that don't necessarily produce code.

## Architecture

```
ORCHESTRATORS — launchd, stateless, exit per tick

  ① mission-dispatcher       every 30m, work hours 09:00–18:00
     picks an eligible issue → spawns worker session → injects initial
     prompt → exits

  ② mission-feedback-router  every 15m, 24/7
     polls each active mission's PR for new reviews → writes per-mission
     summary file → /clear + refresh prompt to the worker → exits

  ③ mission-janitor          every 6h, 24/7 (RunAtLoad)
     enumerates */mission-* sessions → checks PR state → tears down
     MERGED/CLOSED sessions + worktrees → sweeps for orphans → exits

WORKERS — persistent claude-bypass tmux sessions, one per active issue

  session name: {repo_short}/mission-{N}-{slug}
  worktree:     {projects_dir}/{repo_short}-worktrees/mission-{N}-{slug}
  branch:       mission-{N}-{slug}
```

The dash form (`mission-N-slug`) avoids colliding with `parse_session_name`'s split-on-first-`/`. Session, branch, and worktree are a 1:1:1 mapping.

## Lifecycle

1. **File an issue.** Body must include an `## Acceptance criteria` section with bullets. Apply the `agent-ready` label.
2. **Dispatcher tick** picks it up (work hours, eligibility = `agent-ready` AND criteria parsable AND issue OPEN).
3. **Worker spawns** in `{repo}-worktrees/mission-N-slug/` on branch `mission-N-slug`. Initial prompt = issue body + criteria + "open a draft PR titled `mission #N: ...`".
4. **Worker opens draft PR.** Worker idles between feedback rounds.
5. **Reviewer comments on PR.** On the next feedback-router tick (15m), the worker receives `/clear` + a refresh prompt pointing at a per-mission summary file with the new review bodies.
6. **Worker addresses feedback, pushes, idles again.** Repeat 4–6 until reviewer approves and PR is merged.
7. **Janitor reaps.** Once PR state is MERGED or CLOSED, the next janitor tick kills the worker session, removes the worktree, and clears local state.

## Config

### Global: `~/.agentwire/missions.yaml`

```yaml
global_concurrency: 3          # max simultaneous mission workers across all repos
work_hours_start: 9            # dispatcher only runs within [start, end)
work_hours_end: 18
default_max_iterations: 3      # reserved; not enforced at v1

repos:
  agentwire-dev:
    name: dotdevdotdev/agentwire-dev      # full owner/repo for gh
    projects_dir: ~/projects              # parent of `{short}-worktrees/`
    per_repo_concurrency: 1
```

### Per-project override: `.agentwire.yml`

A project can lower its `per_repo_concurrency` from the global default:

```yaml
missions:
  repo: agentwire-dev
  per_repo_concurrency: 2
```

Unknown repo shorts in the project override are silently ignored — the global config is the source of truth for which repos are mission-eligible.

## CLI

All commands print human-readable output by default; pass `--json` for structured output that the portal / MCP tools consume.

| Command | What it does |
|---|---|
| `agentwire mission list` | Active workers + eligible-but-unstarted issues per repo |
| `agentwire mission show N --repo SHORT` | One issue: body, criteria, dispatch status, PR link |
| `agentwire mission status` | Per-repo summary (active/cap, eligible queue depth) + last-tick heartbeats |
| `agentwire mission spawn N --repo SHORT` | Force-dispatch, **bypassing eligibility checks** (manual override) |
| `agentwire mission stall N --repo SHORT --reason "X"` | Remove `agent-ready`, add `stalled`, post comment |
| `agentwire mission resume N --repo SHORT` | Re-add `agent-ready`, remove `stalled` |
| `agentwire mission kill N --repo SHORT` | Kill worker session + worktree (does NOT close the PR) |
| `agentwire mission gc` | Run the janitor synchronously (reap closed PRs, sweep orphans) |
| `agentwire mission tick` | Run one dispatcher tick synchronously |
| `agentwire mission route-feedback` | Run the PR-feedback router synchronously |
| `agentwire mission init REPO` | Create the `agent-ready` label on a target repo (idempotent) |

### `kill` vs `gc`

`kill` and `gc` look similar but serve different purposes:

- **`kill`** is an **operator override**. Tears down the worker side (session + worktree) but does NOT touch the PR. Use when you want to take over manually or stop a runaway worker without losing the PR.
- **`gc`** is **PR-driven**. Only tears down sessions whose PR is already MERGED or CLOSED. Safe to run on a cron — never touches an active PR.

## MCP tools

All 8 mission tools are equally available from any agent session (no read/write tier). They shell out to the CLI; behavior matches:

| Tool | Read/Write | Calls |
|---|---|---|
| `mission_list()` | read | `agentwire mission list --json` |
| `mission_show(number, repo)` | read | `agentwire mission show N --repo R --json` |
| `mission_status()` | read | `agentwire mission status --json` |
| `mission_spawn(number, repo)` | write | `agentwire mission spawn N --repo R --json` |
| `mission_stall(number, repo, reason)` | write | `agentwire mission stall N --repo R --reason X` |
| `mission_resume(number, repo)` | write | `agentwire mission resume N --repo R` |
| `mission_kill(number, repo)` | write | `agentwire mission kill N --repo R` |
| `mission_gc()` | write | `agentwire mission gc` |

## Local state

State lives under `~/.agentwire/missions/`:

| Path | Content |
|---|---|
| `state/last_tick.json` | `{component: iso_timestamp}` heartbeats |
| `state/routed_reviews.json` | `{pr_number_str: last_routed_review_id}` — router idempotency |
| `summaries/{repo_short}/mission-N-slug.md` | Per-mission summary file the worker reads on `/clear` refresh |

State writes go through tempfile + `os.replace` so concurrent orchestrator runs can't tear each other's writes.

## Damage-control rules

When a tmux session is a mission worker (its name matches `{repo}/mission-{N}-{slug}`, carried through as `AGENTWIRE_SESSION_NAME`), two extra rules apply on top of the standard `damage-control` ruleset:

1. **Edit/Write** must target a path inside a `*-worktrees/mission-N-...` directory. No writes to the canonical repo, sibling projects, or arbitrary filesystem locations.
2. **Bash `git push --force` / `--force-with-lease`** is gated by branch name:
   - `main`/`master`/`develop` → always blocked
   - `mission-*` → allowed (the worker's own branch)
   - Anything else → blocked

These live in `agentwire/safety/_core.py` and are inlined into the generated hook scripts via `scripts/regen_damage_control_hooks.py`. The kill switch (`safety.enabled: false`) and the escape hatch (`# allow: <reason>`) both override mission-worker rules — same as for the standard ruleset.

## launchd setup

Templates live in `templates/launchd/`. Each plist has a comment block describing edits needed (paths to `agentwire` binary, your home directory). After substitution:

```bash
cp templates/launchd/dev.agentwire.mission-dispatcher.plist     ~/Library/LaunchAgents/
cp templates/launchd/dev.agentwire.mission-feedback-router.plist ~/Library/LaunchAgents/
cp templates/launchd/dev.agentwire.mission-janitor.plist        ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/dev.agentwire.mission-dispatcher.plist
launchctl load ~/Library/LaunchAgents/dev.agentwire.mission-feedback-router.plist
launchctl load ~/Library/LaunchAgents/dev.agentwire.mission-janitor.plist
```

Logs land in `~/Library/Logs/agentwire-mission-{dispatcher,feedback-router,janitor}.log`.

The janitor's `RunAtLoad: true` flag means it catches up immediately after the Mac wakes from sleep — useful for cleaning up worktrees if a long sleep means several scheduled ticks were missed.

To uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/dev.agentwire.mission-*.plist
rm ~/Library/LaunchAgents/dev.agentwire.mission-*.plist
```

## Keeping the installed CLI in sync

`uv tool install` caches builds. After editing any file under `agentwire/missions/`, `agentwire/safety/_core.py`, or the launchd plists, the installed `agentwire` binary still runs the **previous** code until you rebuild. The orchestrator launchd jobs invoke that installed binary directly — they will keep running stale code indefinitely.

```bash
agentwire rebuild   # reinstall from source so the installed CLI picks up changes
```

A symptom of forgetting this: you push a fix (e.g. branch cleanup in `gc.remove_worktree`), the next `mission gc` looks like it ran successfully, but the bug persists. Run `agentwire rebuild` between code change and any live test against the dispatcher / feedback-router / janitor.

The portal (Python web server) needs `agentwire portal restart --dev` for static-file changes; the launchd plists pick up the new binary on their next tick — no service restart needed.

## Your first mission

1. **One-time setup:**
   ```bash
   # Create the agent-ready label on your repo (idempotent)
   agentwire mission init my-repo

   # Edit ~/.agentwire/missions.yaml: register the repo with its full
   # owner/repo name and projects_dir.
   ```

2. **File an issue with criteria:**
   ```markdown
   ## Acceptance criteria
   - Endpoint /healthz returns 200
   - New test in tests/integration/test_healthz.py
   ```
   Apply the `agent-ready` label.

3. **Dispatch:**
   ```bash
   agentwire mission tick   # or wait for launchd
   ```

4. **Watch progress:**
   ```bash
   agentwire mission list
   agentwire mission show 42 --repo my-repo
   ```

5. **Review the worker's draft PR on GitHub.** Leave comments / request changes / approve as usual.

6. **Routed feedback:**
   ```bash
   agentwire mission route-feedback   # or wait for launchd
   ```
   The worker session receives `/clear` + a summary file pointer. It addresses the feedback and pushes.

7. **After PR is merged:**
   ```bash
   agentwire mission gc   # or wait for launchd
   ```
   Worker session killed, worktree removed.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `mission tick` skips an issue with "no Acceptance criteria" | Issue body missing the `## Acceptance criteria` header. Header is case-insensitive but must be H2 and use bullets (`-`, `*`, `+`). |
| Worker spawned but never opened a PR | Look at the worker session: `agentwire show {repo}/mission-N-slug` (or via portal). Common: git push failed because of branch protection — usually fine, just need the user to permit. |
| `mission gc` keeps complaining "no PR for branch yet" | Worker hasn't pushed/opened the PR. That's a skip, not a failure — gc only reaps when state is MERGED/CLOSED. |
| Feedback router routes the same review twice | Shouldn't happen — `state/routed_reviews.json` keys on PR number. If it does, that file may be corrupted; delete it and the router will re-route everything once. |
| Worker tried to edit a file outside its worktree | Damage-control blocked it. The block message points at which file. If the edit is legitimate (rare — usually it's not), use the `# allow:` escape hatch in a Bash command, or kill+respawn the worker with a wider config (don't disable safety system-wide). |
| Code change to `agentwire/missions/` doesn't take effect | The installed CLI is stale. Run `agentwire rebuild` — see [Keeping the installed CLI in sync](#keeping-the-installed-cli-in-sync). |

## Out of scope (v1)

These are future considerations, not promised:

- Cross-repo mission dependencies
- Mission templates / auto-issue-creation
- Reviewer assignment heuristics
- Mission analytics / time tracking
- Full Missions dashboard window with PR previews + worker output tail
- systemd-timer support for Linux/WSL (macOS only at v1)
- Real MCP tier system (currently all 8 mission tools are equally available)
- Label-driven `max_iterations` override per issue

## References

- Original design + plan: [issue #195](https://github.com/dotdevdotdev/agentwire-dev/issues/195)
- Naming / state / config / dispatcher / feedback_router source: `agentwire/missions/`
- CLI handlers: `agentwire/missions/cli.py`
- MCP tools: `agentwire/mcp_server.py` (search for `mission_`)
- Damage-control: `agentwire/safety/_core.py` (search for `_is_mission_worker_session`)
- Sidebar UI: `agentwire/static/js/sidebar/missions-section.js` + `/api/missions/*` in `server.py`
- launchd templates: `templates/launchd/dev.agentwire.mission-*.plist`

> Live-shakedown verified on 2026-05-19 (issue #199, PR #200) — first end-to-end mission run.
