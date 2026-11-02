# Threat model

What damage control protects against, what it does not, and the residuals. Moved verbatim from AgentWire's `docs/wiki/internals/damage-control.md`.

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
strings linear (30 k chars: 0.2 s). The glob-word scan (`_glob_tokens`, #1132)
is one pass over the word runs: the single regex it replaced retried from every
offset of a long run holding no glob mark, which was the whole cost of a 7 000-
fold `../` run (4.9 s), a 20 000-fold `\` run (2.1 s) and a 7 000-fold `{a,` run
(1.8 s) — now 0.6 / 0.3 / 0.5 s, and doubling the input doubles the time for
quoted and unquoted `../`, `a/b/`, `\` and `{a,` runs alike. The clock budget
stays the backstop for a shape not yet found, not the mechanism for these.
A writer verb next to a protected path stays blocked with a long quoted string
before it, after it, or holding the path, balanced or not (pinned; a differential
against main over that slice found no decision differences). Known allow, same
as main: a writer AND the path both inside ONE quoted string (`ln`, `curl -o`)
is data in a quote, not a command.

The read hook is linear in the path length (a 20 MB path takes ~4 s); edit/write
scan only the path. All five hooks are bounded anyway (#1065): the edit/write/read/mcp
entrypoints run `main` under the same 10 s `SIGALRM` scan deadline
(`run_hook_guarded(main, deadline_seconds=…)`; over it = block exit 2), and EVERY
hook starts a 20 s watchdog thread that exits the process with a block, so a
hook can never outlive the 30 s harness timeout into "no decision".

Why two: the signal is the only thing that interrupts a pattern stuck inside the
regex engine (`re` checks for signals; a thread waiting on a timer needs the GIL,
which a long C regex never yields) — but it needs POSIX and the main thread.
The watchdog needs neither, so it covers Windows and any wait that releases the
GIL (a hung `stat`/`realpath` on a network mount). **Declared residual:** a
runaway regex on a platform without `SIGALRM` (Windows) outlives the watchdog
too; the size gate is the only bound there. `check_command` called in-process
from a portal worker thread keeps the old shape (size gate only; the watchdog
exits the process and so is hook-only).

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
spaced. A `case` header is dropped through the first `)` after `in`, and the
stripper keeps stripping while the remainder still starts with a `)`-bearing word,
so a pattern holding its own parens (`"a)b"`, `[)]`, `a\\)`, `@(a|b)`, `$y`, `${y:-a)b}`)
never leaves the body hidden. This is deliberately CONSERVATIVE: tokens arrive
with quotes already resolved, so the first `)` is taken as the terminator and a
harmless body may be over-read, never a destructive one under-read. A segment that
opens with a command substitution (`$(date)`, a backtick) only counts a `)` that
takes the paren depth negative. Function headers in every spacing (`f()`, `f ()`,
`f ( )`, `f(  )`, `function f`, `function f ()`, newline before `{`) and `time -p`
options are dropped too. Pattern words are never match starts, so a verb in an
arm pattern is data: `case x in 'rm -rf') echo hi;; esac` is ALLOWED.

Wired into: the masked-entry builder (anchored rules, ssh and git-config payloads,
global-option stripping), `ambiguity_conceals_verb`, and `_feed_words` (producer
and consumer of piped `echo`, heredoc and here-string feeds to ssh/at/shell).
`_CD_RE` (directory tracking for control-plane writers) is a regex over raw text,
so it carries the same opener list plus `time`/`nohup`/`env`/`sudo`/`VAR=1`
prefixes. `command_scope_dirs` (the unattended grant evaluator) and
`_parse_rm_command` REFUSE any segment that starts with compound syntax, because
a `cd` behind `then`/`do`/`!` cannot be modelled. Not wired because nothing can
observe it: `_stage_reads_program` and the prose-blanking runner check (the
earlier wiring there was deleted; removing it changed no decision).

Pinned by a differential (command x 70+ wrappers incl. the full pattern-form
sweep, decision equal to top level, in all four kill-switch x unattended states)
in `tests/unit/test_damage_control_bypass.py`, plus must-allow controls and
exact-shape pins for the stripper.

**Declared residuals:** a substitution in a
case pattern is ask-tier, never allowed.

### Indirect verbs and unreadable producers (batch 4)

A verb the shell computes cannot be read, so it is **resolved** from what the
same command defines and the result is judged by the same engine
(`_indirect_verbs` feeds `_hidden_commands`; `_decide_command` recurses, so a
resolved verb is decided exactly as typed — also inside `sh -c`/`ssh` payloads,
which are walked too, with the definitions they mention, as an exported
variable reaches them). Resolved: literal scalars and `export`s (`=` and `+=`,
multi-valued if assigned twice), literal and associative arrays (`+=`, `[@]:n`
slices), `for`/`select` lists (brace-expanded), `set --`, `alias n=v` (chains
too), `${x:=d}` / `${x^^}` / `${x@Q}` forms, and functions (`{ }` and `( )`
bodies, nested calls) — a call substitutes its arguments for `$1…`, `"$@"`,
`$#` in the body and rescans it. Assignments are found after `then`/`do`/`else`/
`{`/`(`/`)`/`!`, not only after `;`. `$x"al"` is read as `${x}"al"`, so
`x=ev; $x"al" '…'` is the builtin. A defined variable is also substituted into
text handed to a shell (`echo "$x -9 1" | sh`, `sh <<< "$x …"`, `at now <<< …`).
Only the definitions a resolved command mentions travel with it, and a
function whose verbs are all literal is not re-scanned per call (its body is
scanned where it sits).

