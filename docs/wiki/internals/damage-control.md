# Damage Control: Security Firewall for AgentWire

> Living document. Update this, don't create new versions.

---

## Overview

Damage Control is a security firewall system that protects AgentWire from dangerous operations during parallel agent execution. It intercepts tool calls (Bash, Edit, Write) via PreToolUse hooks and blocks operations matching security patterns.

**Why Critical for AgentWire**: Parallel remote agent execution multiplies risk. A single `rm -rf /` in a remote session is unrecoverable. Multi-agent execution amplifies the chance of catastrophic mistakes.

### Protection Layers

| Layer | Coverage |
|-------|----------|
| **Bash Tool** | Commands: `rm -rf`, `git push --force`, `systemctl stop`, database drops |
| **Edit Tool** | File protections: SSH keys, credentials, `.env` files, system configs |
| **Write Tool** | Same as Edit tool (creation protection) |
| **Audit Logging** | All security decisions logged for analysis and debugging |

---

## Architecture

```
AgentWire Session
    ↓
Tool Call (Bash/Edit/Write)
    ↓
PreToolUse Hook
    ↓
Damage Control Hook Script (Python/UV)
    ↓
rules/*.yaml → Check command/path
    ↓
Decision: Block (exit 2) | Allow (exit 0) | Ask (JSON response)
    ↓
[If blocked] Error message to agent
[If allowed] Command executes
[If ask] User prompt for confirmation
```

### File Structure

Hooks ship inside the `agentwire` package — Claude Code's `settings.json` invokes them directly via `uv run`:

```
agentwire/hooks/damage-control/       # Bundled in package
├── bash-tool-damage-control.py       # Bash tool hook
├── edit-tool-damage-control.py       # Edit tool hook
├── write-tool-damage-control.py      # Write tool hook
├── mcp-tool-damage-control.py        # Outbound MCP tool hook (email_send/quo_send)
├── audit_logger.py                   # Audit logging framework
└── rules/                            # Pattern files (categorized)
    ├── core.yaml                     # rm, chmod, system-level dangers
    ├── git.yaml                      # force push, reset --hard
    ├── databases.yaml                # DROP, TRUNCATE, migrations, raw DML
    ├── containers.yaml               # docker prune/push, kubectl delete
    ├── cloud-hosting.yaml, aws.yaml, gcp.yaml, firebase.yaml  # incl. deploys
    ├── infrastructure.yaml, remote.yaml
    ├── outbound.yaml                 # email/SMS send verbs (ask)
    ├── publish.yaml                  # package-registry publish (ask)
    ├── agentwire.yaml                # tmux/session protections
    └── gws.yaml                      # Google Workspace CLI

~/.agentwire/
├── damage-control/                   # OPTIONAL user override — same shape as rules/
│   └── *.yaml                        # If present, replaces bundled rules wholesale
└── logs/
    └── damage-control/
        └── YYYY-MM-DD.jsonl          # Daily audit logs (audit_logger.py)
```

Hooks load every `*.yaml` file in the rules directory and merge their pattern lists.

---

## Policy files & the protected control plane (#466)

Damage control reads its kill switch, its rules, and its own hook registration
from files on disk. If the policed agent can write those files, it can disable
safety, act, and re-enable — defeating everything. So the **knobs live in
dedicated, host-owned files** and the **whole control plane is hard-protected**.

### Policy files (the only place the knobs live)

| File | Scope |
|------|-------|
| `~/.agentwire/damagecontrol.yml` | Global `enabled` / `disabled_rules` / `unattended_allow` (+ `allowed_paths`) |
| `<repo>/.damagecontrol.yml` | Per-project override (nearest, walking up from cwd). May both **loosen and tighten** — it wins on `enabled`; rule knobs + `allowed_paths` merge |

```yaml
# ~/.agentwire/damagecontrol.yml  (or <repo>/.damagecontrol.yml)
enabled: true            # master switch; missing file/key ⇒ true (fail-secure)
disabled_rules: []       # stable rule IDs to disable
unattended_allow: []     # extra rule IDs an unattended run may resolve ask→allow
allowed_paths: []        # per-project allowlist (see allowedPaths below)
```

