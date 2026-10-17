# Damage Control: Security Firewall for AgentWire

> Living document. Update this, don't create new versions.

---

## Overview

Damage Control is a security firewall system that protects AgentWire from dangerous operations during parallel agent execution. It intercepts tool calls (Bash, Edit, Write, Read, Grep, Glob) via PreToolUse hooks and blocks operations matching security patterns.

**Why Critical for AgentWire**: Parallel remote agent execution multiplies risk. A single `rm -rf /` in a remote session is unrecoverable. Multi-agent execution amplifies the chance of catastrophic mistakes.

### Protection Layers

| Layer | Coverage |
|-------|----------|
| **Bash Tool** | Commands: `rm -rf`, `git push --force`, `systemctl stop`, database drops |
| **Edit Tool** | File protections: SSH keys, credentials, `.env` files, system configs |
| **Write Tool** | Same as Edit tool (creation protection) |
| **Read/Grep/Glob** | `zeroAccessPaths` enforcement on content reads — blocks reads of secrets even without writing to them |
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
├── read-tool-damage-control.py       # Read/Grep/Glob hook (zeroAccessPaths enforcement)
├── mcp-tool-damage-control.py        # Outbound MCP tool hook (email_send/quo_send)
├── audit_logger.py                   # Audit logging framework
└── rules/                            # Pattern files (categorized)
    ├── core.yaml                     # rm, chmod, system-level dangers
    ├── git.yaml                      # force push, reset --hard
    ├── databases.yaml                # DROP, TRUNCATE, migrations, raw DML
    ├── containers.yaml               # docker prune/push, kubectl delete
    ├── cloud-hosting.yaml, aws.yaml, gcp.yaml, firebase.yaml  # incl. deploys
    ├── infrastructure.yaml, remote.yaml
    ├── control-plane.yaml            # protected agentwire-owned paths (readOnlyPaths)
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

#### Flagless and indirect writers (#1095)

The generic ladder names writers that carry a write *token* (`>`, `tee`, `cp`,
`rm`, `sed -i`). A writer whose only link to the file is an operand, or whose
path arrives indirectly, is classified by the **operation on the operand**,
whatever the spelling (`PROTECTED_WRITER_PATTERNS`, `INTERPRETER_WRITE_PATTERNS`
in `safety/_core.py`; applied only to the protected tier, never to user
`readOnlyPaths`):

- **Operand writers:** `ln`/`link` (path as the *target*; as the source it is a
  read), `curl -o/-D/-c/--output…`, `wget -O/-o/-a…`, `tar` extract naming the
  path or create with it as the archive, `gzip`/`gunzip`/`bzip2`/`xz`/`zstd`…
  (unless `-c`/`-t`/`-l`), `patch`, `touch`, `sponge`, `git restore` (not
  `--staged` alone) and `git checkout`. `$HOME`/`${HOME}`/`~`/absolute are one
  file (`check_protected_command` canonicalizes first).
- **Extraction into a directory** that holds control-plane files names no
  protected path (`tar xf a.tar -C ~/.agentwire`): keyed on the ancestor
  directories (`PROTECTED_DIR_EXTRACT_PATTERNS` — `tar -C`, `wget -P`,
  `curl --output-dir`, `unzip -d`, `cp/mv/install/ln -t`). A human `allowedPaths`
  entry covering the directory re-permits it.
- **`xargs` is a wrapper, not a verb:** its command is re-scanned with the
  mentioned path appended (or substituted for `-I`), so `… | xargs rm` is judged
  as `rm <path>` and `… | xargs cat` as `cat <path>` (`_xargs_rescans`).
- **`| sh`, `sh <<EOF`, `while read`/`for` loops, `sh -c` with a variable
  target** have a payload that is data, not a command we can re-scan. Stance:
  fail closed when a shell is fed (pipe/heredoc/stdin) in a command that
  mentions a protected path, or when a loop / `sh -c` body writes *through a
  variable* (`> "$f"`, `rm "$f"`) in a command that mentions one. A loop whose
  body only reads, or writes to a literal other file, is allowed.
  Newly blocked as a consequence of failing closed: `cat <path> | sh -c 'wc -l'`
  and `X=<path> python3 -c 'print(1)'` (a protected path piped into a shell, or
  assigned before an inline interpreter, is refused whatever the program does).
  A loop that hands a variable to an inline interpreter
  (`… | while read f; do python3 -c '…argv[1]…' "$f"; done`) and a heredoc piped
  into a shell (`cat <<EOF | sh`) are in the same class.