What cannot be resolved asks (`core.indirect-verb`) in these cases: a variable
filled by `read` / `mapfile` / a glob loop list, a head that is a literal
PREFIX OF A BUILTIN glued to an expansion (`e${x}val`), or any unresolved
expansion in a command that names a control-plane path — all `unverifiable`
(never granted, never demoted by bypass, a block unattended); and a variable
filled by an ordinary `$(…)` (`p=$(pick); $p x`), which keeps the plain,
bypass-demotable ask such commands always had. A bare environment name
(`$CC -o x`, `$EDITOR f`, `$VENV/bin/python`, `${HOME}/bin/t`, `"$@"` in a
function that is only defined) passes, and so does any head with a literal `/`
before the expansion (`./$script`, `bin/$tool`, `./scripts/$1.sh`) or a plain
literal prefix that is not the start of a builtin (`python$V`, `pip$V`,
`$CC$FLAGS`): those cannot spell `eval`/`exec`/`source`.

**Measurement** (replay of `~/.agentwire/logs/damage-control`, 6,563 distinct
Bash commands over 9 days, bundled rules, fake HOME — one fleet's own
damage-control work dominates it, so read it as a floor on benign shapes, not a
census): a `$VAR` in command position occurs in ~230 commands, 222 of them a
same-command literal (`P=<path>/python; $P …`, `M=./mutate.sh; $M`), which
resolution now judges instead of passing unread; 8 are truly unresolved
(loop/substitution-filled). `$CC` / `$EDITOR` / `"$@"` as the verb: **0**, so
the "bare environment name passes" carve-out costs nothing measurable and an
ask there would be a silent loop for an unattended run. `alias` used as a verb:
1; a function called in-command: ~490 rows, one repeated mutation-test script.
`curl|wget … | sh`: 0 executions (3 rows are prose inside an issue/prompt body);
`cat … | sh`: 0 executions. **Replay before/after: 0 diffs in 6,563
commands, at the same wall time** (an earlier draft cost 3x on function-heavy
scripts and timed out 14 of them into scan-budget asks — the replay is what
found it; definitions now travel only where mentioned).

