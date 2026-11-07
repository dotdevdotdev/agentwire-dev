# Decoupling damage control (design spike, #1307)

> Design only. No behaviour changed by this page. Sub-issues are listed at the end and linked from #1307.

**Goal.** `damage-control-dev` (the engine) is a Claude Code hook library with its own repo. A user-scope hook in `~/.claude/settings.json` fires in *every* Claude Code session on the machine, workers included, whether or not agentwire spawned them. agentwire adds nothing for workers to be covered. Yet today agentwire requires the engine, imports it at startup, registers and repairs its hooks, ships a rules profile for it, and feeds it signals. After this work agentwire runs fully without it, and says plainly that it is not a guard.

## 1. Observations (what was actually run)

All experiments used scratch venvs and a scratch `HOME`; nothing under the real `~/.claude` or `~/.agentwire` was touched, and `claude` was never launched.

| # | Experiment | Result |
|---|---|---|
| E1 | Install this branch into a fresh venv, `uv pip uninstall damage-control-dev`, run `agentwire --help`, `doctor`, `list`, `safety status`, `hooks install`, … | **Every** command dies at import: `prompt_router.py:33` does `from damage_control.engine import detect_ask_request` and is imported by the CLI entry. `ModuleNotFoundError: No module named 'damage_control'`. agentwire is not runnable without the engine at all. |
| E2 | Same venv, but with an import-time stub that fabricates `damage_control.*` and logs each name imported or called (to see the *second* layer behind E1) | `--help`, `list`, `new`, `init`, `portal`, `scheduler`, `tasks`, `worktree`, `spawn`, `prompts` parse and run. `doctor` and `hooks install` fail at `safety_ext.build_profile()` (`damage_control.profile.AuditSpec` is *called* at import of `safety_ext`). `safety status` fails calling `engine.load_effective_config`. About 30 distinct engine names are imported at module load by the `safety_*` modules alone. |
| E3 | Engine-only venv (`damage-control-dev` installed, **agentwire not installed**, env with no `AGENTWIRE_*`), feed Claude Code PreToolUse payloads to `python -I -m damage_control.hook bash`, `permission_mode: bypassPermissions` | `rm -rf /` → exit 2, blocked. `git push --force origin main` → exit 2, blocked. `ls` → exit 0. The engine judges a payload with no agentwire involvement and does not honour bypass for hard blocks. It keeps its own state in `~/.damage-control/`. |

What E3 does **not** prove: that Claude Code actually applies a user-scope hook to a worker session agentwire spawned. That is Claude Code's documented behaviour and the premise of #1307, but observing it needs a real `claude` run, which the spike's boundaries forbid. Treat it as **unverified by observation** (the payload side is verified; the delivery side is not). The first sub-issue that depends on it (S5) must verify it in a disposable account before anything is deleted.

## 2. Coupling inventory

Grep plus E1/E2. Line numbers are against `6d4eab0b`. **Action**: *delete* (agentwire stops caring), *contract* (stays, as an engine-agnostic seam that works with nothing behind it), *move* (to the destination chosen in §3).

### 2.1 Imports of `damage_control` (the hard dependency)