- **Text is not a command:** a quoted arg that mentions the path among other
  words (`git commit -m 'touch <path>'`, `gh … --body "gzip <path>"`) is blanked
  before the operand-writer patterns run; a bare quoted path and `-c` program
  text are kept (`_blank_prose_args`). Wrapper payloads are NOT prose: an arg
  to `ssh`/`eval`/`watch`/`xargs`/`sudo`-style runners, a double-quoted `$(…)` or
  backtick, a `<(…)` process substitution, or text piped into `xargs sh`/`sh`
  stays visible. The cost (#1100): ANY substitution in a double-quoted argument
  that also names a protected path is refused, even a harmless one
  (`git commit -m "touch <path> $(date)"`) — it executes, so it cannot be told
  from a payload. The block message says so; rephrase with single quotes
  (`$(` is data there) or move the substitution out of the string.
- **Env-prefix indirection** (`X=<path> python3 -c '…os.environ["X"]…'`,
  `env X=<path> perl -e`) is the inline-interpreter net with the path *before*
  the interpreter.
- **Split path strings:** adjacent / `+` / `.`-joined quoted literals are folded
  (`fold_string_concat`) before the interpreter net reads them.

**Derived commands: heredocs, stdin and wrappers (#1098, #1099).** Text a
runner will execute is re-scanned as a command in its own right
(`_derived_commands`, depth- and fan-out-capped), so the verb decides exactly as
it would unwrapped:

- **Unquoted heredoc bodies** expand `$(…)` and backticks, so each substitution
  is a command (`cat <<EOF` … `$(touch <path>)` … `EOF`, also inside
  `git commit -m "$(cat <<EOF …)"`). A quoted delimiter (`<<'EOF'`, `<<"EOF"`) is
  data, as is an escaped `\$(` or arithmetic `$((…))`; a substitution that only
  reads is allowed.
- **Stdin handed to a runner** — a heredoc body, a here-string or piped
  `echo`/`printf` text — is a command when the runner executes its stdin:
  `ssh host` with no remote command or a remote command that is itself a stdin
  reader, `at`, `batch`, `crontab -`, `. /dev/stdin` / `source /dev/stdin`, a
  bare `parallel` (or `parallel bash -c` / `--pipe sh`), and a shell or
  interpreter directly (`sudo sh`, `exec bash`, `busybox sh`, `su -`,
  `docker exec -i c sh`, `sudo -s` / `sudo -i` / `sudo su`). One predicate
  (`_stage_reads_program`) decides, so the same payload is refused or allowed
  however it is fed (pipe, heredoc, here-string): shells and the interpreters of
  the inline-interpreter net (`perl`, `python3`, `node`, `ruby`, `php`, `deno`,
  `bun`) behind `sudo`/`exec`/`nohup`/`busybox`/`env`/`command`/`--`/`-s`;
  `sudo -s`/`-i` count only as sudo's *own* options (`sudo ls -i` does not).
  With a real remote program (`ssh host cat`, `sudo tee`) stdin is data and is
  left alone. Other interpreters (`lua`, `tclsh`, …) are not in the list, the
  same stance the unwrapped inline-interpreter net takes.
- **Heredoc bodies are data for the rewriting passes** (cd / `git -C`
  resolution, glob, brace, redirect and path normalization), quoted delimiter or
  not: a commit body that *describes* `cd <dir> && touch <file>` is not that
  command (`_hide_heredoc_bodies`). In an unquoted body only `$(…)` and
  backticks are code, and those are scanned on the full command (#1098). Bodies
  that feed a shell or runner (`ssh host <<'EOF'`, `sudo sh <<EOF`) are the
  program and stay visible. `|&` is a pipe like `|`. Heredocs end
  where bash ends them: the delimiter ALONE on its line (` EOF` and `EOF ` are
  body text), with `<<-` stripping leading tabs only. The delimiter is a full
  shell word: adjacent parts concatenate (`<<'E'OF`, `<<E"O"F`, `<<$'EOF'` all
  end at `EOF`) and any quote or backslash makes the body literal (`<<\EOF`
  like `<<'EOF'`). Several markers on one line read their bodies in order, and
  each is judged by the simple command it is attached to (`cat <<'A'; sh <<'B'`:
  only B is a program), including a group closed on a later line
  (`(cat <<'EOF' … EOF` newline `) | sh`); operators split into words with or
  without spaces (`<<'EOF'|sh`). A `crontab -` body is judged after its five schedule fields (or
  `@reboot`), and leading whitespace before a `cd` does not hide it.
- **A marker is live only when bash reads it as a redirection (#1106).** One
  tokenizer (`_HeredocScanner`, behind `_heredocs`) decides liveness, finds the
  terminator and reads the body; `_hide_heredoc_bodies`, the inert view
  (`_neutralize_literal_heredocs`) and the older command masker
  (`_strip_heredoc_bodies`) all consume its result, so there is no second
  scanner to disagree with. It walks quotes, `$'…'`, backslash, `#` comments
  (only at the start of a word: `echo a#<<b`, `$#` and `${#v}` are not
  comments; a backslash-newline joins lines, so a `#` after one is a comment),
  `${…}`, `$(…)` and backticks (recursively), and treats `<<` as no heredoc
  inside `$((…))`, legacy `$[…]`, `name[…]=` subscripts, `((…))` (in any
  command position: after `;`/`&&`/`|`, `for`/`while`/`until`/`if`/`then`/
  `elif`/`else`/`do`/`time`/`!`/`{`, and as the `for ((…;…;…))` header),
  `[[ … ]]`, a `let` segment, `<<<`, and a delimiter split by a continuation.
  `<(…)`, `>(…)` and `name=(…)` / `name+=(…)` re-parse their body as separate
  text, so a marker inside closes with the `)` and the next line runs (one
  list, `_REPARSED_OPENERS`: the character before the `(`). A marker whose
  innermost scope is a `name=(` / `name+=(` literal (including `declare`/
  `local`/`export`/`readonly`/`typeset` forms) is never a heredoc, closed or
  not: bash cannot hold a heredoc there, and a *recovering* bash (macOS
  `/bin/bash` 3.2 keeps executing after that syntax error; zsh and newer
  `bash -c` abort) runs the next line. One rule covers every such line (the
  scanner's *poisoned* flag): a `<<` token anywhere in a literal (`a=([1<<2]=x)`,
  `a=(x <<2)`, `a=(${y}<<2)`; quoted `"<<"` and arithmetic `$((1<<2))` are
  not tokens), or a literal that never closes, makes the line a syntax error.
  Every heredoc found inside the literal is discarded and every marker on that
  line, before or after the literal (`a=([1<<2]=x) <<EOF`), is discarded at the
  line's newline instead of reading a body; the flag clears at the first
  newline outside the literal, so a literal closed on a later line poisons the
  rest of its closing line. A closed, clean literal's nested `$(…)` heredoc,
  and a multi-line `<(…)`, are still read. A heredoc opened
  inside `$(…)` / backticks whose body has unbalanced parentheses is not hidden
  (older bash matches the span by counting parentheses, blind to heredocs). A
  marker after a comment, after `\`, or in those contexts opens nothing, so the
  lines after it stay visible to every rule (before, `echo a # <<'EOF'` hid the
  whole rest of the command). `\\<<'EOF'` (escaped backslash) IS a marker.
  Failure direction: every uncertainty resolves to "not a heredoc". A heredoc
  with no terminator line (or only a wrong one) is dropped, though bash would
  swallow the rest as its body: a deliberate fail-closed choice, since a command
  that opens a heredoc it never closes is broken, and hiding to the end would
  make any liveness-model gap hide the whole remainder (a deliberate trigger can
  always append a terminator, so this guards the accidental case and the
  explicit `echo 1<<2` shift canaries). Cost: a prose body with a mistyped
  terminator is scanned as commands (a rephrase). So is one whose `$(…)`/backtick span closes before the newline (bash
  runs the next line). A partly quoted delimiter is one word, so `<<'EO'F` ends
  at `EOF` like `<<'E'OF`, and its literal body is data like any quoted
  heredoc. Declared residuals, all scanned rather than hidden (a rephrase, not a
  bypass): `<<$"EOF"` / `<<-$"EOF"` (locale-quoted delimiter) is not
  recognised, so its body is read as commands; a `case … esac` pattern `)`
  inside `$(…)` can end that span early; `<<` inside a nested `$(…)` within
  `$((…))` is not walked; a single very long line is quadratic in the rule
  path on main and here (tracked in #1108, not part of this change); `cat <<\EOF | xargs sh -c` (a heredoc body fed to a
  data-fed program) is allowed, as it already was for the other quote spellings. `tests/unit/test_heredoc_marker_liveness.py` pins both
  halves (non-heredoc contexts × dangerous tails must block; real heredocs'
  prose bodies must keep their decision) and each guard has a mutation that
  turns it red.
- **`parallel` is xargs-shaped:** its command is re-scanned with the path
  substituted for `{}` (`{.}` `{/}` `{//}` `{/.}` `{#}`, or `-I`), or appended.
  A quoted command string is scanned whole, separators included
  (`parallel 'echo x; rm {}'`), because parallel hands it to a shell.
- **Prefix wrappers** (`caffeinate`, `ionice`, `nice`, `taskset`, `chrt`,
  `sshpass`, … — `_PREFIX_WRAPPERS`) run the command that follows them: a quoted
  arg behind one is code when an arg-runner (`ssh`, `watch`, …) follows and
  prose otherwise (`caffeinate git commit -m '…<path>'` stays allowed).
- **A computed verb** (`bash -c "$(cat x) <path>"`, `$v <path>` handed to a
  shell) is refused when the path is named: nothing static says it does not
  write.

**Spellings of the same file (#1101).** Each is rewritten to the plain form and
re-scanned as a command, so the verb still decides (reads stay allowed):

- **More writers:** `mv` with the path as the *source* (it removes the file),
  `rename`/`mmv`/`chflags`/`setfacl`/`chattr`, `yq`/`tomlq`/`xq` and `sed`/`gsed` with
  `i` anywhere in a flag cluster (`-i`, `-iy`, `-yi`, `-ni`) or `--inplace` /
  `--in-place`, `dasel put|delete`, `scp` with the path as the destination,
  `ed`/`ex` (always scripted), `vi`/`vim`/`nvim`/`view` driven
  non-interactively (`-es`, `-s`, `-c`, `+cmd`, `--headless`) and `emacs
  --batch`. A bare `vim <path>` needs a terminal and is left alone.
- **Redirect operators** `>|`, `>&`, `&>`, `&>>`, `<>` are the same write as `>`
  (the subcommand splitter otherwise reads `>|` as a pipe).
- **Brace expansion** (`damagecontrol.y{m,x}l`) is exact — every expansion is
  scanned (a group count past the cap is not expanded: residual).
- **Globs and partial expansions** (`damage*.yml`, `damagecontrol.y?l`,
  `damage$x`): the word is matched with shell-glob semantics against the
  concrete protected paths and the real directory listing, and each hit is
  substituted back — `rm ~/.agentwire/*` is refused, `ls ~/.agentwire/damage*`
  is not. A bare relative glob needs a 3+ character literal run (`rm *` names
  nothing). **Stance:** a word whose literal part can still resolve to a
  protected file is judged as that file; a path component that is *only* an
  expansion (`~/.agentwire/$f`, `$d/damagecontrol.yml`) is not blocked — it would
  stand for every file in a directory agents write to constantly.
- **Mid-path `//`, `/./`, `/../`** are collapsed (`touch ~/.agentwire/x/../damagecontrol.yml`).
- **A directory set by a literal `cd <dir> &&` / `pushd`, `git -C <dir>` or
  `git --work-tree <dir>`** is applied to later words (until the next `cd`):
  `cd ~/.agentwire && touch damagecontrol.yml` is judged as the absolute path,
  quoted operands included (`touch "damagecontrol.yml"`, `open('damagecontrol.yml')`).
  The text after the `cd` is folded the way the shell folds it before the
  program sees it — adjacent quotes (`'damage''control.yml'`), backslashes,
  ANSI-C `$'…'`, `$PWD` / `${PWD}` as the known directory, `./` — and relative
  globs (`damagecontrol.y?l`, `d*ntrol.yml`) are matched against the protected
  files in that directory (`rm *.yml` there is refused, `cat d*ntrol.yml` is not).
  `~` and `$HOME` are directories for `git -C` / `--work-tree` as well
  (`git -C ~ checkout .agentwire/damagecontrol.yml`).
- `xargs -I{}` / `parallel` substitute into an interpreter literal
  (`… | xargs -I{} python3 -c 'open("{}","w")'`); because the interpreter net is
  fail-closed for any inline program naming the path, an inline *read* of it is
  refused the same way it is unwrapped.

Declared residuals (not statically knowable): paths assembled at runtime
(`join`, `chr`, `format`, decoding, `d=$(…); … $d/damagecontrol.yml`, a
component that is only `$var`), stdin text produced by anything but
`echo`/`printf`/a heredoc (`cat file | ssh host`, `bash -c "$(cat x)"` names no
path until it runs), text that a stdin reader receives through a stage the
scanner does not model (`… | tee x | ssh host`, `… | mosh host`,
`… | ssh host xargs -0 sh`), interpreter source piped to a remote interpreter
(`echo "open('<path>','w')" | ssh host python3` — the text is re-scanned as
shell, not as python), a `patch` / `git apply` (including `git -C <dir> apply`)
whose target is named only inside the diff, `sftp get`, `ssh host lua` (and
other interpreters outside the list), a `cd` written inside interpreter source
(`perl -e 'system("cd <dir> && touch <file>")'`), a body piped into a
`sh -c '{}'` placeholder (`cat <<'EOF' | xargs -I{} sh -c '{}'` with the literal
path in the body), `cd <dir> && rsync -a <file> host:x` (the absolute spelling
is refused too — rsync's source is not distinguished from its destination), a
a crontab line with a trailing `# …` comment that mentions the path (blocks),
`<<$"EOF"` (not recognised as a heredoc, so a body line that is itself
`cd <dir> && touch <file>` blocks; #1106), a directory taken from `$(pwd)` after a `cd` (ask-tier, not
resolved), a cwd known only from the session or set by `cd $dir` / `cd -` / a
subshell the command cannot see, `make -C`/`env -C`-style directory options,
writes through a symlink or hardlink alias, a brace expansion past the cap, a
derived-command budget exhausted by a pathological command, and the TOCTOU
window. Directory-extraction spellings `/.`, `//`, `/sub/..`,
`--one-top-level=` and `bsdtar` are normalized. Pins:
`tests/unit/test_control_plane_writers.py` (must-block per verb *and* must-allow
siblings).

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

Patterns live in **categorized YAML files** under `agentwire/hooks/damage-control/rules/` (15 files, one per topic). To override or extend, drop YAML files into `~/.agentwire/damage-control/` — when that directory exists with `*.yaml` files, hooks load from there instead of the bundled rules.

### Pattern Types

#### 1. bashToolPatterns (Bash commands)

Block dangerous shell commands using regex patterns:

```yaml
bashToolPatterns:
  - pattern: '\brm\s+(-[^\s]*)*-[rRf]'
    reason: rm with recursive or force flags
    anchored: true

  - pattern: '\bgit\s+push\s+--force\b'
    reason: git push --force (use --force-with-lease)
    anchored: true

  - pattern: '\bsystemctl\s+stop\b'
    reason: stopping system services
    anchored: true
```

##### `anchored` — command position vs argument content (#675, #915)

An unanchored rule matches the **raw command string**, so it fires on text that
merely *mentions* the operation: a commit message, an `echo`, a `grep` whose
search string is the rule's own reason, a `--kind done` report explaining that a
deletion was refused. That is #915 — a report-back refused for what it says,
and it reaches every command whose arguments discuss a guarded operation,
including most of the tooling you would use to audit the guard.

`anchored: true` matches against `masked_subcommands()` instead: quoted tokens
that can only be content (a quoted token **containing whitespace**, a heredoc
body) are blanked, while quoting obfuscation of the command itself still
normalizes (`"rm" -rf`, `r\m`, `R=rm; $R`) and `sh -c "…"` payloads are
recursively rescanned.

**Anchoring is decided per FILE, not per rule.** A file may be anchored when
every rule in it is command-prefix shaped **and** the tool takes no
inner-command payload — the `git.yaml` shape test. Two reasons it cannot be a
per-rule choice:

- Masking blanks quoted content *regardless of position*, so a **wrapper** rule
  whose danger arrives as a quoted payload — `ssh box "sudo rm -rf /var"`,
  `psql -c "DROP TABLE users"`, `python -c "…shutil.rmtree…"` — loses exactly
  the half it exists to read.
- It is a *(rule × command-form)* property. Anchored `core.sudo-rm` still blocks
  the local form but loses the ssh-wrapped one; its twin in `remote.yaml` is the
  backstop that keeps that covered. Anchor both and the wrapped forms go from
  double-covered to **uncovered**.

So the wrapper rules live in two uniformly-**unanchored** files —
`payloads.yaml` (SQL statements, interpreter `-c`/`-e`/`--eval` payloads,
`$(…)` substitution) and `remote.yaml` (ssh) — rather than being flagged in
place. No file mixes the two; a mixed file is a per-rule skip-list wearing a
filename. Rules moved into `payloads.yaml` carry an explicit `id:` pinned to
their previous id, so `safety.disabled_rules` / `unattended_allow` entries keep
working.

##### What anchoring gave up — the ssh-wrapped class (#924)

Anchoring the 151 command-prefix rules demotes roughly **125 of 151
ssh-wrapped dangerous forms from refused to allowed**:
`ssh prod "terraform destroy"`, `ssh prod "kubectl delete namespace prod"`,
`ssh prod "gh repo delete …"` and so on now pass.

This is a demotion of **incidental** coverage, not a broken guard: those forms
only blocked because of the same match-anywhere behaviour that *is* the payload
bug. `remote.yaml`'s 12 rules are the *intentional* ssh coverage and are
untouched — but the coverage was real while it lasted, so it is stated here
rather than left to be discovered.

The fix is **#924**: extend the `_SHELL_NAMES` rescan to
`ssh <host> "<payload>"` so every rule applies to the payload, after which
`remote.yaml` becomes **deletable** rather than something to extend. Widening
`remote.yaml` to ~120 ssh twins is explicitly not the answer — that duplicates
the entire rule set, which is the coupling this design exists to avoid.

Known limits, all asserted as expected-fail rows in
`tests/unit/test_damage_control_payload_anchoring.py` so a green suite cannot
imply they work:

| limit | example | tracked |
|---|---|---|
| ssh-wrapped forms outside `remote.yaml`'s 12 | `ssh prod "terraform destroy"` | #924 |
| path ladders have no `anchored` concept | `grep -rn "<deletion>" ~/.agentwire/` → blocked by `noDeletePath` | #922 |
| masking is keyed on **whitespace** | `msg send --kind done "rmdir"` → still blocked | #922 |
| trailing shell comment is not masked | `true  # <guarded op> was blocked` | #922 |

The middle two are why the **payload bug has three mechanisms** and anchoring
`bashToolPatterns` fixes only the first. The path ladders in particular run
three different predicates (`protectedControlPlane`, `zeroAccessPaths`,
`readOnly`/`noDelete`), which is why one anchored-style flag will not serve
them — the same *(rule × command-form)* conclusion that made anchoring a
per-file decision.

Both invariants are enforced by
`tests/unit/test_damage_control_payload_anchoring.py`, which also asserts a
dangerous form for every anchored rule against a **solo config of that rule
alone** — a full-rule-set assertion can pass because some *other* rule caught
the command, which is how a synthetic one-rule fixture stays green through real
regressions.

**Coverage**:
- Destructive file operations (`rm -rf`, `shred`, `truncate`)
- Permission changes (`chmod 777`, `chown root`)
- Git destructive operations (`reset --hard`, `push --force`)
- Database operations (`DROP DATABASE`, `TRUNCATE`)
- System operations (`shutdown`, `reboot`, `systemctl stop`)
- Docker destructive operations (`system prune`, `rm -v /`)
- Package manager risks (`apt-get autoremove`, `npm uninstall -g`)

#### 1a. Global options before the subcommand (#913, #919)

Every pattern above names a tool and then its subcommand — `\bgit\s+push\b`,
`\btmux\s+kill-server\b` — and `\s+` cannot span a global option. So for a long
time `git -C /repo push --force`, `tmux -L agentwire kill-server` and
`kubectl --context prod delete namespace prod` were all **allowed**, while their
plain forms blocked. In `aws`/`kubectl`/`docker`/`redis-cli` the bypassing option
is the *production-targeting* one, which inverts the guard: it held for the
default target and dropped for the named remote one.

The fix is one normalizer at the shared matching seam, driven by a per-tool data
table (`_GLOBAL_OPTION_TABLE` in `agentwire/safety/_core.py`). Rules do not need
to know about it: `global_option_normalized_haystacks()` emits an **extra**
haystack per subcommand with the tool's global options removed, and present and
future rules inherit the fix.

Four properties are load-bearing:

- **Additive.** A third haystack *added* to whichever list a rule already reads.
  Neither the raw nor the masked list is rewritten — some stripped values are
  command payloads (`git -c core.sshCommand=<cmd>`, `tmux -c <shell-command>`),
  and deleting them in place would turn a block into an allow.
- **Fed to both routings**, anchored and unanchored. Which rules are anchored is
  not stable, and the intersection *must-stay-unanchored ∩ bypassable* is
  reachable only if unanchored rules see it too.
- **Derived from the masked tokens**, so quoted argument text can never become
  matchable (#675): `echo 'tmux -L x kill-server'` stays allowed.
- **Per-tool grammar, measured against the binary.** git's usage line advertises
  only the `=` form while the binary accepts both, and `--exec-path` looks
  value-taking and is not. The `short_cluster` property matters most: tmux is
  getopt-based and accepts `-Lname` and `-2Lname`, while git *rejects* `-C/tmp` —
  so the fleet-kill bypass has three spellings and git's answer is the wrong
  default for the class.

**Adding a tool is a table row, not a patch.** Each row records `provenance`
(`measured` against a named binary version, or `documented` where the binary was
not available) because *both directions* of a wrong arity guess fail open — mark
an option value-taking when it is bare and the subcommand is eaten; mark it bare
when it takes a value and the value stays inline. Neither shows up as a spurious
block, so a wrong row is silent. That is why every row must also carry at least
one acceptance-corpus command in `tests/unit/test_damage_control_hooks.py`
(enforced by `test_every_row_has_corpus_coverage`): a row nothing exercises
asserts a grammar nothing can contradict.

Known limits, recorded rather than hidden: argument-consuming wrappers
(`timeout 5 git …`), mysql-style unique-prefix long-option abbreviation
(`mysqladmin --us=root`), and any option a tool adds after the measured version.
All fail by producing *no* variant — an under-strip, never an over-strip.

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

**The override cuts both ways (#938).** Because `allowedPaths` outranks control-plane protection, a *broad* entry silently turns that protection off for whatever it covers — `{path: "*/.agentwire/*", allow: all}` makes the kill switch (`~/.agentwire/damagecontrol.yml`), the rule files, and the hook scripts agent-writable, and `{path: "~/.claude/*"}` takes hook registration (`settings.json`) with it. The control plane is protected *unless your allowlist covers it*. `agentwire doctor` flags any entry whose glob overlaps a protected control-plane path, using the enforcement matcher itself, and names both sides.

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
(`scheduler/dispatch.py::_unattended_env`). Session creation funnels that marker into the new
tmux session via `tmux new-session -e K=V` (`core.py::_with_unattended_env`), so
it lands before the agent launches and the hook can read it. Interactive
sessions never pass through that chokepoint, so the marker can't leak into a
human's session — even though interactive sessions use the same
`--dangerously-skip-permissions` posture.

**A child session inherits BOTH vars — and the two inherit differently (#914).**
`_UNATTENDED_ENV_KEYS` carries the marker and the allowlist as one unit, so a
session an unattended agent spawns gets both, transitively to any depth and
across projects (`created_by` is dropped for a cross-project spawn, #715; this
env is not rooted). For `AGENTWIRE_UNATTENDED` that is defense in depth — it
*tightens*, so inheriting it can only ever block more. **The allowlist rides the
same path and *loosens*,** which is a materially different thing and went
undocumented until #914.

It is kept, deliberately. The motivating fan-out task (`memory-manager`) does
not act itself: it spawns four children that do the committing, so a grant that
stopped at the parent could not fix a delegating task **at all** — and that gap
applies to every task that delegates. What makes the inheritance safe is that
the grant carries its **path scope** with it, so what a child inherits is
"commit under `<store>`", not "commit". A child cannot widen it: the hook reads
the var from the Claude Code process environment, not from the shell the agent
runs commands in, and the files that define grants are protected control plane.

**What's unaffected.** Hard `block` rules (`rm -rf`, `git push --force`, DB
drops) fire regardless — they never depended on a human. Interactive `bypass`
sessions resolve `ask` exactly as before. The kill switch still wins: with
`enabled: false` in `~/.agentwire/damagecontrol.yml`, nothing is checked, so the
unattended gate is inert too (enable safety for scheduled projects to engage it).

**The allowlist** — three layers, **most specific wins outright**:

| Layer | Where | Notes |
|-------|-------|-------|
| 1. per-task `unattended_allow` | `.agentwire.tasks.yml` | The pressure-relief valve: widen (or narrow) for one task instead of loosening the global default |
| 2. `unattended_allow` | `~/.agentwire/damagecontrol.yml` / project `.damagecontrol.yml` | Global / per-project |
| 3. `DEFAULT_UNATTENDED_ALLOW` | `safety/_core.py` | Built-in: `git.add`, `git.add-u`, `git.commit`, `git.push`, `gh.pr-create`, `outbound.agentwire-email` — work + open a PR + notify the owner by email |

**Precedence, not union (#914).** The most specific layer that *names* a rule id
defines that rule's grant outright. A union would make a scoped entry
unexpressible: `git.commit` is already granted **unscoped** by layer 3, so a
task writing `{id: git.commit, paths: [<store>]}` under a union would read as a
constraint and mean nothing. Naming a rule at a more specific layer therefore
**replaces** the looser grant for that dispatch. Within one layer, several
entries for the same id union their scopes.

Naming a rule **binds** it even when the entry is malformed. A refused entry
does not fall through to a looser layer — otherwise a typo in a scope path
(`paths: [relative/dir]`) would silently hand the task the unscoped default,
i.e. a typo granting commits in *every* repo.

Allowlisting is **by rule ID**, not command text — so `git.push` (plain push)
is allowed while `git push --force` (hard block) and `git push --delete`
(distinct `ask` rule `git.deletes-remote-branch`) are not. Tooldef commands the
allowlist references carry an explicit `id:` so the ID is stable across
description edits.

> **A grant naming a rule id that does not exist is inert, and reads exactly
> like one that works.** That is how the whole built-in set went silently dead
> on one machine: `~/.agentwire/tooldefs/git.yaml` was missing the four `id:`
> lines the bundled copy has, the user copy wins, and five of six
> `DEFAULT_UNATTENDED_ALLOW` ids resolved to nothing — so scheduled tasks were
> blocked on `git commit` and it surfaced as a `max_duration` timeout.
> `agentwire doctor` now reports inert built-in grants, and `agentwire tasks
> review` reports a task grant naming an unknown id.

### Path scopes (#914)

An entry is either a bare rule id (**unscoped** — permitted wherever the rule
fires) or a mapping carrying `paths`:

```yaml
unattended_allow:
  - outbound.agentwire-email                  # bare — unchanged, still works
  - id: git.commit
    paths:
      - ~/.claude/projects/*/memory/          # scoped — only under here
```

A scoped grant applies only when **every** directory the matching command would
act on resolves inside one of `paths`. `*` matches within a path segment, `**`
crosses segments; scope paths must be absolute or `~`-rooted (a relative scope
would shift with the dispatch cwd, which is the one thing a scope exists to
pin down). Matching is case-sensitive even on a case-insensitive filesystem —
the wrong direction to be permissive in.

Scope evaluation can only ever **refuse** a command the bare grant would have
allowed. It never permits one, and hard `block` rules never reach it.

**What it reads, and what it refuses rather than guesses.** An enumerated list,
deliberately — not a closure claim over shell semantics:

| Read | Refused (grant does not apply) |
|------|-------------------------------|
| the working directory | a `cd` **not** joined by `&&` (with `;` the next command runs even if the `cd` failed) |
| `cd <literal> &&` | an indirect runner — `sh -c`, `xargs`, `sudo`, `ssh`, `env`, `find` … |
| git `-C` / `--git-dir` / `--work-tree` | a subshell or group |
| git `GIT_DIR`-family env assignments | command substitution **in a directory-deciding position** (a `cd` target, a `-C`/`--git-dir`/`--work-tree` value, a `GIT_DIR`-family assignment, the segment head, a git config key) — one in an operand such as a `-m` message cannot move the command and is scopeable (#942/#943) |
| the enclosing git **repo root** | `eval`, a base64 pipeline |
| the repo root's **`core.worktree` redirect** (#927) | `-c`/`--config-env` setting `core.worktree` or `include.*` |
| | any **unmodelled** environment assignment (`FOO=1 git commit`) |
| | a rule whose pattern matches no single segment |

Three selectors pick a git repo independently — cwd, `-C`, and
`--git-dir`/`--work-tree` — so all three are read and **all** must be in scope.
`GIT_DIR=<other> git commit` is the same redirection spelled as an environment
assignment, and is read the same way; an assignment naming a variable we do not
model refuses, which is what the `env(1)` spelling already got.

**They do not relate to the cwd the same way**, and reading them as if they did
is a bypass rather than an inaccuracy (verified against git 2.50.1):

- **`-C` is cumulative.** `git -C a -C b` chdirs to `a`, then to `b` *relative
  to `a`* (`git -C outer -C inner rev-parse --show-prefix` → `inner/`); an
  absolute value resets the chain. Resolving each `-C` against the cwd instead
  collapses `git -C <in-scope> -C ../..` onto the in-scope directory, so the
  check sees one in-scope target and grants while git walks out of it — into
  the enclosing repo, which is exactly where `~/.claude/projects/*/memory/`
  sits relative to `~/.claude`.
- **`--git-dir` / `--work-tree` are last-one-wins**, and a relative value
  resolves against the directory the `-C` chain produced, not the cwd.

So the chain is folded first and everything else — including the `GIT_DIR`-family
assignments — is measured from its result.

The **repo root walk-up** matters because git resolves its repo by walking *up*
from the working directory, so the directory a command runs in is not
necessarily the repo it writes to. A scope naming `<repo>/subdir` would
otherwise grant over all of `<repo>`. This is checked at decision time rather
than by refusing non-root scopes at lint time: a lint sees only the scope as
written, and `~/.claude/projects/*/memory/` is perfectly safe right up until
someone runs `git init ~/.claude`.

**Symlinks.** Both sides are resolved before comparing: the candidate directory
must be in scope in **both** its lexical and its `realpath` form, and the
scope's literal prefix is resolved too (so a scope rooted behind a symlink still
admits its own contents). This is load-bearing rather than theoretical — `ln
-s`, `mkdir`, `git init` and `git worktree add` are all allowed unattended, so
the grantee can write inside the scope. **Treat "the grantee can write inside
the scope" as the threat model, not a cooperative caller.**

**`core.worktree` redirects (#927).** A repo can be redirected from inside its
own config (`git config core.worktree <elsewhere>`), which no reading of the
command can see. Scope evaluation therefore reads the resolved repo's config
and measures the redirect target against the scope like any other selector —
so a redirected in-scope store **refuses the commit** even though the command
itself stays entirely within scope. The command-line spellings of the same
redirect (`-c core.worktree=…`, `--config-env`, and the `include.path` /
`includeIf.*` keys that can pull one in from an arbitrary file) refuse
outright. The redirect *command* itself is still unruled — making it
`ask`-tier is rule-file work, tracked in #927.

**One limit that is NOT closed**, stated rather than implied: resolution is a
**TOCTOU window** — the hook validates a path the command has not used yet.

**MCP tools.** The MCP hook's command is *synthesized* from the tool call
(`agentwire email --to …`) and names no directory, so a **scoped** grant there
refuses rather than measuring the scope against the session cwd — which would
allow on a coincidence. Unscoped grants are unaffected.

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
resolve through the same `resolve_unattended_grants`).

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
| **DB raw writes** | `psql`/`mysql` executing INSERT/UPDATE/ALTER/CREATE/GRANT, `mongosh` insert/update/delete | ask | `payloads.yaml` (`db.psql-write`, `db.mysql-write`, `db.mongosh-write`) |
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

**Socket options no longer bypass this (#919).** Every session on this machine
runs on a *named* socket, so `tmux -L agentwire kill-server` was the spelling
that mattered — and it was allowed, along with the attached (`-Lagentwire`) and
bundled (`-2Lagentwire`) forms that tmux's getopt parsing also accepts. All
three now normalize to `tmux kill-server` before matching. See
[Global options before the subcommand](#1a-global-options-before-the-subcommand-913-919).

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

### Remote Execution Safeguards — the wrapper-payload rescan (#924)

`ssh <host> "<payload>"` is a command wrapper, and since #924 the engine treats
it as one: the remote command is extracted (`_ssh_remote_payload`, ssh's
option grammar from the OpenSSH manual, nesting bounded) and **re-scanned as a
command in its own right**, so every rule — anchored ones at real command
positions — applies over ssh automatically, and the refusal carries the
payload's OWN rule id. Measured on the 151-form dangerous corpus: 150/150
wrapped forms refused. Never write an ssh twin of a local rule; `remote.yaml`
keeps only the ssh-ONLY surface (reboot / shutdown / `systemctl stop`, which
have no local rule, plus a deliberately-stricter `docker rm -f` block).

The same principle covers two siblings:

- **DB clients** — `psql -c` / `mysql -e` / `mongosh --eval` joined the
  exec-surface table: the quoted statement is emitted as payload text the
  unanchored SQL rules read even after masking.
- **`git -c` exec keys (#921)** — a subset of git config keys name a program
  git will run (`core.sshCommand`, `core.fsmonitor`, `core.pager`,
  `credential.helper`, `alias.x=!…`, `filter.*.clean/smudge`, …). The value is
  re-scanned as a command (a dangerous payload blocks under its own rule) and
  the operation itself is ask-tier via `git.config-exec-key`, with a
  block-outranks-ask guarantee in the decision ladder so rule-file load order
  can never demote a hard block to a confirm.

### The unverifiable tier (#934)

Two populations used to share the `ask` fallback, and under the default bypass
posture "fails closed" was ALLOW. They now resolve differently
(`ambiguity_conceals_verb`, judged by position, not presence):

| shape | example | bypass | unattended |
|---|---|---|---|
| operand substitution | `echo "$(basename $p)"` | allow (demoted, as before) | **allow** (#925 Part 3 — every rule already scanned the masked form) |
| verb concealment | `psql -c "$(cat x.sql)"`, `eval …`, `base64 -d \| sh`, `$(which x) --prod` | **ask — not demoted** | block, no grant applies |

Verb concealment also outranks an ordinary ask-rule match, so a granted id
(`uv run $(echo rm) -rf`) cannot compose with a substitution into an
unguarded run.

**Data is not syntax: the inert view.** The ambiguity analysis used to
tokenize *everything*, so an apostrophe in a commit-message heredoc read as
"unbalanced quotes", its parentheses as a "subshell", and a `\`` inside double
quotes as an unbalanced substitution — an ordinary multi-line commit or
`python3 - <<'EOF'` earned a confirm even under `auto`. `inert_view()`
neutralizes exactly the spans the shell treats as literal — the body of a
**quoted-delimiter** heredoc (`<<'EOF'`, `<<"EOF"`, `<<\EOF`) and backticks /
`$(` inside single quotes or backslash-escaped — **for the ambiguity analysis
only**. Rule matching still reads the raw command, so a protected path in a
heredoc body keeps blocking. What stays unverifiable on purpose: an unquoted
heredoc (its body expands `$(…)`), an unterminated heredoc, a marker that is
not a redirection (inside a string, a comment, after `\`, in arithmetic — it
opens no heredoc, and hiding what follows would hide a real `eval`; see the
marker-liveness bullet above), and a heredoc that **feeds a shell** (`bash <<'EOF'`, `ssh host
<<'EOF'` — the body is the program). A *real* backtick or `$(` is unchanged. The
`git commit -m "$(cat <<'EOF' … EOF)"` idiom keeps its operand-position
substitution ask, which bypass/auto demote — it is just no longer misread as
verb concealment. `tests/unit/test_damage_control_inert_view.py` pins both
halves; each guard has a mutation that turns it red.

**The dynamic-evaluation builtin is judged by position (#1115).** The old
check was a whitespace-keyed word search, so it guarded the phrasing: any
commit message, issue/PR body, grep pattern, `agentwire msg`/`send` payload or
quoted heredoc that merely NAMED the builtin was an un-demotable
`unverifiable` ask (a silent block loop when unattended), while spellings the
regex could not see (`\\eval`, `e''val`, `"eval" x`, `bash -c 'eval "$x"'`)
passed. `has_command_position_eval()` now reads the resolved token view (quote
and escape variants normalize; `$'…'`/`$"…"` lose the `$`), after joining
backslash-newline, with compound-command syntax removed by the shared
`strip_command_openers` (#1112 — there is no second chain walker). A command's
verb is the first word after redirects (`>f`, `2>&1`, `> f`), assignments and
launcher wrappers.

- **Concealing:** the builtin as the verb, after any separator, compound
  keyword, case arm, function body, `builtin`/`command`/`sudo`/`env`/`time`/
  `nohup`/`xargs`/…; inside a `<(…)`/`>(…)` body; and as the verb of program
  text the shell is FED and re-checked as a command in its own right: a
  `bash`/`sh`/`su`/`runuser -c` payload, `ssh host …`, `env -S`, `watch`,
  `trap '…'`, `tmux new-session|run-shell|…`, `find -exec|-execdir|-ok|-okdir
  <vector>`, a heredoc/here-string/`echo … |` feeding a shell (quoted or not,
  directly or via a pipe, incl. `| xargs sh -c`), and `source <(echo …)`.
- **Data:** the word as an argument (`echo`, `grep`, `git commit -m`, `gh
  --body`, `agentwire …`, `tmux send-keys`, a quoted heredoc body not fed to a
  shell, a comment) and lookups (`command -v`, `type`, `which`, `help`). A data
  mention never shields a real call elsewhere in the same command.
- **Option values:** only the KNOWN value-taking options of `sudo`, `doas`,
  `env`, `xargs`, `timeout`, `nice`, `ionice`, `stdbuf`, `time`, `watch`,
  `exec` swallow the next word, so boolean flags (`sudo -E`, `env -i`, `xargs
  -0`, `nohup --`) no longer hide the verb or fabricate one. Launchers outside
  that table (`uv run`, `npx`, …) keep the greedy rule.

Residuals, stated plainly: command-position use is *always* unverifiable (a
literal operand is not re-scanned as a command); an UNKNOWN launcher's option
whose value is the word errs closed; a call inside `$(…)` is judged by the
substitution path, not this one; **not detected:** the verb produced
indirectly (`alias a=eval; a x`, `x="eval y"; $x`, `$1 x`, `e${x}val`, `$'\\x65val'`),
and program text from anything but `echo`/`printf`/heredoc/here-string
(`cat file | sh`). `tests/unit/test_damage_control_eval_position.py` is the
differential (spelling × position × wrapper × consumer, plus a committed
corpus of executing spellings that must never be `allow` — also unattended, and
under size padding — and a corpus of data mentions that must).

### Tool-channel parity (#923)

Damage control is registered per tool matcher, and a guard that asks "did this
arrive as Bash" misses the same operation arriving as a tool call. Coverage
now: `NotebookEdit` routes through the edit hook (`notebook_path` is a file
write), and **every** `mcp__*` tool call has its path-valued arguments
screened — zero-access secrets block on mention for any tool; protected
control-plane paths block for write-shaped tool names. Full rule-level
per-tool policy (classifying each MCP tool's tier) is #923's remaining design
work.

---

## Outcome logging: what happened after the decision (#1068 Phase 0)

The PreToolUse audit row records the decision (`allowed` / `asked` / `blocked`), never what followed. Tuning rules needs the label: did the human approve the prompt, did the call run, did Claude Code's own classifier refuse it. Those facts only reach hooks that fire after the decision, so `hooks install` also registers `outcome_logger.py` for `PostToolUse`, `PostToolUseFailure` and `PermissionDenied`.

- **Join key:** decision rows now carry `tool_use_id` and `permission_mode`; outcome rows (`kind: "outcome"`, `result: executed | failed | denied`) carry the same `tool_use_id` and are appended to the same daily JSONL.
- **Content-free by design:** outcome rows hold identifiers, result, timing and the payload's field names (`payload_keys`, a discovery aid for what this Claude Code version sends). No command, tool input or tool output: the command is already in the decision row, and tool output can hold anything. A `PermissionDenied` row keeps at most 200 characters of the denial reason.
- **Never in the way:** the hook prints nothing, swallows every error and always exits 0.
- **Labels** (`agentwire.safety.audit.join_outcomes`): `ran` (allowed, executed), `approved` (asked, executed), `declined-or-abandoned` (asked, nothing ran), `rule-blocked`, `classifier-denied`, `failed`. Rows written before this existed have no `tool_use_id` and stay unlabelled.
- **Read it:** `agentwire safety report` appends an "Outcomes" section; `--json` includes the per-rule label counts.
- **Re-rank by rule:** the same report adds "Friction by rule" (per rule: approved, declined, blocked, and `approve_rate` = approved / answered prompts; a busy rule at rate ~1.0 is the next loosening candidate, ~0 means it earns its keep) and a "Per day" line so a rule change can be judged before/after. Needs a few days of rows; the join only covers decisions logged since #1074.
- **Scope:** registered with exactly the damage-control matchers (`DAMAGE_CONTROL_MATCHERS`, joined with `|`), so it runs only after tools damage control already decided on. `hooks install` narrows an older catch-all registration in place.

Retention is not bounded and there is no copy outside `~/.agentwire/`: a wipe of that directory erases the history (it did on 2026-10-11). See #1068 for the follow-ups.

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

# Summarize blocks by tier / rule / session over a window (#940 measurement)
agentwire safety report --days 14 [--json]
```

**Audit Log Format**:
```json
{
  "timestamp": "2026-04-30T13:45:22Z",
  "session_id": "agentwire-dev/damage-control",
  "conversation_id": "2f9c…-uuid",
  "agent_id": "wave-2-task-1",
  "tool": "Bash",
  "command": "rm -rf /tmp/test",
  "decision": "blocked",
  "blocked_by": "bashToolPattern: rm with recursive flags",
  "pattern_matched": "\\brm\\s+-[rRf]"
}
```

**Attribution (#940 prerequisite):** `session_id` is the agentwire session
name and `conversation_id` the Claude conversation UUID from the hook stdin
payload (the identity #871 records). Resolution: `AGENTWIRE_SESSION_ID` env →
tmux (`#S` of the pane the hook runs in) → scan of
`~/.agentwire/sessions/*/metadata.json` `conversation_ids` chains. Fail-open by
design: attribution never blocks an action or crashes the hook; when nothing
resolves the row carries the pre-#940 `"unknown"`.

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
- Declare `anchored:` on every rule — `true` for a command-prefix rule, `false`
  if the danger arrives inside another command's quoted payload. The matcher
  defaults to unanchored (fail-safe), which means a rule that omits it silently
  reintroduces #915. See [`anchored`](#anchored--command-position-vs-argument-content-675-915).
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

## Deployment: merged is not deployed (#936 + #916)

`heal_damage_control` is the one function that puts rules, tooldefs and hook
scripts onto a machine, and it used to fail in **opposite directions** for the
two halves it manages:

| | old mechanism | failure |
|---|---|---|
| hook scripts | `copy2` whenever bytes DIFFER, no ordering | went **backwards** freely |
| rules / tooldefs | `if not target.exists()` | never went **forwards** at all |

So "install brings things up to date" was wrong in both directions, differently
— which is worse than wrong in one, because it made the outcome unpredictable.
Both were observed, not theorised: a worktree on a pre-#918 checkout reinstalled
the old `git -C` bypass machine-wide within an hour of the P0 being deployed and
verified; and 9 of 15 live rule files had never been updated since install, so
40 bundled patterns and 14 anchors were simply not in force.

### Three properties, each a separate guard

**1. Provenance — WHICH package may write.** Machine-global files are written
only from the canonically installed tool (`agentwire/safety/provenance.py`).
Four states: `canonical` (writes), `bootstrap` (no install exists — writes, or
the tool would be uninstallable), `worktree` (no install and the source is a
linked worktree — refuses), `foreign` (an install exists and is not what is
running — refuses). Override: `--allow-foreign-source`.

Deliberately **not** keyed on the cwd: `~/.local/bin/agentwire` is a console
script with a venv shebang, so it imports site-packages regardless of where it
was invoked from. Running `hooks install` *while sitting in* a worktree was
always safe; `uv run agentwire` *from* a stale checkout never was. A cwd check
gets both backwards. Nor on `PATH` — `uv run` puts an ephemeral venv first, so a
`which`-based lookup reports the stale checkout AS the canonical install.

**And there is no environment override**, deliberately. An earlier draft had
`AGENTWIRE_CANONICAL_PACKAGE`; it was the wrong shape twice — it duplicated
`--allow-foreign-source` with something undocumented, and a leading `VAR=value`
assignment is collapsed to a mask token by `masked_subcommands`, so a
command-position damage-control rule **cannot observe it being set**. An
override invisible to the layer guarding machine-global writes is this whole
section's failure mode one level in, and the threat model (an agent on a task
branch) sets inline env vars routinely. The flag is an argument in command
position and stays visible. Tests patch the resolver function instead.

**`rebuild` carries the same guard**, and it is the one that matters most. It is
the only installer-adjacent command that *changes the answer* every other
provenance check reads — it reinstalls the tool FROM a source checkout, so a
worktree it installs from **becomes canonical**:

```
uv run agentwire hooks install   (from a worktree)  -> refused
uv run agentwire rebuild         (from a worktree)  -> worktree is now canonical
agentwire hooks install                             -> proceeds, legitimately
```

Guarding the heal alone blocks the one-step and permits the two-step — and the
second step is what the Dev Workflow tells people to run after a code change.
The refusal is bound to `--allow-foreign-source`, **not** to `rebuild --force`,
which means "rebuild despite being behind `origin/main`": folding them together
would make a documented staleness override silently grant a machine-global one.
(Distinct from the behind-main check, which says nothing about a worktree that
is *ahead and divergent* rather than behind.)

**2. Ordering — not equality.** The five generated hooks carry a stamp emitted
by `scripts/regen_damage_control_hooks.py`:

```python
AGENTWIRE_HOOK_STAMP = {"core_sha256": "…", "generated_at": "2026-08-06T…Z"}
```

`generated_at` moves only when the inlined `_core.py` actually changes, so the
stamp is a property of the SOURCE TREE and orders two checkouts. Drift states
become `ok | missing | older | newer | stale`, and a `newer` installed copy is
never overwritten without `--force`. Installs also write with a **fresh mtime**
— `copy2` preserving the source mtime is what made the original downgrade
invisible to any timestamp check.

Ordering is **prospective**: an install that predates the stamp reports `stale`
(differs, unorderable) rather than a direction. Direction becomes available from
the first stamped deploy onward. Provenance is what protects the interval.

**3. Three-way sync — not install-missing-only.** Rules and tooldefs are
host-editable, so a blanket overwrite is off the table. The missing leg of a
three-way merge is the common ancestor, and `agentwire/safety/rule_baselines.json`
ships it: the sha256 of **every version of each file that has ever shipped**,
generated from git history by `scripts/gen_rule_baselines.py`.

```
live bytes == bundled        -> ok
live sha256 in the manifest  -> outdated  : a pristine older release, updated
live sha256 in neither       -> unknown   : reported, left alone, --force replaces
```

`unknown` is named for what it is. It is *usually* a hand edit, but it is also
what a file older than our recorded history looks like, and nothing here can
tell those apart — so it is classified explicitly rather than defaulted into
either bug. `--force` replaces it and keeps a `.local-<ts>.bak`.

Content-addressing is what makes this work for machines installed **before the
mechanism existed**, which is every existing machine. A manifest written at
install time would answer nothing for them.

### A tooldef refresh is a PERMISSIONS CHANGE

Say this out loud, because it reads as a chore and lands as a grant. Tooldefs
carry the pinned `id:` fields that `DEFAULT_UNATTENDED_ALLOW` names. On a machine
whose tooldefs had drifted, five of six default grants matched **nothing**, so
they had never been in force. Repairing the drift does not "restore" the
default — in practice it **grants** it: `git add`/`commit`/`push` and
`gh pr create` permitted unattended in ANY repo. `heal` prints the notice
whenever tooldefs change; narrow it with `unattended_allow` in
`~/.agentwire/damagecontrol.yml` (path-scoped entries supported, #914).

### Duplicate rule ids

`_assign_rule_id` honours an explicit `id:` verbatim — pinning is the point, and
renaming one would silently break the `disabled_rules` / `unattended_allow`
entry naming it. So collisions are possible, and never benign: two live rules
sharing an id makes both knobs ambiguous. `load_config` records them under
`_duplicate_rule_ids`; `doctor` and `safety status` report them.

It is also **the only detector for a partially-applied rule set** — a new
bundled file landing beside a stale one that still carries the same pinned ids
is otherwise invisible. Detection only: an ambiguous id does not change matching
or grant resolution, because a wrongful refusal in the unattended tier is a
silent loop with no screen to show it on.

### What doctor can now say

- `DC hook scripts OLDER than this package` / `... NEWER than this package` —
  the second is the #936 signal and was structurally unsayable before.
- `Damage-control rules are OUT OF DATE (a previously shipped version)` at
  `[!!]`, not `[..] differ from bundled` — drift that REMOVES protections is a
  different event from a local tweak.
- `unrecognized AND MISSING bundled protections`, **naming** what is gone;
  local additions only stay `[..]`.
- `Running from a NON-CANONICAL package — installs are refused`.
- `DUPLICATE rule ids in the loaded rule set`.

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

Hooks are registered with `timeout: 30` and the scan decides within 10 s (see *A hook that times out FAILS OPEN*). A hook registered without a timeout was measured to run for more than 100 s before the harness kills it. If your rule files are very large or patterns are complex, you may hit the scan budget and get an ask.

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

### A hook that times out FAILS OPEN (#1108)

Measured with `claude -p` (2026-10-16): a `PreToolUse` command hook registered
with `timeout: 2` that sleeps 8 s is killed at the timeout and the Bash call
**runs anyway**; the same registration with a hook that exits 2 immediately
blocks it. The harness treats a killed hook as "no decision". So a command that
stalls the scan is a bypass of the whole ladder, not just a slow call.

The scan therefore has a budget that fails **closed** (`check_command`):

| Axis | Limit | Verdict |
|------|-------|---------|
| Command size | 256 K chars | `ask`, id `core.command-too-large` |
| Longest single line | 32 K chars | `ask`, id `core.command-too-large` |
| Scan clock | 10 s (`SIGALRM`) | `ask`, id `core.scan-budget` |

The verdicts are `unverifiable` asks: bypassPermissions/auto do not demote
them, an unattended run blocks them, and the message names the cause. Limits
come from the audit log of real commands (7.7 k rows: longest command 28.9 KB,
longest line 2.9 KB), leaving ~9x / ~11x headroom. The cap counts PHYSICAL
lines: backslash-continued lines are not joined, so 240 K of continued lines is
scanned and bounded by the clock alone.

**Every hook entrypoint fails closed.** A hook that crashes exits 1, which the
harness treats as non-blocking. `read_hook_input` / `run_hook_guarded` (shared
by all five hooks) turn malformed or oversized (> 64 M chars) stdin, a
non-object payload, `RecursionError`, an escaped alarm and any other exception
into exit 2 with a reason. The MCP hook additionally walks arguments
iteratively, screens each distinct string once, and runs the screening under
the same 10 s deadline (over it: an unverifiable `ask`, id `core.scan-budget`).
Edit/write/read never scan `content` / `old_string` / `new_string` (50 MB
payloads pass in ~0.1 s); only the path is checked.

**An audit-log failure never changes a decision.** Every `log_*` call in all
five hooks goes through `audit_best_effort`: a full disk or an unwritable
`logs/` dir prints one short stderr line and the computed decision stands. An
allow stays an allow, and a genuine block keeps its real reason (not the audit
error). Only a failure of the DECISION machinery fails closed.

**Exit code is the decision code.** `run_hook_guarded` ends with `hard_exit`
(`os._exit` after a guarded flush), so closed pipes cannot turn a block into the
interpreter's exit 120. If stdout cannot be flushed the decision cannot be
delivered, so the exit becomes 2; a dead stderr is ignored.

**The kill switch and the budget.** `enabled: false` in `damagecontrol.yml` is
read first (`kill_switch_off`) and turns the budget off with everything else: no
size gate, and a scan or MCP screen that outruns the deadline is allowed
(disabled), not asked, so a disabled hook no longer stalls unattended tasks
with big commands. The control plane is exempt from that: what runs BEFORE the
switch by design (protected paths and commands) still blocks while disabled.
The guarantee for a PADDED command is "refused or asked", never allowed: when
the deadline fires, `mentions_control_plane` (a linear, regex-free containment
test over every protected path's literal, its basename, and its `$HOME`/`~`
spellings) decides. If the command names a protected path, only the lines that
name it are re-checked under their own 2 s deadline: a positive write is a
BLOCK, anything unconcluded is the unverifiable `core.scan-budget` ask (bypass
cannot demote it, unattended blocks it), in either switch state. If it names
none, a disabled hook allows and an enabled one asks. The check over-matches by
design (a bare `settings.json` mention counts), which only costs an ask once
the budget is already spent; a padded read of a protected path therefore asks.
The MCP over-budget path follows the same rule: a protected path anywhere in
the arguments asks even with the switch off. A corrupt or unreadable `damagecontrol.yml` is the
existing fail-secure behavior: `enabled` stays true.

**Timeouts.** Hook registration writes `timeout: 30` (`HOOK_TIMEOUT_SECONDS`).
`agentwire hooks install` UPDATES the timeout on existing agentwire-owned
entries and `agentwire doctor` flags `[!!]` any registered matcher whose
timeout is missing or differs from 30 (so also one below the deadline plus
margin). The installer OVERWRITES a user-tuned value (2, 5, 120, `"30"`, 0,
`true`): this is an upgrade, not a preference it respects. A malformed
`settings.json` (`hooks: []`, a non-list `PreToolUse`, non-dict entries) is
skipped entry by entry in doctor and left untouched, with a warning, by the
installer; neither crashes. A hook registered with NO timeout was measured not
to be killed at 100 s, so the harness default is > 100 s, far above the 10 s
scan deadline.

**Re-measuring on a new Claude Code version.** In an empty temp dir, put a
project-local `.claude/settings.local.json` registering a PreToolUse Bash hook
(`"timeout": 2`, script sleeps 8 then exits 0; variant: no `timeout`, sleeps
100 then exits 2), run `claude -p "run exactly this bash command: touch
SENTINEL" --permission-mode bypassPermissions --allowedTools Bash`, and check
whether `SENTINEL` exists. A control hook that exits 2 at once must block.

Speed: the protected/zero-access path patterns are prefiltered on the path's
required literal and `_PATH_ODDITY` is anchored at token starts. This makes
long UNQUOTED tokens linear (10 k chars: 9.8 s -> 0.2 s) and plain quoted
strings linear (30 k chars: 0.2 s). Quoted strings full of path-like text are
still super-linear (30 K of `a/b/` takes ~2 s, a quoted `../` run ~8.5 s); those,
and runs of `\` or `{a,`, are bounded by the clock budget rather than fixed.
A writer verb next to a protected path stays blocked with a long quoted string
before it, after it, or holding the path, balanced or not (pinned; a differential
against main over that slice found no decision differences). Known allow, same
as main: a writer AND the path both inside ONE quoted string (`ln`, `curl -o`)
is data in a quote, not a command.

The read hook has no deadline but is linear in the path length (a 20 MB path
takes ~4 s); edit/write scan only the path.

Declared residuals: the clock budget needs `SIGALRM` on the main thread (every
hook process; not a portal worker thread or Windows, where only the size gate
applies).

### A command inside a compound body is judged like the same command at top level (#1112)

`case x in a|b) rm -rf /tmp/x;; esac` was allowed while the bare `rm -rf /tmp/x`
was blocked, and the same hole covered `if`/`while`/`for`/`{ }`/`( )`/function
bodies, `!`, `coproc`: an anchored rule matches only at a command position, and
the position chain stopped at the first non-wrapper word (`case`, `then`, `{`,
`a|b)`), and the ssh/global-option/concealment walkers each had their own copy of
that blind spot. **Guaranteed now, in ONE place:** `strip_command_openers` (and
the plain-word twin `strip_command_opener_words`) drops compound syntax from the
front of every command before any walker runs, so the body command is seen
exactly as at top level. It follows bash: the keywords (`if then else elif while
until do ! time coproc function`) and `{` are reserved only as WHOLE words
(`{rm`, `do{` stay plain words), while `(` and `)` always break words, so
`(rm`, `then(`, `!(`, `f(){`, `f ( ) {`, `a)rm` open a command position glued or
spaced. A `case` header is dropped through the `)` that ends the first arm's
pattern list, a bare `pat)` at segment start covers later arms, and `time -p`
options go with `time`. Pattern words are never match starts, so a verb in an
arm pattern is data. Wired into: the masked-entry builder (anchored rules, ssh
and git-config payloads, global-option stripping), `ambiguity_conceals_verb`,
`_stage_reads_program` and the prose-blanking runner check; the last two have no
behavioural repro of their own (other layers already agree) and are wiring only.
`_CD_RE` (directory tracking for control-plane writers) is a regex over raw text,
so it carries the same opener list plus `time`/`nohup`/`env`/`sudo`/`VAR=1`
prefixes. The unattended scope evaluator refuses groups outright (fail closed).
Pinned by a differential (command x 40+ wrappers, decision equal to top level, in
all four kill-switch x unattended states) in
`tests/unit/test_compound_command_positions.py`, plus must-allow controls.

**Declared residuals** (both pre-existing, both the false-BLOCK direction): a
quoted multi-word case pattern with the verb inside (`case x in 'rm -rf x') …`)
is read as a payload-bearing word and blocked; and a glob like `[a-z]*` in the
same command as a pipe into a shell can expand onto a control-plane name
(`echo [a-z]*; echo hi | sh` blocks at top level too). Neither is
wrapper-specific.

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