**Unreadable producers.** `echo`/`printf`/heredoc/here-string feeds are read and
judged (above). Any other producer piped into a program-reading shell or
interpreter (`bash`, `sh`, `python3 -`, `lua`, `sudo -u root bash -s`,
`busybox sh`; not `-c`, `-n`, or a script operand — a redirect such as `2>&1`
is not an operand) or fed through `sh <(…)` / `. <(…)` / `source <(…)`, in the
command, in any `-c`/ssh payload of it, or in a `( … )` group: a **download** (`curl|wget|fetch|aria2c|http|xh` anywhere in the
pipeline) is a **block** (`core.fetched-program-pipe` — the classic remote-code
run; the fix is to download, read, run the file, which no rule touches, and
the measured use is zero); a file or another command's output is an
`unverifiable` ask (`cat setup.sh | bash` → `bash setup.sh`). `curl … | jq`,
`| python3 -c '…'`, `| sh -c 'wc'` and `curl -o f u && sh f` are unaffected.

Declared residuals: a **computed consumer after a fetch pipe** (`c=sh; curl u | $c`,
`| /bin/$c`, `| command $c`, `| $SHELL`, `| ${SH:-sh}`) is not recognised as a
shell, so it allows; and a fetch pipe inside a substitution (`$(curl u | sh)`,
backticks, `echo $(curl u | sh)`) is not blocked — it asks through the
ordinary substitution rule (still gated, just not hard-blocked). Subscript
assignments (`m[k]=v`, `a[0]=v`) resolve; a subscript computed from an
expansion does not. Also: a function or alias defined in a *previous* command, a
variable set in another command, `declare -n` namerefs, `${!x}` indirection and
arithmetic heads (treated as environment names, so they pass unless the
command names a control-plane path). A resolved verb is decided *as typed*, so
two pre-existing gaps in the typed form remain visible here: a redirect before
the verb (`>/dev/null kill -9 1`) and `env -S "kill -9 1"` are not read as the
verb by the kill rule even without any indirection. An alias is not carried
into `bash -c '…'` — the real shell does not either. Test:
`tests/unit/test_damage_control_bypass.py`.

### Hidden commands are judged as commands (#1136)

Text another program RUNS is a command in its own right, whatever carries it:
`<(…)` / `>(…)` bodies (the shell runs them whatever consumes the pipe), a
heredoc / here-string / `echo`-`printf` pipe into a shell (or into `xargs -I{}
sh -c '{}'`), and the value of an option or the program of an embedded language:

| carrier | hidden command |
|---|---|
| `sed` | the `e command` command (any address form), the replacement of `s/x/<cmd>/e` |
| `awk`/`gawk`/`mawk` | every string literal (and their concatenation) of a program that uses `system(`, `getline`, or a `|` pipe |
| `tar` | `--to-command`, `--checkpoint-action=exec=`, `-I`/`--use-compress-program`, `-F`/`--info-script`/`--new-volume-script`, `--rsh-command`, `--rmt-command` |
| `git` | `difftool -x/--extcmd`, `rebase -x/--exec`, `--upload-pack`/`--receive-pack`/`--exec` (fetch/pull/push/clone/ls-remote/archive), `clone -u`/`-c <exec-key>=`, `grep -O`, `filter-branch --*-filter`, `submodule foreach`, `bisect run`, set-form `config <exec-key> <value>` |
| `npm exec` / `npx` | `-c`/`--call` |
| `ssh`/`scp`/`sftp` | `-o ProxyCommand=`/`LocalCommand=`; scp/sftp `-S program` |
| `sh -c '…'`, `ssh host '…'` | a hidden command *inside* the payload |

`_hidden_commands` collects them; `_decide_command` judges each with the same
engine that judges it top-level and keeps the strictest verdict, and
`_derived_commands` feeds the same list to the protected-path scan. So a
payload's decision inside a carrier equals its decision alone — by
construction, with no per-rule twin. The outer escape hatch / kill switch ends
the walk (the hidden command is part of what the user waved through); an inner
one can never lower the outer verdict. For the general rules an interpreter's
stdin is NOT shell text (`python3 - <<< 'eval'` runs no eval); only the
protected-path scan keeps reading it as shell, as before.

