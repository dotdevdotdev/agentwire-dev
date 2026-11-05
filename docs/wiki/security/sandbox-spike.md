# Spike: workers inside the Claude Code sandbox (#1279)

**Recommendation: adopt for some roles, opt-in.** Worktree workers and reviewers
run fine under the sandbox with a small allowance list; it genuinely bounds
writes and network for an *approved* Bash command. It is not a replacement for
damage control and not a default: the gaps below are real. Shipped as
`agentwire new --sandbox` (default OFF, recorded so `restart` keeps it,
reported by `agentwire doctor`).

Tested 2026-11-05 with Claude Code 2.1.318 on macOS (Seatbelt). Linux
(bubblewrap + socat) was **not** exercised.

## Settings keys (from the official sandboxing docs)

Delivered with `claude --settings '<json>'`: per-process, above user/project
files, below managed. No settings file is written, so `~/.claude` is untouched.

| Key | Default | We set |
|---|---|---|
| `sandbox.enabled` | `false` | `true` |
| `sandbox.failIfUnavailable` | `false` (runs unsandboxed with a warning) | `true` |
| `sandbox.allowUnsandboxedCommands` | retry allowed | `false` |
| `sandbox.filesystem.allowWrite` | cwd, `--add-dir`, `$TMPDIR` | config allowances |
| `sandbox.network.allowedDomains` | empty | config allowances |
| `sandbox.network.strictAllowlist` (v2.1.219+) | off | `true` |
| `sandbox.network.allowUnixSockets` | none | tmux socket + config |
| `sandbox.network.allowLocalBinding` | off | `true` (macOS: reach portal) |
| `sandbox.excludedCommands` | none | `gh *` |

Defaults that matter: **reads are machine-wide** (`~/.ssh`, `~/.aws` readable);
writes to `.claude/settings*`, `.mcp.json`, `.git/hooks`, shell rc files and
`~/.claude` stay denied even inside allowed dirs.

## Documented gaps (still true under `--sandbox`)

- Built-in Read/Edit/Write/WebFetch/WebSearch are **not** sandboxed.
- Hooks, local MCP servers (so the `agentwire` MCP), statusline and
  `apiKeyHelper` run **outside** the sandbox.
- Tools that ignore `HTTP(S)_PROXY` (plain `ssh`, DB drivers, UDP) cannot connect.
- Under `bypassPermissions`, a host off the allowlist is **allowed without a
  prompt** by default. `strictAllowlist` closes that; we always set it.

## Observed (probe battery, run inside a sandboxed `claude -p`)

Reproduce: scratch repo + bare origin, `claude -p --dangerously-skip-permissions
--settings <json> "<run these commands as separate Bash calls>"`.

| Probe | Round 1 (no allowances) | With allowances |
|---|---|---|
| write in worktree | ok | ok |
| write `$HOME/x` | `Operation not permitted` | same (denied, as intended) |
| write `~/.agentwire/x` | n/a | `operation not permitted` |
| write `.git/hooks/pre-push` | `Operation not permitted` | same |
| `git add`/`commit` in a **linked worktree** | ok | ok, no extra allowance |
| `git push` to local bare origin | ok (bare repo was in `allowWrite`; necessity not isolated) | ok |
| `curl` allowlisted host | 200 | 200 |
| `curl example.com` | 502 CONNECT failed | 403, `strictAllowlist` refusal logged |
| `gh api rate_limit` | **TLS error `x509: OSStatus -26276`** | ok via `excludedCommands: ["gh *"]` |
| `tmux list-sessions` | **`Operation not permitted` on the socket** | ok via `allowUnixSockets` |
| `agentwire list` | **"No sessions running" (false negative)** | correct |
| portal `https://localhost:8765` | empty reply | 200 with `allowLocalBinding: true` |
| `uv run` | **`Could not create temporary file`** (uv cache) | ok via `allowWrite ~/.cache/uv` |

## Failures and the minimal fix for each

1. **tmux socket** blocked: add it to `allowUnixSockets`. Without it `agentwire`
   does not error, it reports *no sessions* — a silent wrong answer, so the
   socket is always added.
2. **`gh`** fails on macOS (Go TLS cannot use the proxy's trust path): run
   outside the sandbox with `excludedCommands: ["gh *"]`. The match is on the
   command *head*: `bash script.sh` that calls `gh` inside stays sandboxed and
   fails. This is an escape hatch by design; it widens, not narrows.
3. **uv cache**: `allowWrite ~/.cache/uv`.
4. **Portal API** on macOS: `allowLocalBinding: true`.
5. **Git over SSH** fails on macOS per the docs; our checkout rewrote ssh→https
   (`url.<ssh>.insteadOf`), so the probe could not exercise it. Use https remotes.
6. **pytest from a different cwd** failed to create `.venv`: expected, the
   sandbox's writable root is the session's cwd.

## Not verified (say so, don't assume)

- `agentwire msg send` / `notify-parent --queued` actually writing
  `~/.agentwire/inbox`: the default allowances name those paths, but writing the
  real inbox from a probe was out of bounds for this spike. A write-denied
  `~/.agentwire` was verified; the inbox allowances are inferred from the code
  (`inbox.INBOX_ROOT`, `EVENTS_FILE`, `sent_ledger.ledger_dir`). Verify with one
  real `--sandbox` worker before relying on report-back.
- A real `git push` to GitHub and `gh pr create` (only `ls-remote` and
  `gh api` read paths were exercised).
- Scheduler/unattended dispatch, Linux, cost/latency (not measured).

## Residual risk if adopted

Damage control remains the decision layer and is unaffected (hooks run outside
the sandbox). The sandbox adds a second, OS-level bound on Bash only. It does
**not** stop reads of secrets (`~/.ssh`), nor writes through the Edit/Write
tools, nor anything an excluded command does.

## What shipped

- `agentwire new --sandbox` — builds `--settings` via `agentwire/sandbox.py`;
  the flag is recorded in the launch record and `agentwire restart` regenerates it.
- `sandbox:` in `~/.agentwire/config.yaml` — `allow_write`, `allowed_domains`,
  `allow_unix_sockets`, `excluded_commands`, `allow_local_binding`. Absent keys
  keep their defaults; the five fixed invariants (`enabled`, `failIfUnavailable`,
  `allowUnsandboxedCommands: false`, `strictAllowlist`, tmux socket) are not configurable.
- `agentwire doctor` — lists sessions launched sandboxed and whether the
  machine can start the sandbox (`sandbox-exec` on macOS, `bwrap`+`socat` on Linux).
- No default posture changed.