**ALL** damage-control policy — kill switch, rule knobs, AND the per-project
`allowed_paths` allowlist — lives in these files. They **no longer live in
`config.yaml` or `.agentwire.yml`** at all (relocated out entirely;
`load_safety_config` / `_find_project_config` read only the files above). The
allowlist had to move too (#467): it's the one knob that overrides the protected
check, so leaving it in the agent-writable `.agentwire.yml` would let an agent
allowlist a control-plane path and re-permit its own write. `agentwire safety
install` scaffolds the global file with `enabled: true` if missing.

### The protected control plane (escape-hatch- AND kill-switch-exempt)

Any write / edit / delete / move / chmod targeting one of these paths is
**BLOCKED**, and — unlike every other rule — the `# allow:` escape hatch and the
`enabled: false` kill switch **do NOT override it**:

- `~/.agentwire/damagecontrol.yml`, any `.damagecontrol.yml`
- `~/.claude/settings.json` (the PreToolUse hook registration)
- `~/.agentwire/hooks/damage-control/*.py` (the hook scripts), `~/.claude/hooks/*`
- `~/.agentwire/damage-control/*.yaml` (the rule files)
- `~/.agentwire/scheduler.yaml`, `~/.agentwire/config.yaml` (gate/healthcheck commands run via the same `shell=True` confused-deputy path)
- any `.agentwire.tasks.yml` (per-project task-execution config — see [Task-execution config split](#task-execution-config-split-agentwiretasksyml-720) below; **not** `.agentwire.yml`, which is pure declarative session config and is agent-writable)

The guarantee: **the agent only ever operates within the freedom the human
preset; it can never expand its own freedom by editing a file.** The one
override is the user's `allowedPaths` allowlist — a human, host-side opt-in (the
agent can't add to it without editing a protected file, which is itself blocked).
The mechanism is `check_protected_command` / `check_protected_path` in
`safety/_core.py`, which run **before** `detect_escape_hatch` and the kill switch.

The same escape-hatch-/kill-switch-exempt tier also covers specific COMMANDS,
not just paths — `PROTECTED_COMMAND_PATTERNS` (`check_protected_bash_command`,
also checked before `detect_escape_hatch`) for operations that are dangerous
regardless of which file they touch. See [Task-execution config
split](#task-execution-config-split-agentwiretasksyml-720) below for the first
entry (`agentwire tasks promote`).

### Task-execution config split (`.agentwire.tasks.yml`, #720)

`.agentwire.yml` used to carry BOTH declarative session config (`type`/`roles`/
`voice`/`parent`/`worktree`) AND the `tasks:` block (`pre`/`post`/`on_task_end`/
`shell` — code the scheduler runs via `shell=True`). Protecting the whole file
to guard the second category also blocked agents from authoring the first, the
common/safe case. The fix: split them.

- **`.agentwire.yml`** — purely declarative, zero execution vector, agent-writable again.
- **`.agentwire.tasks.yml`** — the `tasks:` block, protected control-plane (same tier as `.damagecontrol.yml`).

Since a policed agent can't write the protected file directly, authoring it is
**propose-and-promote** (mirrors the worktree → PR → review → merge model,
because task defs ARE executable code):

1. The agent drafts to the **unprotected** staging file `.agentwire.tasks.proposed.yml`.
2. A human runs `agentwire tasks review [session]` — a diff against the live
   file plus every shell-bearing field the draft would run (that's the
   review's whole purpose), and any validation issues.
3. The human runs `agentwire tasks promote [session] [--yes]` — agentwire
   itself (host-trusted) copies the vetted draft into the live
   `.agentwire.tasks.yml` and deletes the draft. The agent never writes the
   live file.

Both commands are deliberately **CLI-only, never MCP**: an MCP tool that
shelled out to `promote` would bypass the Bash-tool hook entirely (see
[Outbound MCP tool gating](#outbound-mcp-tool-gating-457) — everything not on
that explicit gated list is open by default).

`agentwire tasks promote` self-invocation is **hard-gated**, not just
defense-in-depth (#721 review found three bypasses of the original
Bash-pattern-only block; all three are closed):

1. **`PROTECTED_COMMAND_PATTERNS`** (`safety/_core.py`) blocks the command
   text at the SAME tier as the protected-control-plane path check —
   escape-hatch- and kill-switch-EXEMPT, evaluated before
   `detect_escape_hatch`. Unlike an ordinary `bashToolPatterns` rule, `# allow:`
   and `enabled: false` cannot reopen it, and there's no allowlist override at
   all (there's no legitimate reason for an agent to ever run this command).
2. **`cmd_tasks_promote` itself refuses to run** outside a genuine host
   context, regardless of how it's reached — this is the layer that actually
   matters, since a raw `python3 -c "from agentwire.tasks_cli import
   cmd_tasks_promote; ..."` never matches any bash pattern at all (no
   protected-path string, no command text), so it reaches the function
   directly. It:
   - Refuses unconditionally when `AGENTWIRE_UNATTENDED=1` (the marker the
     scheduler stamps on every headless dispatch) — closing the concrete
     exploit: an unattended task writes malicious `shell:` strings to the
     proposed file, then self-promotes so the scheduler executes them
     unguarded on the next tick.
   - Otherwise requires a genuine host signal: a real interactive tty (Claude
     Code's Bash tool never attaches one, attended or not), or the explicit
     `AGENTWIRE_ALLOW_TASKS_PROMOTE=1` opt-in for a human's own
     non-interactive script. **`--yes` only skips the confirmation prompt —
     it never substitutes for this gate.**

`rules/control-plane.yaml` lists the protected PATHS as `readOnlyPaths` for
defense-in-depth + visibility, but the code-level checks above are what make
the protection absolute.

**Deferred / follow-up — police-at-execution.** The deeper root fix is routing
agentwire's own task/gate/healthcheck `subprocess.run(shell=True)` calls
through the damage-control policy engine in-process (with the existing
unattended fail-closed guardrail). That would mean an agent-authored command
gains no unguarded exec even if it somehow lands in the file, and the file
wouldn't strictly need protecting at all. Split-the-file (above) guards
*authorship*; police-at-execution guards *the real risk*. Recommended as
defense-in-depth on top of the split, not implemented here.

---

## Security Patterns

Patterns live in **categorized YAML files** under `agentwire/hooks/damage-control/rules/` (14 files, one per topic). To override or extend, drop YAML files into `~/.agentwire/damage-control/` — when that directory exists with `*.yaml` files, hooks load from there instead of the bundled rules.

### Pattern Types

#### 1. bashToolPatterns (Bash commands)

Block dangerous shell commands using regex patterns:

```yaml
bashToolPatterns:
  - pattern: '\brm\s+(-[^\s]*)*-[rRf]'
    reason: rm with recursive or force flags

  - pattern: '\bgit\s+push\s+--force\b'
    reason: git push --force (use --force-with-lease)

  - pattern: '\bsystemctl\s+stop\b'
    reason: stopping system services
```

**Coverage**:
- Destructive file operations (`rm -rf`, `shred`, `truncate`)
- Permission changes (`chmod 777`, `chown root`)
- Git destructive operations (`reset --hard`, `push --force`)
- Database operations (`DROP DATABASE`, `TRUNCATE`)
- System operations (`shutdown`, `reboot`, `systemctl stop`)
- Docker destructive operations (`system prune`, `rm -v /`)
- Package manager risks (`apt-get autoremove`, `npm uninstall -g`)

#### 2. zeroAccessPaths (Complete blocks)

Paths that cannot be accessed at all (read, write, edit, delete):

```yaml
zeroAccessPaths:
  - ~/.ssh/id_rsa
  - ~/.ssh/id_ed25519
  - ~/.agentwire/credentials/
  - ~/.agentwire/api-keys/
  - "*.pem"
  - "*.key"
  - ".env*"
```

Supports:
- Literal paths: `~/.ssh/id_rsa`
- Directory prefixes: `~/.agentwire/credentials/`
- Glob patterns: `*.pem`, `.env*`

#### 3. readOnlyPaths (No modifications)

Paths that can be read but not modified:

```yaml
readOnlyPaths:
  - ~/.agentwire/damage-control/
  - ~/.gitconfig
  - /etc/hosts
```

Blocks: write, append, edit, move, copy, delete, chmod, truncate

#### 4. noDeletePaths (Deletion protection)

Paths that can be modified but not deleted:

```yaml
noDeletePaths:
  - ~/.agentwire/sessions/
```

Blocks: `rm`, `unlink`, `rmdir`, `shred`

#### 5. allowedPaths (Granular path-based allowlist)

Paths where path-based protections (zeroAccess, readOnly, noDelete) are bypassed. Each entry specifies which operations are permitted. Hard-blocked bash patterns (like `rm -rf`) are **NEVER** bypassed. Bypassable bash patterns (like plain `rm`) can be overridden if the target path has the required operation permission.

**Operations**: `all`, `read`, `write`, `edit`, `delete`, `move`, `chmod`

**Global** (in any rules YAML — bundled or override):
```yaml
allowedPaths:
  - path: "*/dist/*"
    allow: all                     # bypass everything including bypassable rm
  - path: "~/.agentwire/.env"
    allow: [read, write, edit]     # but NOT delete
  - path: "*/__pycache__/*"
    allow: all
```

**Per-project** (top-level `allowed_paths` in the **protected** `.damagecontrol.yml` at the repo root — NOT `.agentwire.yml`, see [Policy files](#policy-files--the-protected-control-plane-466) and #467):
```yaml
# <repo>/.damagecontrol.yml
allowed_paths:
  - path: ".env.development"
    allow: [read, write, edit]
  - path: "dist/*"
    allow: all
```

The allowlist is the one knob that overrides the protected-control-plane check, so it lives behind that same protection — an agent can't edit `.damagecontrol.yml` to widen its own freedom.

Per-project paths are relative to the project root and resolved to absolute paths before matching.

**Bypassable bash patterns**: Some bash patterns (plain `rm`, `rmdir`, `trash`) are marked `bypassable: true` in their rules YAML. When a command matches a bypassable pattern, the system checks if ALL target paths have the required operation permission (e.g., `delete` for `rm`). If all paths match, the command is allowed. Hard-blocked patterns (like `rm -rf`) are never bypassed regardless of permissions.

**Security**: When checking bypassable patterns, ALL paths in the command must have the required permission. A command like `rm /tmp/safe.txt /etc/passwd` is blocked because `/etc/passwd` is not in the allowlist, even though `/tmp/` has delete permission.

**Precedence**:
1. Hard-blocked `bashToolPatterns` (no `bypassable` flag) — always blocked, NEVER bypassed
2. Ask patterns (`ask: true`) — prompt for confirmation when a human is present; **fail closed when unattended** (see below)
3. Bypassable `bashToolPatterns` (`bypassable: true`) — check allowlist for required operation
4. `allowedPaths` (global + per-project merged) — if target matches with correct operation, skip path checks
5. `zeroAccessPaths` — block (unless allowlisted with `read`)
6. `readOnlyPaths` — block modifications (unless allowlisted with specific operation)
7. `noDeletePaths` — block deletions (unless allowlisted with `delete`)

---

## Unattended (no-human-present) guardrail

The `ask` tier only means something when a human is there to confirm. The
scheduler dispatches agents headless (cron, nobody watching) with
`--dangerously-skip-permissions`, so historically an `ask`-tier command
resolved to a **silent allow** — an unsupervised agent could deploy, drop a
table, or delete a remote branch with no one seeing it until after the fact.

When a session is marked **unattended**, the bash hook resolves `ask` by
**failing closed**: it **blocks** the command and **emails the owner**, unless
the matched rule's stable ID is on the unattended allowlist.

**How a session is marked unattended.** The scheduler is the single chokepoint:
on every headless dispatch it seeds `AGENTWIRE_UNATTENDED=1` (and any per-task
`AGENTWIRE_UNATTENDED_ALLOW`) into the dispatch subprocess environment
(`scheduler._unattended_env`). Session creation funnels that marker into the new
tmux session via `tmux new-session -e K=V` (`__main__._with_unattended_env`), so
it lands before the agent launches and the hook can read it. Interactive
sessions never pass through that chokepoint, so the marker can't leak into a
human's session — even though interactive sessions use the same
`--dangerously-skip-permissions` posture. A child session an unattended agent
spawns inherits the marker (defense in depth).

**What's unaffected.** Hard `block` rules (`rm -rf`, `git push --force`, DB
drops) fire regardless — they never depended on a human. Interactive `bypass`
sessions resolve `ask` exactly as before. The kill switch still wins: with
`enabled: false` in `~/.agentwire/damagecontrol.yml`, nothing is checked, so the
unattended gate is inert too (enable safety for scheduled projects to engage it).

**The allowlist** (union of three sources):

| Source | Where | Scope |
|--------|-------|-------|
| `DEFAULT_UNATTENDED_ALLOW` | `safety/_core.py` | Built-in: `git.add`, `git.add-u`, `git.commit`, `git.push`, `gh.pr-create`, `outbound.agentwire-email` — work + open a PR + notify the owner by email |
| `unattended_allow` | `~/.agentwire/damagecontrol.yml` / project `.damagecontrol.yml` | Global / per-project extension (list of rule ids) |
| `unattended_allow` | per-task in `.agentwire.tasks.yml` | Per-task extension — the pressure-relief valve: widen for one task instead of loosening the global default |

Allowlisting is **by rule ID**, not command text — so `git.push` (plain push)
is allowed while `git push --force` (hard block) and `git push --delete`
(distinct `ask` rule `git.deletes-remote-branch`) are not. Tooldef commands the
allowlist references carry an explicit `id:` so the ID is stable across
description edits.

When a command is blocked, the owner email and `agentwire safety logs` name the
exact rule id, so widening is copy-paste: add that id to the task's
`unattended_allow`.

**`agentwire email` is a blanket unattended-allow, by design (#804).** Emailing
the owner is the *primary* way an unattended agent reports back — fail-closed
blocking it defeats the use case (a scheduled review silently never reaches the
owner). `outbound.agentwire-email` is on `DEFAULT_UNATTENDED_ALLOW`
unconditionally: **any** `--to`, not just the owner's own address. A narrower
owner-address-only exemption was considered and rejected — the owner explicitly
accepted the exfil tradeoff in favor of the simpler blanket allow. `agentwire
quo` (SMS) is unaffected and still fails closed unattended; widen it per-task
via `unattended_allow` (`outbound.agentwire-quo`) same as any other verb. This
applies identically to the Bash shell-out and the `email_send` MCP tool (both
resolve through the same `resolve_unattended_allow`).

```yaml
# project .agentwire.tasks.yml — let ONE scheduled task run terraform apply unattended
tasks:
  infra-drift:
    prompt: reconcile infra drift and apply
    unattended_allow:
      - tooldef.terraform-apply-planned-changes-to-infrastructure
```

> **Coverage note:** the guardrail makes the `ask` tier fail closed. A
> destructive command that isn't classified as `ask`/`block` at all is
> unaffected and would sail through unattended. The moment such a verb is
> classified `ask`, this guardrail blocks it unattended for free. The matrix
> below (the #428 audit) is the record of which high-impact verbs are covered.

### Unattended verb-coverage matrix (#428)

The guardrail is only as strong as the tier assignments for the verbs we most
want stopped headless. Two mechanisms classify a verb as `ask`:

- **rule** — an `ask: true` `bashToolPattern` in `rules/*.yaml`
- **tooldef** — an `access: write` command in `tooldefs/*.yaml`, auto-promoted
  to an `ask` pattern at load time

Both land in the same `ask` tier, so both are caught unattended. `ask` resolves
per session mode: interactive **bypass/auto** → allow (no friction, the common
agentwire posture); interactive **non-bypass** → confirm prompt; **unattended**
→ block + email owner (unless the rule id is allowlisted). Genuinely
catastrophic, never-reversible verbs are `block` (fire in every mode).

| Verb class | Representative commands | Tier | Where |
|---|---|---|---|
| **Deploy — hosting** | `vercel deploy` / `--prod`, `netlify deploy`, `fly deploy`, `wrangler deploy`/`publish`, `railway up`, `render deploys create`, `supabase functions deploy` | ask | `cloud-hosting.yaml` (`deploy.*`) |
| **Deploy — cloud** | `gcloud run deploy`, `gcloud app deploy` | ask | `gcp.yaml` (`deploy.gcloud-*`) |
| | `gcloud functions deploy` | ask | gcp tooldef |
| | `aws cloudformation deploy`, `aws lambda update-function-code`, `aws ecs update-service` | ask | `aws.yaml` (`deploy.aws-*`) |
| **Deploy — IaC** | `terraform apply` | ask | terraform tooldef |
| | `pulumi up`, `serverless`/`sls deploy`, `sam deploy`, `cdk deploy`, `ansible-playbook` | ask | `infrastructure.yaml` (`deploy.*`) |
| **Deploy — containers** | `kubectl apply` | ask | kubectl tooldef |
| | `docker push`, `docker compose push` | ask | `containers.yaml` (`container.docker-push`) |
| **Deploy — CI/release** | `gh release create`, `gh workflow run`, `gh pr merge` | ask | gh tooldef |
| **Outbound comms** | `agentwire email` | ask (unattended-allowed by default, #804) | `outbound.yaml` (`outbound.agentwire-email`) |
| | `agentwire quo`, `twilio … messages create`, `aws ses send-email`, `aws sns publish`, `sendmail`, `mail -s` | ask | `outbound.yaml` (`outbound.*`) |
| **DB migrations** | `prisma migrate deploy`/`dev`, `prisma db push`, `supabase db push`, `supabase migration up`, `alembic upgrade`/`downgrade`, `manage.py migrate`, `rails`/`rake db:migrate`, `knex migrate:*`, `sequelize db:migrate`, `flyway migrate`, `liquibase update` | ask | `databases.yaml` (`db.*`) |
| **DB raw writes** | `psql`/`mysql` executing INSERT/UPDATE/ALTER/CREATE/GRANT, `mongosh` insert/update/delete | ask | `databases.yaml` (`db.psql-write`, `db.mysql-write`, `db.mongosh-write`) |
| **DB schema-drop** | `prisma migrate reset`, `flyway clean` | **block** | `databases.yaml` (`db.prisma-reset`, `db.flyway-clean`) |
| **Package publish** | `npm publish`, `uv publish` | ask | npm/uv tooldef |
| | `cargo`/`poetry`/`pnpm`/`yarn publish`, `twine upload`, `gem push`, `mvn deploy` | ask | `publish.yaml` (`publish.*`) |
| **Destroy / drop** | `vercel remove`, `gh repo delete`, `terraform destroy`, `DROP DATABASE`, `aws … delete-*`, `git push --force`, `rm -rf` | **block** | various |

**Allowlisting a covered verb for one task** — the block message and owner email
name the exact rule id, so widening is copy-paste into the task's
`unattended_allow` (e.g. `deploy.vercel`, `outbound.agentwire-quo`,
`db.prisma-migrate`). `outbound.agentwire-email` doesn't need this — it's
already on `DEFAULT_UNATTENDED_ALLOW`.

**Residual gaps (intentional / known):**

- **MCP send paths bypass the hook.** Agents in agentwire sessions usually send
  via MCP tools (`email_send`, `quo_send`), which are *not* Bash/Edit/Write and
  so never reach this hook. The `outbound.*` rules only catch a shell-out to the
  CLI. Closing the MCP path needs a guard at the MCP layer, not a rule — out of
  scope here.
- **File-fed SQL can't be introspected.** `psql -f migration.sql` /
  `mysql < dump.sql` carry their statements in a file the regex can't read, so
  they stay `allow` unless the file path trips a path rule. Catastrophic inline
  statements (`DROP`/`TRUNCATE`/`DELETE`-without-`WHERE`) are still blocked by
  the client-agnostic SQL patterns.
- **Bare implicit-deploy invocations.** `vercel` with no subcommand deploys to
  preview; matching a bare binary name would false-positive on every read
  subcommand, so only the explicit `vercel deploy` / `--prod` forms are gated.
- **Text matching is conservative.** A literal mention inside `echo`/a comment
  can trip an `ask` (errs safe). This is the same tradeoff every existing rule
  carries (`DROP DATABASE` in an `echo` also blocks).

---

## Outbound MCP tool gating (#457)

Agents inside agentwire sessions reach external comms through **MCP tools**, not
the Bash tool — `email_send` (external email via Resend) and `quo_send`
(external SMS via Quo/OpenPhone). PreToolUse fires for MCP tools too, so a fourth
hook gates them:

- **`mcp-tool-damage-control.py`** registered with matcher
  `mcp__agentwire__(email_send|quo_send)`.
- On fire it **synthesizes the equivalent shell command** the tool runs under the
  hood (`email_send` → `agentwire email --to … --subject …`; `quo_send` →
  `agentwire quo --to …`; the message body is omitted from the synthesized,
  audit-logged command) and runs it through the **identical** decision ladder
  (`check_command` + `is_unattended` + `resolve_unattended_allow`) as the Bash
  hook. That reuses `outbound.agentwire-email` / `outbound.agentwire-quo`
  verbatim — same rule IDs, same `unattended_allow`, same
  `agentwire safety notify-unattended-block` owner-alert on an unattended block.
- Generated from `agentwire/safety/_core.py` via
  `scripts/regen_damage_control_hooks.py` like the other three — never hand-edit
  between the GENERATED markers.

Effect: an unattended scheduler dispatch can no longer send SMS silently (email
is a deliberate exception — see the blanket-unattended-allow discussion above,
#804), and an attended session now gets a real `ask` prompt instead of zero
friction.

### MCP surface audit — what is gated vs left open

Only verbs that are **outward-facing AND irreversible** (reach real people, can't
be un-done) warrant gating, matching the `outbound.*` scope. The rest of the
`mcp__agentwire__*` surface was reviewed and intentionally left open:

| MCP tool(s) | Decision | Why |
|---|---|---|
| `email_send`, `quo_send` | **Gated** | External email/SMS to real people — irreversible. |
| `say`, `notify_user`, `notify_parent`, `notify_event`, `msg_send`, `session_send` | Open | Internal to the agentwire network / local desktop; not external, reversible. |
| `session_create`/`recreate`/`fork`/`kill`, `pane_*` | Open | Local tmux lifecycle; reversible, no external reach. |
| `machine_add`/`machine_remove` | Open | Local registry edit; reversible. |
| `scheduler_run`, `scheduler_enable`/`disable` | Open | Triggers local task runs (themselves gated by this hook + the Bash hook). |
| `council_start`/`stop` | Open | Local orchestration sessions. |
| `desktop_*`, `worktree_*`, `handoff_*`, `history_*` | Open | Local UI / git-backed / filesystem; reversible. |

If a new outward-irreversible MCP verb is added, extend
`DAMAGE_CONTROL_MATCHERS` (matcher) + `_synthesize_command` (in the hook) and add
the matching `outbound.*`/`publish.*` rule — don't invent a tool→tier table.

## AgentWire-Specific Protections

### Tmux Session Protection

```yaml
bashToolPatterns:
  - pattern: '\btmux\s+kill-server\b'
    reason: tmux kill-server (kills all sessions)

  - pattern: '\btmux\s+kill-session\s+-t\s+agentwire-'
    reason: killing AgentWire tmux sessions
```

Protects:
- `tmux kill-server` - would kill all sessions
- `tmux kill-session -t agentwire-*` - would kill AgentWire workers
- Allows: `tmux list-sessions`, `tmux attach`, killing non-AgentWire sessions

### Session File Protection

```yaml
zeroAccessPaths:
  - ~/.agentwire/credentials/
  - ~/.agentwire/api-keys/
  - ~/.agentwire/secrets/

noDeletePaths:
  - ~/.agentwire/sessions/
```

Protects:
- Credentials and API keys from any access
- Session state from deletion

### Remote Execution Safeguards

```yaml
bashToolPatterns:
  - pattern: '\bssh\s+[^\s]+\s+.*\brm\s+-[rf]'
    reason: dangerous remote rm command

  - pattern: '\bssh\s+[^\s]+\s+.*\bDROP\s+DATABASE\b'
    reason: remote database drop

  - pattern: '\bssh\s+[^\s]+\s+.*\bsystemctl\s+stop\b'
    reason: remote service shutdown
```

Protects against:
- Remote file deletions via SSH
- Remote database drops
- Remote service shutdowns
- Remote Docker prune operations

---

## Usage

### Testing Commands

Test commands before running them using the CLI:

```bash
# Test if command would be blocked
agentwire safety check "rm -rf /tmp"
# → ✗ Decision: BLOCK (rm with recursive or force flags)

# Test if command would be allowed
agentwire safety check "ls -la"
# → ✓ Decision: ALLOW

# Check overall safety status
agentwire safety status
# → Shows pattern counts, recent blocks, audit log location
```

### Querying Audit Logs

View security decisions from audit logs:

```bash
# Show recent blocked operations
agentwire safety logs --tail 20

# Show today's operations
agentwire safety logs --today

# Show blocks for specific session
agentwire safety logs --session agentwire-dev/auth-refactor

# Search for specific pattern
agentwire safety logs --pattern "rm -rf"
```

**Audit Log Format**:
```json
{
  "timestamp": "2026-04-30T13:45:22Z",
  "session_id": "agentwire-dev/damage-control",
  "agent_id": "wave-2-task-1",
  "tool": "Bash",
  "command": "rm -rf /tmp/test",
  "decision": "blocked",
  "blocked_by": "bashToolPattern: rm with recursive flags",
  "pattern_matched": "\\brm\\s+-[rRf]"
}
```

---

## Customizing Patterns

### Adding New Patterns

Drop a YAML file into `~/.agentwire/damage-control/` (creates the user-override layer):

```yaml
# ~/.agentwire/damage-control/myapp.yaml
bashToolPatterns:
  - pattern: '\bmyapp\s+destroy\b'
    reason: myapp destroy command is dangerous

zeroAccessPaths:
  - /myapp/secrets/

readOnlyPaths:
  - /myapp/config/production.yaml
```

**Heads-up:** the user-override directory **replaces** the bundled rules wholesale — copy what you need from `agentwire/hooks/damage-control/rules/` if you want to extend rather than override.

**Pattern Tips**:
- Use `\b` for word boundaries: `\brm\b` matches `rm` but not `format`
- Use `\s+` for required whitespace: `git\s+push` matches `git push`
- Test patterns before deploying: `agentwire safety check "command"`
- Patterns are case-insensitive for Bash commands

### Temporarily Disabling Protection

**Option 1**: Comment out specific patterns in your override `*.yaml`:

```yaml
# Temporarily disabled for migration
# - pattern: '\bgit\s+push\s+--force\b'
#   reason: git push --force
```

**Option 2**: Remove the hook entry from Claude Code's `~/.claude/settings.json` (the file Claude Code reads, not `~/.agentwire/settings.json`).

**Warning**: Disabling protection removes safety nets. Re-enable as soon as the risky operation is complete.

---

## Troubleshooting

### Hook Not Blocking Expected Command

**Check using CLI**:
```bash
# Test the command
agentwire safety check "your command here"

# Check hook status
agentwire hooks status
```

**Verify hook is registered**:
```bash
cat ~/.claude/settings.json | grep damage-control
```

### False Positive (Safe Command Blocked)

**Identify the pattern**:
```bash
agentwire safety check "your command here"
# Shows which pattern matched
```

**Adjust the pattern** — copy the relevant rules file from `agentwire/hooks/damage-control/rules/` into `~/.agentwire/damage-control/` and edit there:
```yaml
# Before (too broad)
- pattern: '\brm\b'

# After (more specific)
- pattern: '\brm\s+(-[^\s]*)*-[rRf]'
```

### Hook Timeout

Hooks have a 5-second timeout. If your rule files are very large or patterns are complex, you may hit it.

**Solution**: Optimize regex patterns
```yaml
# Slow (backtracking)
- pattern: '.*rm.*-rf.*'

# Fast (specific)
- pattern: '\brm\s+.*-[rf]'
```

### Audit Logs Growing Too Large

Audit logs are stored in `~/.agentwire/logs/damage-control/`.

**Implement log rotation** (future enhancement):
```bash
# Manual cleanup (keep last 30 days)
find ~/.agentwire/logs/damage-control/ -name "*.jsonl" -mtime +30 -delete
```

---

## Testing

### Manual Testing

Test with real AgentWire session:

```bash
# Create AgentWire session
agentwire new -s test-session

# In session, try dangerous commands
rm -rf /tmp/test           # Should be blocked
tmux kill-server           # Should be blocked
ls -la                     # Should be allowed

# Check audit logs
agentwire safety logs --session test-session
```

---

## Performance

### Hook Overhead

Each tool call adds <100ms overhead for pattern checking:
- Load `rules/*.yaml`: ~10ms (cached after first load)
- Pattern matching: ~50ms for 300+ patterns
- Audit logging: ~10ms

**Total**: ~70-100ms per command

### Optimization Tips

1. **Pattern order**: Put most common patterns first
2. **Specific patterns**: Avoid `.*` wildcards that cause backtracking
3. **Compiled patterns**: Python's `re` module caches compiled patterns
4. **Audit logs**: Async logging reduces blocking time

---

## Security Model

### What Damage Control Protects Against

✅ **Accidental catastrophic commands**
- `rm -rf /` during parallel agent execution
- `DROP DATABASE production` in wrong terminal
- `chmod 777` on sensitive files

✅ **Pattern-based risks**
- Deleting AgentWire infrastructure
- Modifying credentials/keys
- Remote destructive operations

✅ **Multi-agent amplification**
- Parallel agents making same mistake
- Cascading failures across sessions

### What Damage Control Does NOT Protect Against

❌ **Intentional malicious activity**
- Attackers can bypass hook system
- Not a replacement for proper auth/permissions

❌ **Logic errors**
- Code bugs that cause data corruption
- Application-level mistakes

❌ **Supply chain attacks**
- Malicious dependencies
- Compromised packages

### Defense in Depth

Damage Control is ONE layer:
- **System permissions**: Run AgentWire as non-root
- **Backups**: Regular backups of critical data
- **Version control**: Git commits for code changes
- **Audit logs**: Track all operations
- **Damage Control**: Block catastrophic commands

---

## FAQ

### Q: Does this slow down AgentWire?

**A**: Minimally. Hooks add ~70-100ms per command, which is negligible compared to actual command execution time.

### Q: Can I customize patterns per session?

**A**: Not yet. Patterns are global — bundled `agentwire/hooks/damage-control/rules/*.yaml` plus an optional override at `~/.agentwire/damage-control/`. Per-session overrides are a future enhancement.

### Q: What if I need to run a blocked command?

**A**: Four options:
1. Add the path to `allowedPaths` in a user-override `*.yaml` under `~/.agentwire/damage-control/` (global) or to `allowed_paths` in the protected `.damagecontrol.yml` at the repo root (per-project — a host-side edit; the agent can't widen its own allowlist)
2. Use "ask" patterns (prompts for confirmation)
3. Temporarily comment out the pattern in your override YAML
4. Run command outside AgentWire session

### Q: Do hooks work in remote sessions?

**A**: Yes, if the remote machine has AgentWire installed with damage-control hooks configured.

### Q: How do I add patterns for my own tools?

**A**: Drop a YAML file into `~/.agentwire/damage-control/` (the user-override layer):

```yaml
# ~/.agentwire/damage-control/mytool.yaml
bashToolPatterns:
  - pattern: '\bmytool\s+dangerous-operation\b'
    reason: mytool dangerous operation blocked
```

Remember: when this directory exists, the bundled rules are **replaced**. Copy the bundled `*.yaml` files in if you want to extend rather than override.

### Q: Can hooks block malicious LLM behavior?

**A**: Only pattern-based risks. Sophisticated attacks that don't match patterns can bypass the system. Damage Control is for accident prevention, not malware defense.

### Q: Where are audit logs stored?

**A**: `~/.agentwire/logs/damage-control/YYYY-MM-DD.jsonl` (one file per day)

---

## Related Documentation

- `agentwire safety` — CLI surface for testing commands and viewing audit logs (`agentwire safety check ...`, `agentwire safety logs`).
- `agentwire/hooks/damage-control/rules/` — bundled pattern source-of-truth.