| Where | What | Action |
|---|---|---|
| `pyproject.toml:27-32` | `damage-control-dev>=0.5.1,<0.6` required dep | delete (S9) |
| `prompt_router.py:33`, `:913-916` | `detect_ask_request` at **module level** → breaks every command (E1). Decorates the routed prompt with a `# ask:` reason | delete the decoration; show the tail of the command instead (S1) |
| `tasks_cli.py:49`, `:344` | `is_unattended` at module level | contract: agentwire owns the marker (S2) |
| `tasks.py:130,143,403` | `parse_unattended_allow` validates a task's `unattended_allow` | delete validation; field becomes an opaque passthrough (S2) |
| `scheduler/dispatch.py:549-574` | `encode_unattended_allow`; seeds `AGENTWIRE_UNATTENDED=1` + `_ALLOW` | contract: keep seeding the marker, pass grants through unparsed (S2) |
| `hooks_cli.py:288-296` | `MATCHERS` from `damage_control.claude_code` picks the outcome-logger matcher | move with the logger (S6) |
| `hooks_cli.py:808-852` | `engine_requires_human` / `hooks needs-human` (Guard.decide) | move to the engine (S5), then delete (S6) |
| `hooks/agentwire-permission.sh:35-48` | bypass/auto short-circuit asks `agentwire hooks needs-human`; failure ⇒ "hold for a human" | contract: see §5 (S6) |
| `doctor_cli.py:367-648` | `_report_delete_recursive`, `_repair_damage_control`, `_render_damage_control_section` (kill switch, drift, breakglass, packs, lint, residuals); imports at `:369,415,435,520,546,591` | replace by a generic guard report (S7) |
| `safety_ext.py` (250 l.), `safety_commands.py` (623), `safety_migrate.py` (278), `safety_notify.py` (389), `safety_packs_cli.py` (102), `safety_cli.py` (206) | the profile and the `agentwire safety …` CLI; import the engine at module level | move (S5) / delete (S6) |
| `safety/` (`audit`, `lint`, `provenance`, `residuals`, `suggest`, `rules/*.{yaml,json}`; `rule_baselines.json`) | rules layer, lint, install provenance, audit reader | move (S5) / delete (S6) |
| `__main__.py:126,189,211,395,418` | registers `safety`, special-cases `safety tooldefs` | delete (S6) |
| `mcp_status.py:108,119` and the `safety_*` MCP tools | `safety_packs_list`, `safety_groups_list` | delete (S6) |
| `onboarding.py:298,329-341` | claims bypass is safe "because hooks"; lists packs | rewrite (S8), delete packs (S6) |
| `system_cli.py:50,564`, `hooks_cli.py:76,461,502`, `doctor_cli.py:296,469` | provenance of the engine as an installed tool; `rebuild` refuses foreign engine source | delete engine half; agentwire's own provenance stays (S6) |

### 2.2 Env vars, files, signals the engine reads

| Contract | Where agentwire touches it | Action |
|---|---|---|
| `AGENTWIRE_UNATTENDED=1` (+ `_ALLOW`) | `core.py:260,290-312,1795,1806`; `scheduler/dispatch.py:564-574`; `tasks_cli.py:344` | contract. It means "no human is present"; any guard may read it. Document it as agentwire's public signal (S2). `core.py:1806` (unattended email-on-block shell snippet) reads it for non-engine reasons and stays |
| `AGENTWIRE_CREATED_BY` | `core.py:358` ("worker damage-control rules identify the session") | contract: stays, it is session identity; delete the engine-specific comment |
| `~/.agentwire/damagecontrol.yml`, project `.damagecontrol.yml`, `~/.agentwire/damage-control/*.yaml` | read by the engine; referenced in `config.py:304`, `routes/config.py:99`, `security.py:56`, `project_config.py:21,91`, doctor | delete references; files are the engine's (S6) |
| `~/.agentwire/damagecontrol-profile.json`, `damagecontrol-install.json`, `bin/damage-control-breakglass`, `~/.claude/hooks/*`, `settings.json` PreToolUse entries | written by `safety_ext.install` / `hooks install` / `doctor --fix` | move registration to the engine's own installer (S5), stop writing it here (S6) |
| Protected-path set naming `~/.agentwire/.env`, `portal.token`, `config.yaml`, `scheduler.yaml`, `machines.json`, `.agentwire.tasks.yml`; protected *command* `agentwire tasks promote` | `safety/rules/agentwire-control-plane.yaml` + engine `PROTECTED_*` | move (S5), see §3 |
| `~/.agentwire/logs/damage-control/*.jsonl`, `hooks/outcome_logger.py` | audit + outcome rows | move (S5/S6) |

### 2.3 Prompts, docs, posture

