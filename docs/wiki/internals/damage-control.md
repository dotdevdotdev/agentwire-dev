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

Damage control is its own package, a uv-workspace member the `agentwire` distribution depends on
(#1209 M4). Claude Code's `settings.json` runs it in exec form —
`<recorded interpreter> -I -m damage_control.hook <kind> --profile ~/.agentwire/damagecontrol-profile.json`
(`kind` = bash, edit, write, read, mcp). There are no hook scripts to install or regenerate.

```
packages/damage-control-dev/src/damage_control/
├── hook.py                           # bootstrap wrapper: os+sys only, any import failure -> exit 2
├── adapter.py                        # payload -> ToolCall, Verdict -> exit code / ask envelope
├── policy.py                         # Guard.decide: the decision ladder
├── engine.py                         # rules, ladders, loading, kernel
├── profile.py                        # Profile (host data) + JSON manifest dump/load
├── groups.py / packs.py              # groups and packs, read at RUNTIME from the policy file
├── audit.py                          # audit logging
├── tooldefs/                         # tool command catalogues (*.yaml source, *.json read)
└── rules/                            # Pattern files (categorized; *.yaml source, *.json read)
    ├── core.yaml                     # rm, chmod, system-level dangers
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
- `~/.agentwire/damagecontrol-profile.json`, `~/.agentwire/bin/damage-control-breakglass`, `~/.agentwire/damagecontrol-install.json`, `~/.claude/hooks/*`
- `~/.agentwire/damage-control/*.yaml` (the rule files)
- `~/.agentwire/scheduler.yaml`, `~/.agentwire/config.yaml` (gate/healthcheck commands run via the same `shell=True` confused-deputy path)
- any `.agentwire.tasks.yml` (per-project task-execution config — see [Task-execution config split](#task-execution-config-split-agentwiretasksyml-720) below; **not** `.agentwire.yml`, which is pure declarative session config and is agent-writable)

The guarantee: **the agent only ever operates within the freedom the human
preset; it can never expand its own freedom by editing a file.** The one
override is the user's `allowedPaths` allowlist — a human, host-side opt-in (the
agent can't add to it without editing a protected file, which is itself blocked).
The mechanism is `check_protected_command` / `check_protected_path` in
`packages/damage-control-dev/src/damage_control/engine.py`, which run **before** `detect_escape_hatch` and the kill switch.

#### Flagless and indirect writers (#1095)

The generic ladder names writers that carry a write *token* (`>`, `tee`, `cp`,
`rm`, `sed -i`). A writer whose only link to the file is an operand, or whose
path arrives indirectly, is classified by the **operation on the operand**,
whatever the spelling (`PROTECTED_WRITER_PATTERNS`, `INTERPRETER_WRITE_PATTERNS`
in `packages/damage-control-dev/src/damage_control/engine.py`; applied only to the protected tier, never to user
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
  left alone. The embedded-language interpreters (`lua`, `luajit`, `tclsh`,
  `wish`, `R`, `Rscript`, `julia`, `groovy`, `osascript`, `expect`) are in the
  inline-interpreter net too, but read stdin as a program only when handed none
  of their own (`lua -e '…'` and `lua script.lua` take stdin as data).
  `mosh` is `ssh` for this purpose. Text piped or heredoc'd to a *remote*
  interpreter (`… | ssh host python3`, `… | mosh host lua`) is Python/Lua, not
  shell: it is re-scanned as that interpreter's inline program
  (`python3 -c '<text>'`), so the same nets and rules apply as when typed.
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
  heredoc. `<<$"EOF"` / `<<-$"EOF"` is a locale-quoted delimiter: it quotes like
  `<<"EOF"`, so its body is data (#1132). Declared residuals, all scanned rather
  than hidden (a rephrase, not a
  bypass): a `case … esac` pattern `)`
  inside `$(…)` can end that span early; `<<` inside a nested `$(…)` within
  `$((…))` is not walked; `cat <<\EOF | xargs sh -c` (a heredoc body fed to a
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
  scanned. A group count past the cap (256 variants) cannot be enumerated, so a
  command that names the control plane AND outgrows the cap is an unbypassable
  ASK (`core.control-plane-target`, #1132); without a control-plane name it is
  left to the zero-access fail-closed block it already hit.
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
- **Path resolution / cwd lane (#1132).** The directory a relative name is
  read against is resolved from everything the command itself says:
  `d=~/.agentwire; cd $d && touch damagecontrol.yml` (a literal assignment
  earlier in the same command, last one wins, a `$(…)` value makes it unknown),
  `env -C <dir>` / `env --chdir=<dir>` and `patch -d <dir>`, next to `cd` and
  `git -C`. The `cd` chain regex accepts whitespace inside a prefix assignment
  (`A=${x:-\n} cd <dir> && …`, `A="a b" cd <dir>`). A command inside
  `sh -c '…'` / `ssh host '…'` is re-scanned as a command in its own right
  (`_wrapper_payload_commands`), so the same resolution applies inside the
  wrapper — the decision equals the top-level one (payload found through
  `timeout 5`, `sudo -u x`, `env -C d`, `--`, `ssh host -t`, and `\$` inside a
  double-quoted payload). Redirects on the `cd` (`cd d >/dev/null 2>&1 &&`) do
  not hide the directory; `env -iC` clusters and nested `env -C a env -C b`
  resolve; a relative `cd` with no known base (`cd ..`) is unresolved. The
  unresolvable-`cd` ask is evaluated against the control-plane allowlist, so a
  default `/tmp/*` allowance cannot waive it. The `patch`/`git apply` read-only
  flags excuse only the command they sit in, and the verb is matched by basename.
  An existing alias reached through a literal `cd <dir> &&` is resolved against
  that directory too (`_cwd_realpath_variants`: `cd /tmp/d && echo x > lnk`).
  Not covered: a bare `cd`, or a `cd` into a directory the scanner cannot read.
- **A `cd` the scanner cannot resolve** (`cd -`, `popd`, `cd $x` with `x` not a
  literal assignment) followed by a distinctive protected NAME that the rest of
  the command would write (`cd - && touch damagecontrol.yml`) is an ASK that
  bypass does not demote (`core.control-plane-target`, `unverifiable`;
  unattended: a block unless the id is granted). Each directory that holds the
  name is tried as the unknown directory and the text after the `cd` is judged
  as the refused write it would be there. `settings.json` and `config.yaml`
  are not "distinctive" (ordinary files everywhere else): `cd $x && sed -i …
  settings.json` stays allowed. Reads stay allowed.
- **`patch` / `git apply` / `git am` run in a protected directory** (`cd
  ~/.agentwire && patch -p0 < x.diff`, `git -C ~/.claude apply x.diff`, `patch
  -d <dir>`; also `~` itself, where a diff names `.agentwire/…`) ask: the files
  they rewrite are named only inside the diff. `--dry-run`, `--check`,
  `--stat`, `--numstat` and `--summary` are reads and stay allowed. Only a
  literal directory counts: the session cwd and a project's `.damagecontrol.yml`
  directory are not enumerable.
- **`rsync` writes its destination only** — the last operand, options may trail
  it. `rsync -a ~/.agentwire/damagecontrol.yml host:x` reads the file and is
  allowed; `rsync -a src ~/.agentwire/damagecontrol.yml` is refused.
- **Aliases.** `ln` / `link` naming a protected file or one of its directories
  on EITHER side is refused (a symlink or hardlink is how a later
  `echo x > /tmp/l` reaches the file outside the scan's view); a link to a
  directory that holds no protected file (`ln -s ~/.agentwire/sessions /tmp/d`)
  is allowed. At hook time every path word is resolved against the filesystem
  (`_realpath_variants`: a symlink anywhere in the word, a symlinked directory,
  or a hardlink to a concrete protected file) and a word that really lands on the
  control plane is scanned as that file, so an alias made by an earlier command
  or turn is judged by where it goes (reads stay allowed). At most 64 words
  per command are resolved.
- `xargs -I{}` / `parallel` substitute into an interpreter literal
  (`… | xargs -I{} python3 -c 'open("{}","w")'`); because the interpreter net is
  fail-closed for any inline program naming the path, an inline *read* of it is
  refused the same way it is unwrapped.

**Run-time-assembled paths fail closed on SHAPE (#1065).** The program text is
taken ONLY from an inline interpreter's `-c`/`-e` argument (`php -r`;
`python/node/perl/ruby/lua`) or a heredoc fed to one — never the rest of the
command line. A write/exec sink whose path expression is not one plain literal
AND itself contains a control-plane word (`.agentwire`, `.claude`,
`damagecontrol`, `settings.json`) asks (`core.control-plane-target`,
unverifiable — bypass does not demote it). Sinks: `open` / `io.open` /
`codecs.open` / `os.open` / `File.open` / `File.new` / `fs.openSync` / `fopen` in a
write (or non-literal) mode, a Perl `open(F, ">…")`, `File.write`,
`writeFile*` / `appendFile*` / `createWriteStream`, `file_put_contents`,
`os.system` / `subprocess.*` / `popen` / `exec*` / `spawn*`, `unlink` / `remove` /
`rmtree` / `chmod`, `shutil.copy*` / `move` / `rename` / `os.replace`,
archive `extractall` / `extract` / `unpack_archive`, and the receiver of
`Path(...).write_text` / `write_bytes` / `touch` / `unlink` / `open(mode)`.
`getattr` / `__import__` / `eval` / `exec` in a program that names the control
plane asks too (the sink is hidden). Calls quoted as data (a fixture, a
message) are not calls.

Not fired: a control word in a comment, a printed string, written data or argv;
read-only use; a literal path (the verb rules decide it). The agent-owned
`.claude/projects/*/memory` and `.agentwire/wiki|artifacts` are exempt
**all-or-nothing**: only when the path expression is ONE static string literal
(optionally the sole argument of `expanduser` / `Path`) whose normalized form lies
inside that directory on a whole path segment (`.agentwire/wiki-evil` is not
wiki). Any other piece — a variable segment, `os.path.join`, `+`, `os.pardir`,
a call, a `chdir` earlier in a program that names the control plane — means the
scan cannot say where the path lands, so there is no exemption and it asks. This
is a shape, not a spelling denylist: `'.\x2e'`, `'.'*2`, `chr(46)*2` and a
`dd='..'` variable all fall out the same way. The price: a routine writer that
builds a memory path from a variable segment asks; write the full path as one
literal (the Write tool is unaffected). Declared residuals: a symlink INSIDE
`memory`/`wiki` that points at the control plane; a control word split across a
variable assigned elsewhere in the program
(`d = Path.home()/'.claude'; (d/'x').write_text(..)`).

Audit replay (7.0 k distinct logged commands): exactly one new ask on the current
code — a scratch-dir script writing `S+"/ks/damagecontrol.yml"`, the shape itself,
by design; no routine memory/wiki writer exists among them (memory is written with the
Write tool). Earlier revisions asked on three: that one, a test-fixture author whose
string data held sink calls, and a probe writer; string-literal masking removed
the data-as-code ones.

Declared residuals (not statically knowable): paths assembled at runtime
beyond that shape (`format`, decoding, `d=$(…); … $d/damagecontrol.yml`, a
component that is only `$var`, a program that names none of the four words), stdin text produced by anything but
`echo`/`printf`/a heredoc (`cat file | ssh host`, `bash -c "$(cat x)"` names no
path until it runs), text that a stdin reader receives through a stage the
scanner does not model (`… | tee x | ssh host`, `… | mosh host`,
`… | ssh host xargs -0 sh`), interpreter source piped to a remote interpreter
(`echo "open('<path>','w')" | ssh host python3` — the text is re-scanned as
shell, not as python), `sftp get`, `ssh host lua` (and
other interpreters outside the list), a `cd` written inside interpreter source
(`perl -e 'system("cd <dir> && touch <file>")'`), an sftp batch read from a
file (`sftp -b file`; stdin batches' `get` / `!` lines ARE judged), a body piped into a
`sh -c '{}'` placeholder (`cat <<'EOF' | xargs -I{} sh -c '{}'` with the literal
path in the body), a directory taken from `$(pwd)` after a `cd` (ask-tier, not
resolved), a cwd known only from the session or set inside a subshell the
command cannot see (an unresolvable `cd` followed by a distinctive protected
name asks instead — above), `make -C`-style directory options of other tools
(`make -C <dir> clean` runs a Makefile nobody can read), a symlink or hardlink
alias created AND written through by the same command from a computed name, a
derived-command budget exhausted by a pathological command, and the TOCTOU
window (see [Closed, fail-closed, user-owned](#closed-fail-closed-user-owned-1065)). Directory-extraction spellings `/.`, `//`, `/sub/..`,
`--one-top-level=` and `bsdtar` are normalized. Pins:
`tests/unit/test_damage_control_bypass.py` (must-block per verb *and* must-allow
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

1. **`PROTECTED_COMMAND_PATTERNS`** (`packages/damage-control-dev/src/damage_control/engine.py`) blocks the command
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

## Moved: rule authoring

Rule authoring (patterns, anchoring, global options, tooldefs, groups and packs) moved to [`packages/damage-control-dev/docs/rule-authoring.md`](../../../packages/damage-control-dev/docs/rule-authoring.md).

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
| 3. `DEFAULT_UNATTENDED_ALLOW` | `packages/damage-control-dev/src/damage_control/engine.py` | Built-in: `git.add`, `git.add-u`, `git.commit`, `git.push`, `gh.pr-create`, `outbound.agentwire-email` — work + open a PR + notify the owner by email |

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
outright. The redirect *command* itself is `ask`-tier too (#1133):
`git config core.worktree|core.hooksPath|include.path|includeIf.*.path <value>`
(`git.config-set-redirect-key`; reads, `--unset` and `--list` pass).

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
- The same package entry as the other four (`python -I -m damage_control.hook mcp`).

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
verb concealment. `tests/unit/test_damage_control_bypass.py` pins both
halves; each guard has a mutation that turns it red.

**The dynamic-evaluation builtin is judged by position (#1115).** The old
check was a whitespace-keyed word search, so it guarded the phrasing: any
commit message, issue/PR body, grep pattern, `agentwire msg`/`send` payload or
quoted heredoc that merely NAMED the builtin was an un-demotable
`unverifiable` ask (a silent block loop when unattended), while spellings the
regex could not see (`\\eval`, `e''val`, `"eval" x`, `bash -c 'eval "$x"'`)
passed. `has_command_position_eval()` reads the resolved token view (quote and
escape variants normalize; `$'…'` is decoded, `$"…"` loses the `$`), after
joining backslash-newline, with compound-command syntax removed by the shared
`strip_command_openers` (#1112 — there is no second chain walker). A command's
verb is the first word after redirects (any bash redirect form, with or
without a glued target, `N`/`{var}` prefix, `&>`, `>|`, fd dups), assignments
and known launchers.

**The failure direction is ask.** A command word falls in one of four classes:

- **known launcher** (`_EVAL_WRAPPER_VALUE_OPTS`: `sudo`, `doas`, `env`,
  `xargs`, `timeout`, `nice`, `ionice`, `stdbuf`, `time`, `watch`, `exec`,
  `nohup`, `command`, `builtin`, `setsid`, `uv run`, `uvx`, `npx`, `bunx`,
  `pnpx`): its value-taking options are skipped with getopt cluster semantics
  (`sudo -Eu root`, `xargs -0I{}`), boolean flags are not, and the next word is
  the verb. The table only **reduces** false blocks; an option missing from it
  leaves its value in the verb slot, which is UNKNOWN and asks. `command
  -v|-V|-pv` and `type`/`which`/`help` are lookups;
- **shell** (`sh bash zsh dash ksh`, and the `su`-style `su runuser script sg`):
  the payload is found with the shell's own option grammar (`-o|+o|-O|+O NAME`,
  `--rcfile|--init-file F`, clusters like `-ec`/`-eo pipefail`, `--`), and if
  the walk stops on an option shape it does not model (`-O3`, `-`), any later
  `-…c` cluster or `--command` names the payload anyway. The payload is
  re-checked as a command; one that expands a variable assigned the word in the
  same command (`p='eval x'; sh -c "$p"`, `y=$x`) asks, and so does one whose
  command position is a positional parameter (`sh -c '$1' _ eval`) when an
  argument is the word;
- **known pure data consumer** (`_EVAL_DATA_CONSUMERS`: `echo`, `grep`, `git`,
  `gh`, `agentwire`, `say`, test runners such as `jest`/`vitest`, interpreters,
  …) and **project runner** (`_EVAL_PROJECT_RUNNERS`: `make just cargo npm pnpm
  yarn fd go bundle`): the rest is data (`make eval`, `cargo test eval`, `npm run
  eval`, `npx jest -t eval`). A runner falls back to UNKNOWN as soon as its
  arguments can hand a program to a shell (a shell name, `SHELL=`); `exec`/`dlx`/
  `x` re-check the launched command (`pnpm exec vitest -t eval` is data, `pnpm exec
  sh -c '…'` is a shell payload), and `--call=` is a shell payload. A few
  data consumers have executing forms that ARE modelled: `sed` (`e` command after any
  address form incl. `!`, in `-e`/`--expression`/`-f -` scripts, and the `s///e`
  flag, when the script holds the word) and `git` (`rebase --exec|-x`,
  `bisect run`, `submodule foreach`). Package, service and resource verbs take the
  word as a NAME (`_EVAL_DATA_SUBCOMMANDS`: `uv add`, `brew install`, `pip install`,
  `systemctl restart`, `kubectl get`, `helm template`, `docker build`, …); `docker
  run|exec`, `kubectl exec`, `docker compose run` are not in it and keep asking;
- **anything else is UNKNOWN** (`flock`, `chroot`, `strace`, `parallel`, a tool
  this file has never heard of): the bare word asks, and so does any quoted
  multi-word argument whose own verb is the word.

- **Concealing:** the builtin as the verb, after any separator, compound
  keyword, case arm, function body or launcher chain; inside a `<(…)`/`>(…)`
  body; a head `$var`/`${…}` holding the word (`${x:-eval} y`) or a variable
  assigned it earlier in the same command (`x=eval; $x y`) or a `for`/`select` variable iterating a list that holds it (brace lists
  are expanded, bounded; `for c in {eval,ls}; do $c x; done`), or a variable set by
  `read`/`mapfile`/`readarray` in a command that holds the word (`read c <<< eval;
  $c x`, `while read c`, `read -a a`, `${a[0]}`), when the body uses it at command
  position, as a payload or as shell-fed text; and as the verb of
  program text a shell is FED: a `-c` payload, `ssh host …`, `env -S`, `npx
  -c`, `watch`, `trap`, `tmux new-session|run-shell|…`, `find -exec|-execdir|
  -ok|-okdir`, a heredoc or here-string feeding a shell, the `echo`/`printf`
  text or here-string of ANY earlier stage of a pipeline ending in a shell —
  through `tee`, `cat`, `sed`, … and inside `for`/`while`/`if`/`case`/function
  bodies, as written and escape-expanded — `xargs sh -c`, and the body of a
  `<(…)` handed to a shell or `source`/`.`, and a make/just file read from stdin, `/dev/stdin`, `/dev/fd/N` or `<(…)`
  (`-f F`, `-fF`, `-sf F` clusters, `--file|--makefile|--justfile[=]F`). Text that expands an
  alias variable (`echo "$p" | sh`, `sh <<< "$p"`) asks too.
- **Data:** the word as an argument of a data consumer or a known launcher's
  real verb, a `for`/`select` word list, `tmux send-keys`, a quoted heredoc
  body not fed to a shell, a comment, bodies piped to an *interpreter*
  (`python3`, `node`, …), arguments of a script run by a shell (`bash script.sh
  -c x`), and lookups. A data mention never shields a real call elsewhere, and
  a producer only feeds a shell it is PIPED to (`echo eval x; echo hi | sh`
  allows).

Residuals, stated plainly. Wrongful asks we accept as the price of the
direction: an unknown launcher followed by the bare word (`chroot / grep eval f`),
a quoted `'>f'` used as a command name, and a shell invoked with a payload
that merely mentions the variable (`p=eval; sh -c 'echo $p'`). **Not detected:**
a data consumer that can itself run a command line, except `sed` and `git` above
(also: `read c < <(echo …)` — `read c <<< word` and `read c <<'EOF'; eval "$c"` ask via `ambiguous:eval`;
`git config <exec-key> <value>` set-form asks since #1133 and its value is rescanned (#1136); `git rebase -x` and
`GIT_SEQUENCE_EDITOR` ask via the git.rebase rule); any tool not in the
tables asks; Makefile/justfile recipe text from a plain file is undecidable);
a name assigned in another command (a bare `$X` that this command does not
define is an environment name and passes — see *Indirect verbs* below),
`parallel '{eval} {}'`, and a call
inside `$(…)`, which the substitution path judges. Command-position use is
*always* unverifiable; a literal operand is not re-scanned as a command. With
the kill switch off (`enabled: false`) nothing here applies — the switch
allows everything by design.
`tests/unit/test_damage_control_bypass.py` is the differential: generated
spelling × position × wrapper × consumer cases, one generated test per table
(driven by independent copies of the table specs, so a deleted row or element goes red),
committed corpora of executing spellings (never `allow`, also unattended and
under size padding) and of data mentions (must `allow`).

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

## Attended-block friction, round 3 (#940)

Corpus: 9 days of audit logs, 255 attended blocks (222 distinct commands; rows repeat). Reviewer / fix / probe sessions netted out by name: 83 distinct commands from real work sessions. Replaying every logged block through the bundled rules: 29 already allow on `main` (rounds 1-2), this round flips 5 more, and **no previously allowed command regressed and none became an ask**. Netted still-blocking: 61 on `main`, 58 after.

- **The "no rule_id" bucket (127 of 255) was six checks, not one.** `protectedControlPlane`, `protectedCommand` and the three path ladders (`zeroAccessPath`, `readOnlyPath`, `noDeletePath`) built their block result with a `pattern` but no `id`. Every Bash block now carries one (`core.protected-control-plane`, `core.protected-command`, `core.zero-access-path`, `core.read-only-path`, `core.no-delete-path`), so `agentwire safety report` ranks them like any rule. Blocks are not grantable, so the ids change no permission.
- **Zero-access on stored text: tried, dropped.** Hiding a quoted `cat` / `tee` heredoc body from the zero-access check (a `.gitignore` listing the env file, a script containing `event.key`) was built twice and review kept finding a way to execute the stored text (process substitution, a later `sh`/`make`/`rake`, a startup file as destination). The sound version needs an inert-extension allow-list for the destination and an inert-command allow-list for everything after the body; replayed on the corpus it flipped 0 of the logged commands, so it was removed rather than shipped. The mention still blocks; the escape hatch covers a genuine case. A file written in one command and executed by a LATER one is outside the hook's view regardless.
- **`rm` allowances gave up on any group.** `(npm run start >/dev/null 2>&1 &); … rm shot.tmp.mjs` left the whole compound unmodelled, so the scratch / untracked allowances never ran. A parenthesised span is now treated as a no-op only when it opens at COMMAND POSITION (start of text, after a newline, `;`, `&`, `|`, or a spaced `!`) and holds no `cd` / assignment / `export` / `rm` / `find` / `git` / interpreter word. Case-arm patterns (any command containing `case`), extglobs (`!(x)`, `@(a|b)`), array assignments, empty `()` and a paren after a word are not groups and stay as before. A group that could leak directory or variable state stays unmodelled. Known limit, same as the flat form: a deletion verb hidden inside a real group is not separately modelled.

Remaining blocks, by design or out of reach of a text hook:

| Shape | Why it stays |
|---|---|
| Real reads of the env file, `~/.pypirc`, `.vercel/project.json` | the rule working |
| `rm -rf <untracked dir>` (`sites/test-client`), `git rm` | untracked bytes cannot come back; `git rm` is permissioned by owner decision |
| `rm` behind `cd $VAR` / `cd -` / `cd x; …` | the directory the `rm` runs in is not statically known |
| `rm <glob>` | the expansion is not known |
| Prose naming a protected path inside `agentwire send` / `msg send` bodies | control-plane batch, not this round |

Candidates left unbuilt (policy, not mechanism): `rm -rf` of a git-ignored directory with a well-known build-output name (`dist`, `node_modules`, `.next`, `__pycache__`), `rm <file>` directly under `/tmp`, and a final-component glob strictly inside scratch.

**Does an attended hard block need a per-project escape short of the kill switch? Recommendation: not yet.** Every friction class in this corpus was a precision bug (a rule reading text as an operation), fixed by precision, and the kill switch plus the escape hatch already cover a genuine one-off. An escape is a loosening of a control-plane knob that would live in the protected `damagecontrol.yml` (human-only), so each entry is a standing hole that an unattended run inherits. If one is ever built: scope it to `(rule id, path glob, project dir)`, never to a bare rule, refuse the kernel ids (`core.protected-*`, `core.zero-access-path`), and have `doctor` list every active entry. Revisit only if precision fixes stop shrinking the netted count.

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

# Recurring blocked `rm -rf dist|node_modules|…` + the delete-recursive policy to paste (#1152; read-only)
agentwire safety suggest-delete-recursive --days 14 [--min 2] [--json]
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

## Deployment: merged is not deployed (#936 + #916)

`agentwire.safety_ext.install` (→ the package's `init`) is the one function that puts damage control on
a machine. The sections below are the lessons that outlived the pre-package `heal` and its file copies; where
they describe copying files, read "the retired `heal`" (`agentwire safety migrate` is what classifies and removes
what it left). It used to fail in **opposite directions** for the
two halves it managed:

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

**2. Ordering — retired (#1209 M4).** The five generated hook scripts and their
`AGENTWIRE_HOOK_STAMP` no longer exist: the hook is the installed package, so there is
no installed copy that can be older or newer than the source. The failure this guarded
(a stale checkout reinstalling an older hook) is now closed by provenance alone, which also
covers the interpreter the registration records.

**3. Three-way sync — not install-missing-only.** Rules and tooldefs are
host-editable, so a blanket overwrite is off the table. The missing leg of a
three-way merge is the common ancestor, and `agentwire/safety/rule_baselines.json`
ships it: the sha256 of **every version of each file that has ever shipped**,
generated from git history by `scripts/gen_rule_baselines.py`. Since #1209 M6 it is read by exactly one
command, `agentwire safety migrate`, which deletes pristine copies and keeps (and names) edited ones.

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
`gh pr create` permitted unattended in ANY repo. The package's `init --refresh` prints the ruleset diff
(loosening marked `!!`); narrow it with `unattended_allow` in
`~/.agentwire/damagecontrol.yml` (path-scoped entries supported, #914).

### Rule ids are frozen (#1209 M1)

Every rule pattern, MCP policy and write-tier tooldef command carries an explicit
`id:`; none is derived from the `reason:` text any more (a derived id made a typo
fix a silent rename). Policy:

- **Ids are frozen.** Editing a `reason:`, `pattern:` or description never changes an id.
- **Removing or renaming an id is a changelog entry**, not a refactor: it breaks
  `disabled_rules`, `unattended_allow` and audit queries that name it. Record it in the
  release notes, and update `corpus/golden/rule_ids.json` in the same PR.
- **No alias shim.** The old id simply stops resolving; `doctor` already reports a grant
  naming no rule.
- **Adding a rule** means writing its `id:` (`<file>.<slug>`, or `tooldef.<slug>` for
  tooldefs) and adding it to `rule_ids.json`.

Enforced by `tests/unit/test_rule_ids_pinned.py`: `safety.lint.unpinned_rules` rejects a
rule with no `id:`, the sorted id set must equal `corpus/golden/rule_ids.json`, ids must be
unique, and every `DEFAULT_UNATTENDED_ALLOW` id must resolve to a loaded rule.

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

## Closed, fail-closed, user-owned (#1065)

The single honest statement of what the text hook decides. A PreToolUse hook reads
the command text before it runs, so anything whose path or verb exists only at run
time, or whose content is in a file the text merely names, is not decidable by
tightening rules (the lesson of #913/#915/#917/#921: it moves the bypass to the
next spelling). The claim is **not** "zero residuals": it is *every statically
decidable shape closed, the rest fails closed or is yours to enforce at the OS*.

| Residual | State | What decides it |
|---|---|---|
| Literal / quoted / escaped / `$HOME` / `~` spellings of a protected path; `ln` / `link` onto one | **Closed** (block) | path ladders, masked subcommands |
| Existing symlink / hardlink / directory alias of a protected file (pre-existing); a relative word after `cd <dir> &&` is new | **Closed** (block) | realpath + inode match at hook time (`_realpath_variants`, `_cwd_realpath_variants`) |
| Brace expansion past the 256-variant cap (pre-existing) | **Fail-closed** (block; ask `core.control-plane-target` when only that check sees it, e.g. a protected path before the braces) | `brace_variants` returns None; path patterns treat it as a hit |
| Inline interpreter writing to a computed path while naming the control plane | **Fail-closed** (ask, unverifiable) | `_opaque_inline_write` |
| Unresolvable `cd` then a bare protected name; `patch` / `git apply` in a protected dir | **Fail-closed** (ask) | `unverifiable_target_verdict` |
| Scan too large / too slow; hook outliving its timeout | **Fail-closed** (ask / block) | size gate, SIGALRM deadline, watchdog |
| SQL fed from a file (`psql -f`, `mysql < f`) | **User-owned** | read-only database role for agent sessions |
| `make` / `just` / package-script recipe text | **User-owned** | sandbox profile / container / separate uid |
| Check-then-use race (TOCTOU) | **User-owned** | `chflags uchg` (macOS) / `chattr +i` (Linux) on the control files |
| Run-time-assembled paths or verbs beyond the fail-closed shapes (decoded payloads, `chr()` chains, `$(…)` output, earlier-turn aliases) | **User-owned** | the same immutable flags, or a sandbox profile denying writes to them |

`agentwire doctor` prints the user-owned rows under "Residuals you own"
(informational, never scored; the list is `agentwire/safety/residuals.py`).
agentwire does not set these controls: the immutable flag stops agentwire's own
edits to the file too (`chflags nouchg` to change it), which is a decision for
the person who owns the machine.

## Moved: extending

The seam (Profile, `Guard.decide`, adapter) and the runtime package moved to [`packages/damage-control-dev/docs/extending.md`](../../../packages/damage-control-dev/docs/extending.md). AgentWire's side of it is `agentwire/safety_ext.py`.

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

**Adjust the pattern** — copy the relevant rules file from `packages/damage-control-dev/src/damage_control/rules/` into `~/.agentwire/damage-control/` and edit there (it then replaces the bundled file of that name):
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

### Corpus and golden snapshot (#1209 M0)

- **Corpus as data** — `corpus/cases/*.yaml`, one case per `(tool, input, context) -> expect`, validated by `corpus/schema.json`; every case carries a `class` and a `source` (lint rejects a case without them). `tests/corpus_runner.py` runs it; `tests/unit/test_corpus.py` has **one test per class**, plus must-fail controls (a contradicted expectation must fail, the lint must reject a bad case).
- **Golden snapshot** — `corpus/golden/snapshot.json` is `(input, context) -> decision, rule_id` for every corpus case plus every engine input harvested from the safety tests (`harvested.json`), through the in-process engine, the five real hook entrypoints (subprocess; interactive/bypass/unattended postures) and representative audit rows. A later change must reproduce it byte-identically; a deliberate behaviour change lands as an explicit snapshot diff in its own PR.

```bash
scripts/golden_snapshot.py check     # re-record and diff against the committed snapshot
scripts/golden_snapshot.py record    # re-record (only for a deliberate behaviour change)
scripts/golden_snapshot.py harvest   # refresh harvested.json from the safety tests
```

The recorder pins `HOME`/`PWD`/`AGENTWIRE_DIR` and the session identity. The fake `HOME` is `/golden/home`: not under `/tmp` (core.yaml allowlists `/tmp`, which outranks `zeroAccessPaths`) and not `/home/...` (macOS autofs makes every stat of a nonexistent `/home/x` ~60 ms, a 25x slowdown).

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

## Moved: threat model

The security model (what it protects against, what it does not, the residuals) moved to [`packages/damage-control-dev/docs/threat-model.md`](../../../packages/damage-control-dev/docs/threat-model.md).

## FAQ

### Q: Does this slow down AgentWire?

**A**: Minimally. Hooks add ~70-100ms per command, which is negligible compared to actual command execution time.

### Q: Can I customize patterns per session?

**A**: Not yet. Patterns are global — bundled `packages/damage-control-dev/src/damage_control/rules/` plus an optional overlay at `~/.agentwire/damage-control/`. Per-session overrides are a future enhancement.

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
- `packages/damage-control-dev/src/damage_control/rules/` and `agentwire/safety/rules/` — bundled pattern source-of-truth.


## AgentWire's extension surface (#1209 M6)

AgentWire consumes the `damage_control` package through extension points only. `agentwire/safety_ext.py` is the whole
surface: `build_profile()` (protected paths and commands, default grant ids, the notifier argv, MCP command templates,
the `audit` field, the rules layer, default packs), `install()` (provenance for BOTH packages, write the manifest, then
the package's `init`), `status()` (the package's `status --json` / `doctor --json` under AgentWire's profile), and
`activate()`. Everything else is a call through the package: `safety_cli` / `safety_packs_cli` are thin over
`damage_control.packs`, `doctor` renders the package's findings (version and profile included) and adds only
AgentWire's own checks, and the break-glass script is the package's.

- **Two-package provenance.** `rebuild` and `install` refuse a worktree or foreign source for EITHER package
  (`agentwire/safety/provenance.py`: `PACKAGES`, `all_provenance`, `rebuild_package_refusal_lines`). The engine is the
  package that runs on every tool call; guarding only `agentwire` left it open.
- **`agentwire safety migrate`** (human-only, in `PROTECTED_COMMAND_PATTERNS`, `--dry-run` reads only): classifies an old
  install with `rule_baselines.json` (pristine copies deleted, edited ones kept and named), records the pack decision the
  old files implied, installs the package hooks, and only then removes the pre-package script registrations and files.
- **Notifier protocol.** The block notifier receives the event as JSON on stdin; the profile supplies only the argv.
- **Tests.** One integration test per extension point (`tests/unit/test_extension_points.py`) drives the real hook under
  AgentWire's manifest and the generic one; `tests/unit/test_engine_standalone.py` is the AST proof the package imports
  nothing from `agentwire`, with no exemptions.
- **Differential.** The in-tree engine no longer exists (it moved in M4), so the old-versus-new differential is the
  golden snapshot: `uv run python scripts/golden_snapshot.py check` replays every recorded payload through the real
  `python -I -m damage_control.hook` entry and must reproduce the M0 recording, recorded from the old in-tree hooks,
  byte for byte.
