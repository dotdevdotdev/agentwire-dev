# Damage-Control Matcher Hardening (2026-06)

> Consolidated security note for the hardening shipped in PR #500. High-level by
> design — it records *what* was strengthened and the new operational behavior,
> not step-by-step bypass recipes. The concrete evasion vectors live as
> regression tests in `tests/unit/test_damage_control_bypass.py`.

An internal adversarial review of the damage-control matcher (the
`PreToolUse`/permission layer that classifies agent actions as allow / ask /
block) surfaced five classes of weakness. All five were fixed in one change.
Public over-block issue [#492](https://github.com/dotdevdotdev/agentwire-dev/issues/492)
was closed as part of the same work; the remaining four were held until the fix
landed and are summarized here afterward. Credit: internal security review.

## What was hardened

1. **Control-plane path coverage.** The matcher already protected its own
   kill-switch/rule/hook files. It now also protects the *execution-plane*
   configs whose strings agentwire runs through its **own**
   `subprocess.run(..., shell=True)` calls — `~/.agentwire/scheduler.yaml`
   (gate commands), `~/.agentwire/config.yaml` (service healthchecks), and
   per-project `.agentwire.yml` (task commands). Those subprocesses never
   traverse the Claude Code hook, so write-access to them was a confused-deputy
   path to unguarded execution. Tradeoff: under worktree dispatch, task config
   is now authored host-side.

2. **Tilde / `$HOME` canonicalization.** Commands are canonicalized (`~`,
   `$HOME`, `${HOME}` expanded) before path matching, so a home-relative
   reference to a protected path resolves to the same rule as its absolute form.

3. **`.env` whole-component matching (#492).** Zero-access *literals* are matched
   as complete path components instead of bare substrings, so ordinary text
   (`.environment`, `docs/.env.example`, `grep -v .environ`) is no longer
   hard-blocked. Committed env *templates* (`.env.example`, `.sample`,
   `.template`, `.dist`) are treated as secret-free and stay readable.

4. **Shell-aware, fail-closed matching.** Commands are tokenized, quotes/escapes
   are stripped, and simple `$VAR` assignments are resolved before rules are
   matched against the normalized sub-commands — closing a family of
   quoting/escaping/indirection evasions. Constructs that cannot be statically
   verified fail closed (see below). A missing YAML parser now fails closed
   (block) rather than open (no rules).

5. **Read-surface policing.** Native content-reading tools (`Read`, `Grep`,
   `Glob`) are now routed through the zero-access checks via a dedicated hook,
   so a secret can't be read directly without traversing damage control. The
   `zeroAccessPaths` guarantee now accurately spans shell, file edits/writes,
   **and** reads.

## New operational behavior: fail-closed on unverifiable commands

Commands whose intent can't be statically verified — **command substitution**
(`$(...)` / backticks), **`eval`**, a **`base64 -d | sh`-style pipeline**, or
**unbalanced quotes** — now resolve to **`ask`** instead of a silent allow.

How `ask` resolves depends on the session:

- **Interactive bypass / auto sessions** (most agent sessions): `ask` → allow,
  unchanged. No new friction.
- **Interactive non-bypass sessions:** `ask` → the human confirms.
- **Unattended scheduler dispatch** (`AGENTWIRE_UNATTENDED=1`, no human present):
  the unattended guardrail turns `ask` → **block + owner email**. This is the
  safe default for cron-style work.

> A position-aware refinement (reasoning about *where* a substitution appears) is
> tracked separately; for this release the conservative fail-closed behavior
> stands.

### Allowlisting a legitimate unattended task

If a scheduled task legitimately needs a construct that now fails closed (e.g. a
gate or command using `$(...)`), an operator opts it in — always a host-side act:

- **Per task:** add the matched rule id to the task's `unattended_allow` list in
  its `.agentwire.yml` (the scheduler stamps it into
  `AGENTWIRE_UNATTENDED_ALLOW` for that dispatch).
- **Globally:** add the rule id to `safety.unattended_allow` in the protected
  `~/.agentwire/damagecontrol.yml`.

The blocked-action email names the rule id to add. See
[`docs/wiki/internals/damage-control.md`](../internals/damage-control.md) for the
unattended guardrail and [`secrets.md`](secrets.md) for the secret-path policy.

## Regression coverage & CI

`tests/unit/test_damage_control_bypass.py` loads the real bundled rule YAMLs and
asserts both directions at once: the evasion corpus stays block/ask, and a
false-positive corpus of common safe commands keeps passing (a safety layer that
cries wolf gets disabled). The `.github/workflows/security.yml` workflow runs
this corpus as a **hard merge gate**, plus advisory `bandit` and `pip-audit`
passes (the latter advisory because most flagged CVEs are in heavy optional deps;
direct portal-facing deps are bumped explicitly).