| Where | What | Action |
|---|---|---|
| `roles/{worker,worker-worktree,orchestrator,reviewer,reviewer-worktree}.md` (line 35-97) | teach `# ask: <reason>` | delete (S8); the grammar is the engine's, not agentwire's |
| `project_config.py:21-23` | `DEFAULT_POSTURE = "bypass"`, comment "damage-control hooks as the guard"; `session_cli.py:97,499,857`, `pane_cli.py:933`, `worktree_cli.py:144`, `mcp_pane.py:24`, `core.py:2186` repeat the default | posture decision, §4 (S3/S8) |
| `onboarding.py:298`, `SECURITY.md:60`, `CLAUDE.md`, `README.md`, `docs/wiki/{architecture,concepts,glossary,quickstart,…}`, skills `agentwire-{config,mcp-tools,project-config}` | "bypass is safe because the hooks guard it" | rewrite (S8) |
| `docs/wiki/internals/damage-control.md` (1657 l.), `docs/wiki/security/damage-control-hardening.md`, `…/internals/upgrade-test.md` | engine/profile documentation | move to the engine/profile repo (S5/S9) |

### 2.4 Tests, CI, release

| Where | What | Action |
|---|---|---|
| `tests/` | 50 files import the engine or `agentwire.safety*`; ≈47 of 191 unit files are engine/rules tests by name (`bypass`, `heredoc`, `corpus`, `policy_ladder`, `fail_closed`, `rules_json`, …) | engine-behaviour tests move with the code (S5); agentwire-only tests lose their engine imports (S1/S2) |
| `corpus/{cases,golden,schema.json}`, `scripts/{build_rules_json,effective_rules,gen_rule_baselines,golden_snapshot,mutate_scoped_allow}.py` | rule corpus and tooling | move (S5), delete here (S9) |
| `.github/workflows/security.yml` (whole), `pytest.yml:96-151` (scoped-allow mutation job, hook-under-system-python job, engine pip-install) | engine-only gates | move (S5), delete here (S9) |
| `RELEASING.md:7-24`; `tests/unit/test_damage_control_pin.py`; `pyproject.toml:148-160,260,276` | "raise the floor" release step, pin test, packaged rules, markers | delete (S9) |

## 3. Where agentwire-specific protections go

The protections are: the `~/.agentwire` control-plane/secret path set, the `agentwire tasks promote` command block, tmux/session rules, outbound `email`/`quo` verbs, MCP tool policies, and unattended grants.

| Option | For | Against |
|---|---|---|
| **A. damage-control-dev owns an optional `agentwire` profile** (`damage-control-dev init --profile agentwire`) | One repo, one release train; agentwire keeps *zero* guard code. The owner owns both repos | The engine now names agentwire. Rules can lag agentwire changes: add a new secret file in agentwire, forget the profile, and it is silently unprotected |
| **B. Separate small package** (`agentwire-guard`: profile + rules, depends on the engine) | Cleanest: neither core knows the other | A third thing to release and keep in sync; same lag problem; more places for a version floor to rot |
| **C. Nowhere** | Smallest | Loses real protections (secrets under `~/.agentwire`, the `tasks promote` block that exists because agents must not self-promote task code). These are not engine-generic; deleting them is a safety regression for anyone who does run a guard |

**Recommendation: A**, with agentwire publishing the path set as data so the lag failure has a mechanical check: `agentwire paths --json` (new, tiny, CLI-is-SSOT) lists secret/control paths; the profile installer reads it when present and a CI check in the profile repo compares. The `~/.agentwire` path knowledge itself moves to the profile; agentwire only *enumerates* it. If the owner prefers B the sub-issues are unchanged except S5's destination.

## 4. Default posture with no guard installed (owner decision)

Today every spawn defaults to `bypass` (`--dangerously-skip-permissions`) and the docs justify that by the hooks. After decoupling, agentwire cannot assume a guard exists.