`parallel … :::`, `flock LOCK cmd` / `flock LOCK -c 'cmd'` and `docker|podman|nerdctl
run|exec IMAGE cmd` (also `docker container exec`, `docker compose|docker-compose exec|run`, `kubectl exec POD -- cmd`; `--entrypoint` joins the command; a quoted `parallel 'cmd {}' :::` line is scanned whole) are hidden-command
carriers for the verb rules too: the launched command is judged as if typed
(`flock /tmp/l rm -rf /x` blocks like `rm -rf /x`). The eval check models the same
grammars, so only the eval word's wrongful ask is gone — `docker run alpine ls`
still asks, by the docker write-tier rule.
`tests/unit/test_damage_control_bypass.py` is the differential.

**Declared residuals (undecidable statically, or text a program produces):**
`sed e` with no operand and `awk system($0)` run input data; `awk -f prog` /
`sed -f script` are files; an
interpreter's stdin on a LOCAL run (`echo 'os.execute(…)' | lua`, `lua <<< …`,
`… | python3`, `python3 - <<EOF`) is judged by the protected-path net only, not
the general verb rules (a prose literal in a heredoc script would otherwise
block, as `python3 -c` does); a remote interpreter's stdin IS judged.
A here-string behind a same-line compound keyword (`if true; then sh <<< X; fi`,
`for …; do bash <<< X; done`) and behind wrapper + `-c` (`sudo|env|nohup|timeout N
bash -c 'sh <<< X'`, also heredoc) are judged like the bare form (#1144): the feed line
is cut at its last segment break before its opener words are stripped, and the
`-c` payload scan steps past prefix wrappers. Pinned by
`test_damage_control_bypass.py` (every hidden-input form x every
opener/wrapper == the top-level decision).

### A `)` / `;` inside a word's data does not hide the verb (#1119)

`A=${x:-)} rm -rf /tmp/x` was ALLOWED while `rm -rf /tmp/x` was blocked. Two
readers of the same text disagreed with bash about where a word ends:

- `_scan_masked_tokens` cut at `;` `&` `|` newline inside `${...}`, so the
  second half started `}` and became the "command" with `rm` as its argument.
- `strip_command_openers` took the first `)` of a head token as a case-arm
  terminator. Tokens arrive quote-resolved, so `A='a)b' rm`, `A="a)b" rm`,
  `A=a\)b rm`, `A=$'a)b' rm` and `A=${x:-)} rm` all looked like an arm ending
  mid-word; the leftover was the head and the real verb an operand.

Fix, in one place per reader. The scanner reads `${...}` (brace-depth, quote,
escape and `$(`/backtick aware; only `${` nests, a bare `{` is data, as in
bash), `$'...'` and an assignment subscript `name[...]=`/`+=` as ONE word
(`_word_span`, helpers `_param_expansion_end`/`_ansi_c_end`/`_subscript_end`).
A subscript only counts when `=` follows the `]` — `a[x;rm -rf /x]` is two
commands in bash and stays two here. Every `)` the scanner knows is data
(quoted, escaped, inside a span) is written as `_DATA_PAREN` for the opener
stripper and restored right after, so no rule sees it. Walkers without quote
info (`strip_command_opener_words`) get the span-aware `_first_paren_outside_spans`.
`_ASSIGN_TOKEN_RE` now accepts a subscripted name, so `a[)]=1 rm` is a prefix
assignment like `A=1 rm`.

An unclosed `${` / `$'` / `[` is NOT a span: the scanner falls back to plain
character scanning, which sees every verb in the remainder (errs closed for the
operation). A `$'a\'b'` that shlex cannot parse is ask-tier via the existing
"unbalanced quotes" ambiguity, as is any substitution inside an expansion.

Pinned by a differential in `tests/unit/test_damage_control_bypass.py`
(command x 20 constructs x 6 positions x 8 wrappers, equal to the top-level
decision, plus the kill-switch-off / unattended states), with must-allow
controls and scanner-shape pins.

**Declared residuals:** `normalize_subcommands` / `_scope_segments` still split
with shlex punctuation (their consumers are the raw/unanchored haystacks, which
see the verb regardless).

### Defense in Depth

Damage Control is ONE layer:
- **System permissions**: Run AgentWire as non-root
- **Backups**: Regular backups of critical data
- **Version control**: Git commits for code changes
- **Audit logs**: Track all operations
- **Damage Control**: Block catastrophic commands

---