| Option | Behaviour | Failure modes |
|---|---|---|
| **1. Keep `bypass`, say plainly agentwire is not a guard** | No code change; docs/onboarding/SECURITY.md rewritten to state it | A new user with no guard runs unrestricted agents; the only protection is whether they read the docs. Silent in normal use |
| **2. Default to `prompted`/`auto` unless a guard is detected** | Safe on a fresh machine; fast with a guard | Detection is a heuristic: "is there a PreToolUse hook on Bash?" cannot tell a working guard from a broken or fail-open one, so a false positive is *silently unsafe*; a false negative is a prompt storm on headless workers (a parked, un-answerable session; see prompt routing). Re-introduces coupling to Claude's settings layout. Guard removed after sessions spawn ⇒ stale decision |
| **3. Keep `bypass`, detect and report only** (`doctor`, `init`) | No behaviour change; absence of a guard is visible | Warnings get ignored; same exposure as 1 with a nag |

**Recommendation: 3, plus an explicit one-time consent in `agentwire init`** ("agentwire does not guard; sessions will run with `--dangerously-skip-permissions`. Continue / choose `auto`?") that records the answer as the config default. That keeps the posture model unchanged, never claims safety agentwire doesn't provide, and avoids option 2's silent-false-positive. Choose 2 only if the owner wants fail-safe-by-default for strangers and accepts the headless-prompt failure mode. **Not chosen here; S3 records the owner's ruling.**

## 5. The permission hook

`agentwire-permission.sh` auto-approves in bypass/auto except when the engine says a human must decide. With the engine gone the question belongs to the engine. Preferred end state: the engine ships its own `PermissionRequest` handling and agentwire's script just auto-approves in bypass/auto. **Unverified:** how Claude Code combines two `PermissionRequest` hooks (first wins? deny wins?). S5 must establish this by a disposable-account observation before S6 removes the `needs-human` call; until then the current fail-closed path stays.

## 6. Ordered sub-issues

Each is one PR; main is releasable after every one. Until S9 the dependency is still declared, so nothing breaks for installed users in between.

| # | Task | Done when |
|---|---|---|
| S1 (#1308) | **Engine-free import path** (+ no-engine CI smoke) | Defer/remove module-level engine imports reached by startup (`prompt_router` decoration deleted; `tasks_cli` import deferred); new CI job installs agentwire in a venv with the engine uninstalled and runs `agentwire --help`, `list`, `new --help`, `portal --help`, `tasks --help` green; `safety`/`doctor`/`hooks install` may still require the engine |
| S2 (#1309) | **Own the unattended contract** (done; [contract](unattended.md)) | `agentwire/unattended.py` owns marker + opaque grants passthrough; `tasks.py`/`dispatch.py`/`tasks_cli.py` have no engine import; `AGENTWIRE_UNATTENDED` documented as a public signal; scheduler tests green |
| S3 (#1310) | **Owner decision: default posture** (§4) | Owner's choice recorded in this page; S8 unblocked |
| S4 (#1311) | **Owner decision: protections destination** (§3) | A/B/C ratified in this page; S5 unblocked |
| S5 (#1312) | **Stand up the profile at its destination** (other repo) | Profile, rules, corpus, scripts, tests and `agentwire paths --json` consumer live there; its own CI green; `needs-human`/permission semantics verified in a disposable account (§5); issue in that repo cross-linked |
| S6 (#1313) | **Delete the safety surface from agentwire** | `agentwire safety`, `safety_*` MCP tools, `safety_*.py`, `safety/`, `hooks needs-human`, outcome logger, engine provenance removed; `hooks install` no longer registers an engine; no `damage_control` import remains in `agentwire/` (grep-checked in CI) |
| S7 (#1314) | **Generic guard report** | `doctor`/`init` report "PreToolUse guard registered: yes/no" without naming an engine; breakglass/kill-switch/drift checks removed |
| S8 (#1315) | **Posture + prompts + docs per S3** | Roles stop teaching `# ask:`; `onboarding.py`, `SECURITY.md`, `CLAUDE.md`, README, wiki and skills no longer claim the hooks make bypass safe; init consent implemented if option 3 |
| S9 (#1316) | **Drop the dependency and its machinery** | `damage-control-dev` out of `pyproject.toml`/`uv.lock`; `corpus/`, engine scripts, `security.yml`, `pytest.yml` engine jobs, pin test, `RELEASING.md` engine section, packaged rules removed; full suite + no-engine smoke green; this page marked done |
