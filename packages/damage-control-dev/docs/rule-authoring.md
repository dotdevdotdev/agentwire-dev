# Rule authoring

How rules, tooldefs, groups and packs are written and loaded. Moved verbatim from AgentWire's `docs/wiki/internals/damage-control.md`; the examples use AgentWire paths where they were written against it, and the config home is the profile's (`~/.damage-control` by default).

## Security Patterns

Patterns live in **categorized YAML files** (committed beside their JSON, which the hook reads — `packages/damage-control-dev/tools/build_rules_json.py`) under `packages/damage-control-dev/src/damage_control/rules/` (generic, one per topic) and `agentwire/safety/rules/` (the AgentWire/owner layer, #1209 M2: `~/.agentwire` paths, `mcp__agentwire__*` policies, `agentwire email|quo`, the owner `.env` allow). Both are read from the package and merge in file-name order, so `agentwire-mcp-tools.yaml` sorts ahead of the generic `mcp-tools.yaml` write-shaped fallback; file names must be unique across the two dirs. `tmux.yaml` is the opt-in generic `tmux` pack (AgentWire enables it by default; heal carries it forward on a machine whose overlay `agentwire.yaml` still held the tmux rules). `scripts/effective_rules.py --check` diffs the merged rule set against `corpus/golden/effective_rules.json`. To override or extend, drop YAML files into `~/.agentwire/damage-control/`: a file there REPLACES the bundled file of the same name and adds to the rest (the bundle is never dropped). Needs PyYAML; without it the file is skipped with a loud load failure and the bundled rules still enforce.

### Flag position: `{ARGS}` (#1125)

GNU tools and git accept options after operands, so `rm foo -rf x` is the same operation as `rm -rf foo x`. A rule spelled `verb\s+(-[^\s]*)*-f` only sees flags before the first operand. Such rules are authored with `pattern_src:` (containing `{ARGS}`, "any leading arguments of this command segment") on the line after `pattern:`; `scripts/gen_rule_patterns.py` expands it into the plain `pattern:` the hook reads (`--check` fails when stale, pinned by `test_damage_control_bypass.py`). **The hook never expands anything**: an old hook reading new rules sees an ordinary valid regex. The first cut expanded at match time and an old hook + new rules ALLOWED every rm/push/reset/clean/chown (a literal `{ARGS}` matches nothing). A token is an argument unless it is a separator (`; & |`) or the bare `--` (everything after is an operand, so `rm -- -rf` stays allowed). Short flags match as cluster members (`-vrf`, `-hR`) plus the long option; `git branch` uses a case-sensitive `D` or delete+force (`-d -f`, `-df`, `--delete --force`); `git clean` is allowed when a dry run (`-n`, `--dry-run`) appears anywhere in the segment. Rules: rm, chown -R root, chmod 777 (non-recursive: mode must be the first non-flag operand, `chmod u+x 777` allows, #1133), git reset --hard, clean, push -f/--delete, branch -D, killall/pkill -9, kill -9 (ask) / kill with the all-processes target `-1` (block), rsync --delete* (ask), mail -s, kubectl delete all --all. After editing a `pattern_src`, run the generator.

### Batch-1 residual closures (#1133)

Rule-YAML only; each ships independent of the hook scripts (plain regexes, no `{ARGS}` at match time).

| operation | tier | rule id | notes |
|---|---|---|---|
| `kill` with `-1` as the target: `-1` anywhere but the first operand (`kill -9 -1`, `--signal TERM -1`, `-s KILL 1 -1`, `kill 123 -1`), or `-1` first followed by another option (`-1 -9`) | block | (unnamed) | bash parses options only up front, so a later `-1` is pid -1; `kill -1 123` (SIGHUP one pid) allows |
| `kill -9` / `-KILL` / `-SIGKILL` / `-s KILL` / `-n 9` / `--signal KILL`, flag in any position | ask | `core.kill-sigkill` | unattended: needs `unattended_allow`; rephrase to plain `kill` |
| `rsync` every `--delete*` long option (`--delete-missing-args`, `--delete-delay`, …), `--del`, `--remove-source-files` | ask | `core.rsync-delete` | `--no-delete`, `--max-delete` pass; a no-delete *destination* is the no-delete ladder's job |
| `crontab` other than listing (`-l`, `-lu me`, `-ul me`, `--list`) or `-V` (file, `-`, `-e`, `-r`, bare) | ask | `core.crontab-install` | body is deferred execution and unreadable here; matched on the command word, so `crontab.sh` / `vim ./crontab` do not ask |
| `git config <key> <value>` for redirect keys (`core.worktree`, `core.hooksPath`, `include.path`, `includeIf.*.path`) | ask | `git.config-set-redirect-key` | always |
| `git config <exec-key> <value>` (the `git -c` key list) | ask unless the value is ONE known editor/pager word (`vim nvim vi view nano pico micro emacs ed ex less more cat bat most true false code subl mate zed`), bare or quoted, with optional plain flags (`'vim -f'`, `'code --wait'`) and scope flags | `git.config-set-exec-key` | unanchored (raw view: quoting/escaping decide safety); git runs the value through a shell, so `'less;sh'`, `"less\|sh"`, `less\;sh`, `vim\`id\``, `$'vim'`, `sh`, `python3` all ask |
| `find … -exec` (or `-execdir`, `-ok`, `-okdir`) `chmod 777` | block | (unnamed) | the chmod sits in find's arguments, outside command position |
| `git config alias.x '!cmd'` | ask | `git.config-set-alias-bang` | unanchored: the `!` is inside a masked quote |

`git config core.hooksPath .githooks` asks (policy: it names a directory of programs git will run). Known price: `git config core.pager 'less -FRX'` asks once. The value is **not** re-scanned as a command in the set-form (that is `engine.py` work; the `-c` form is, via `_git_exec_config_payloads`), so the rule is a coarse bare-word test rather than a payload judgment. The `chmod` fix narrows only the non-recursive rule; `chmod -R` keeps 777-anywhere on purpose (an unreadable mode token such as `${m:-x}` must not hide a world-writable tree), so `chmod -R u+x 777` (a file named 777) still blocks.

**Wrapper x git global options (#1133).** `timeout 5 git -C d push --force` used to allow, and so did `caffeinate git push -f` (no global option at all): the anchored-rule position walk knew only a narrow wrapper list, and the global-option stripper gave up at any token that was not a bare `sudo`/`env`/`nice`-class wrapper. `_POSITION_WRAPPERS` now includes every `_PREFIX_WRAPPERS` entry, wrappers that take a bare operand before the command (`flock FILE`, `lockf`, `chroot`, `sg GROUP`) are modelled in `_WRAPPER_OPERANDS`, and `_strip_global_options` skips a wrapper's own options/operands before the tool. Pinned by a 34-wrapper x 7-global-option x 11-destructive-op differential (decision wrapped >= decision top level). Still open (declared): wrappers that carry the command in a quoted string or have no stable operand grammar (`watch`, `script -qc`, `sg grp -c 'git …'` quoted) are judged by the shell-payload rescan, not this path. `./crontab` as a command no longer asks.

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
`tests/unit/test_damage_control_bypass.py` so a green suite cannot
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
`tests/unit/test_damage_control_bypass.py`, which also asserts a
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
table (`_GLOBAL_OPTION_TABLE` in `packages/damage-control-dev/src/damage_control/engine.py`). Rules do not need
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

Blocks: `rm`, `unlink`, `rmdir`, `shred`, plus the three ways to the same end that the verb list used to miss (#1127, #1128; see [What the no-delete ladder judges](#what-the-no-delete-ladder-judges-1127-1128)).

##### What the no-delete ladder judges (#1127, #1128)

`noDeletePaths` (and the delete half of `readOnlyPaths`) used to pair a delete verb with a path inside ONE simple command. Four shapes got around it; each is now judged like the direct operand (`rm <path>`) by `producer_scan` / `producer_fed_hit` / `move_overwrite_hit` in `engine.py`. **Entry classes matter:** a *file* entry (`CLAUDE.md`, `README.md`, `~/.agentwire/settings.json`, `LICENSE`, …) is genuinely guarded; a *directory* entry (`~/.claude/`, `~/.agentwire/`, `~/.agentwire/sessions/`, `.git/`) also covers runtime state agents write and move routinely (cohorts, inbox, sessions). The mv/overwrite/emptying checks therefore apply to file entries, and to a directory entry only as the directory itself or an ancestor of it.

**Producer-fed deletes (#1128).** A delete consumer whose operands come from elsewhere: `xargs [opts] rm`, `parallel rm` / `parallel 'rm {}' ::: x`, `rm $(…)` / `` rm `…` ``, `while read f; do rm "$f"; done`, `for f in …; do rm $f; done`, `find … -exec|-execdir|-ok|-okdir rm`, and `truncate`/`dd of=` in any of those positions (also inside `ssh`/`sh -c`/`sudo`/`if`/`( )`). The words of every OTHER segment are treated as its operands:

- the producer **names** a guarded path or file, or a literal search root equal to / inside one (`find ~/.claude …`, `git ls-files ~/.claude`, `echo <path>`, `cat <path>`) → **block**, same as `rm <path>` (allowlist `delete` entries still re-permit). `-execdir`/`-ok`/`-okdir` give the same verdict as `-exec`.
- the producer is a **search** (`find`/`ls`/`tree`/`du`/`fd`/`rg`/`ag`/`grep -r`/`locate`/`git ls-files|ls-tree|status|diff|grep`) whose literal root merely CONTAINS a guarded path (`find ~ …`), or a root held in an unresolved variable (`find "$d"`), or a relative root/implicit cwd that resolves into a guarded directory, or a name filter/glob that could match a guarded bare name (`-name 'README*'`, `*.md`) when the producer works from the cwd → **ask, unverifiable** (id `core.producer-fed-delete`; not demoted by bypass, a block unattended). A filter that cannot match a guarded FILE basename exempts it; a guarded DIRECTORY is never exempted by a filter.
- everything else is untouched, as before: a producer with no protected argument in an unguarded cwd (`find . -name '*.o' | xargs rm`, `find . -type f | xargs rm`, `ls build | xargs rm`, `ls -t | tail | xargs rm`, `git ls-files | xargs rm`) and scratch pipelines (`find /tmp/work … | xargs rm`, `cat /tmp/list | xargs rm`).

**mv / rename (#1127, owner ruling: moving it out of place deletes it).** A SOURCE that is, or CONTAINS, a guarded path is a delete of it → **block**: `mv ~/.claude ~/.claude.bak` and `mv ~/.agentwire /tmp/aw` (a whole directory moves everything in it), `mv * /tmp` (the glob reaches a guarded bare name), `mv -t`, `mv -T`, `git mv` (incl. `git -C`), `rename`, `sudo`/`nohup`/`ssh`/`sh -c`/compound bodies, and mv fed by `xargs`/`find -exec` (file entries only). A DESTINATION that names a guarded FILE entry is overwritten → **block**: `mv`/`cp`/`install`/`rsync`/`ln -f` onto `~/.agentwire/settings.json`, `CLAUDE.md`, `README.md`. Renaming a guarded FILE inside its own directory (`mv ~/.claude/CLAUDE.md ~/.claude/CLAUDE.md.bak`, `mv README.md README.old`) → **ask** (`core.guarded-rename`, an ordinary demotable ask: a rephrase cost, and unattended it fails closed). Files INSIDE a directory entry are runtime state and are not in the mv ladder: `mv ~/.agentwire/cohorts/x.json x.json.held`, moving or copying files into/within `~/.agentwire/{cohorts,inbox,sessions}/…` stay allowed. Copying a guarded file OUT (`cp ~/.claude/CLAUDE.md /tmp/b`) and moving scratch files stay allowed.

**Emptying == deleting (owner ruling).** On a guarded FILE entry these block: `truncate …`, `dd … of=<path>`, and a redirect whose source is a known-empty producer — `: > f`, `: >| f`, `true > f`, `false > f`, `cat /dev/null > f`, `printf '' > f`, `echo -n > f`, `'' > f`, `tee f </dev/null`. **Not closed, by ruling:** a plain `> f` / `echo x > f` (the ordinary edit path stays allowed), and any emptying spelling not listed (`head -c0`, `cp /dev/null f` is covered as an overwrite, `sed -i d f`, an interpreter one-liner). Emptying a file inside a directory entry (state) is allowed.

**Two accepted tradeoffs.** Files and subdirectories inside a directory entry (runtime state such as `~/.claude/projects`, `~/.agentwire/sessions/<name>`) are outside the mv ladder, so they can be moved, renamed or overwritten. In a non-guarded cwd, `find . -type f | xargs rm` (no protected argument, no filter that could match a guarded name) is allowed, as it was before.

**Declared residual** (not closable by a text hook): the CONTENT of a list file (`cat /tmp/list | xargs rm` is judged by where the list lives, not by what it names); `rm $f` loops with non-literal operands (the generic rm rule already refuses them, scratch or not); `cd <dir> && …` is not modelled for relative roots (the hook's cwd resolves them); `rsync --delete` into a guarded directory; the emptying spellings above.

Pinned by `tests/unit/test_damage_control_bypass.py`: a differential (consumer spellings × producers × targets × homes × {default, kill switch off, unattended}) against the direct-operand decision, must-allow controls (scratch pipelines, the narrowed relative producers, runtime-state moves including the real owner command), must-ask controls, and the exec-variant equivalence. Logic lives only in `engine.py` (no rule-YAML change), so a newer ruleset never needs a newer hook.

#### 5. allowedPaths (Granular path-based allowlist)

Paths where path-based protections (zeroAccess, readOnly, noDelete) are bypassed. Each entry specifies which operations are permitted. Hard-blocked bash patterns (like `rm -rf`) are never bypassed by `allow: all` — only the explicit `delete-recursive` operation (below) frees the recursive `rm`, and only for the directories it names. Bypassable bash patterns (like plain `rm`) can be overridden if the target path has the required operation permission.

**Operations**: `all`, `read`, `write`, `edit`, `delete`, `move`, `chmod`; plus `delete-recursive`, which `all` does **not** include (below)

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

**Per-project** (top-level `allowed_paths` in the **protected** `.damagecontrol.yml` at the repo root — NOT `.agentwire.yml`, see [Policy files](../../../docs/wiki/internals/damage-control.md#policy-files--the-protected-control-plane-466) and #467):
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

**A location allowance never re-permits the control plane (#1121).** "Covers it" is judged by the entry's SHAPE at a canonical home (`engine.control_plane_allowlist`, the same anchoring doctor's overlap check uses), not against the live path. Judged live, the shipped `/tmp/*` scratch entry matched `$HOME/.agentwire/damagecontrol.yml` whenever `$HOME` lived under `/tmp` (a CI image, container, sandbox), so `cd ~/.agentwire && touch damagecontrol.yml` was ALLOWED at `HOME=/tmp/x` and blocked at `/Users/x` — the verdict followed where the home sat, not what the target was. An entry now counts only if it names a protected file literally (`/repo/.damagecontrol.yml`) or its glob covers the control plane at the canonical home (`~/.agentwire/*`, `*/.agentwire/*`). Applied at both entry points (`check_protected_command`, `check_protected_path`). Pinned by a differential test (writers × spellings × HOME in `/Users`, `/home`, `/tmp`, `/private/tmp`, `/var/folders`, a symlinked home × kill switch on/off) that must give the same verdict everywhere. The same filter (`guarded_allowlist`) guards `zeroAccessPaths`, `readOnlyPaths` and `noDeletePaths` at every override site (Bash ladders, `check_path`, `check_read_path`, and the MCP path screen that calls them) (#1122). `_anchor_home` never rewrites a bare ancestor of HOME (`/tmp/*` at `HOME=/tmp`, `HOME=/`, a trailing slash): a rewrite must not change what an entry covers, so a live-HOME prefix is rewritten only when it names a literal component below HOME. Stated cost: `/tmp/*` no longer re-permits a zero-access file that sits in /tmp (`cat /tmp/foo.pem` blocks); a literal entry for it still does.

Per-project paths are relative to the project root and resolved to absolute paths before matching.

**Bypassable bash patterns**: Some bash patterns (plain `rm`, `rmdir`, `trash`) are marked `bypassable: true` in their rules YAML. When a command matches a bypassable pattern, the system checks if ALL target paths have the required operation permission (e.g., `delete` for `rm`). If all paths match, the command is allowed. Hard-blocked patterns (like `rm -rf`) are never bypassed by these permissions (only `delete-recursive`, below, frees them).

**Security**: When checking bypassable patterns, ALL paths in the command must have the required permission. A command like `rm /tmp/safe.txt /etc/passwd` is blocked because `/etc/passwd` is not in the allowlist, even though `/tmp/` has delete permission.

**`delete-recursive` — the user-owned opt-in for `rm -rf <regenerable dir>` (#1152).** `rm -rf dist`, `rm -rf node_modules` and `rm -rf .next` are hard-blocked by default and stay so: nothing ships loosened. A human who wants them can name the operation explicitly, per project (`<repo>/.damagecontrol.yml`) or machine-wide (`~/.agentwire/damagecontrol.yml`) — both protected, human-edited:
```yaml
allowed_paths:
  - path: "dist/*"            # project file: relative to the repo root
    allow: [delete-recursive]
  - path: "*/node_modules/*"  # global file: any directory of that name
    allow: [delete-recursive]
```
The recursive/forced `rm` (`-r -R -f -rf -fr`, `--recursive`, `--force`) is then allowed only when **every operand of every `rm` in the command** is covered; `rm -rf dist /etc` blocks. The entry names a *directory* (`dist`), and that directory and everything below it is covered.
- **`allow: all` does NOT include it.** `all` is the pre-existing set (`read write edit delete move chmod`); `delete-recursive` must be named, so no existing `dist/*: all` entry changes meaning. Pinned.
- **What no entry can ever cover** (checked per operand, lexically AND by realpath): the home dir, `/`, the repo root or any ancestor of it, the cwd or a directory containing it, anything outside the project (a per-project entry's own root; for a global entry, the git root of the cwd), any `..`, `.`-style operand (`.`, `./*`), a glob/brace/`$`/backtick operand or anything not statically resolvable, `.git` anywhere in the path, and any symlink between the project root and the operand (a covered root that *is* a symlink included). The control plane and zero-access paths are still judged by their own tiers; the read-only/no-delete ladders (`dist/` is read-only in the bundled rules) are lifted only for the operands the entry covered, never for another path in the same command.
- **Broad entries are refused at load**, not honored: `*`, `**`, `/*`, `~/*`, `*/*`, a glob mid-path, `.git`, a project's own root, anything at or above the home dir. The entry stays in the list marked refused and does nothing; `agentwire doctor` names it. Accepted shapes: a literal directory under an absolute/`~` path or `<project>/name/*`, and `*/<name>/*` for a literal name.
- **Where each entry shape belongs.** `*/<name>/*` (any directory of that name) is for the **global** file only. A project-file entry is relative to the project (`dist/*`, `packages/web/.next/*`); a project entry written `*/x/*` is refused (its glob would sit mid-path under the project root) and doctor flags it.
- **Trust model (owner decision, #1153):** a project's own `.damagecontrol.yml` may grant `delete-recursive` for directories strictly inside its own checkout. A cloned repo can therefore loosen deletion for *its own files only* — the same trust as every existing project `allowed_paths` entry; the never-covered set above stays absolute whoever wrote the entry. `agentwire doctor` prints each project entry found in the current repo ("from this repo's .damagecontrol.yml"), so review it after cloning.
- **A nested repository is never regenerable:** an operand that is, or directly contains, a `.git` is not allowed (one `lexists` of `<operand>/.git`, no tree walk; a repo deeper than that is not detected).
- **Flags:** only `-r -R -f` combinations, `--recursive`, `--force` and `--` are honored; `--no-preserve-root`, `--one-file-system`, `-i`, `-v`… keep the removal blocked.
- **Wrappers are never more permissive than the bare command.** The `rm` must be modelled by the same parser the scratch exemption uses, so `sudo`/`env`/`timeout`/`sh -c`/`ssh`/`xargs`/`if … then`/`for … do`/`( )`/`{ }` around a covered `rm -rf` still block (stricter than the bare form, never looser — stated cost: a covered `rm -rf` inside a `for` loop or `sh -c` keeps blocking). `cd <literal> && rm -rf …` is modelled.
- **Audit:** the allowed row carries `allowed_by: "delete-recursive: <entry path>"`. `agentwire doctor` lists active entries (informational) and flags a refused one `[!!]`.
- **No newer-hook requirement:** hooks that predate the operation never consult it, so the entry is ignored and the removal stays blocked (verified against the pre-#1152 core).
- **`agentwire safety suggest-delete-recursive [--days N] [--min N] [--json]`** reads the audit log, counts blocked `rm -rf` of regenerable-looking directories (dist, build, node_modules, .next, .nuxt, target, __pycache__, .pytest_cache, .turbo, coverage, .cache, out) per project, and **prints** the YAML to paste. Read-only; it never writes policy. Agents may *suggest* running it to the user, never edit policy themselves.
- **Reading the global file:** the global policy file's top-level `allowed_paths` (documented in its template but never read by the hook before) is now merged like the rules-YAML `allowedPaths`. An entry someone wrote there expecting it to work is now live — review that file after upgrading; `agentwire doctor` lists the delete-recursive ones.
- **Not covered by design:** `rmdir` stays on the existing `delete` operation; `find … -delete` and `git clean` are unchanged. A removal assembled at run time (`rm -rf $d`, `for` loops) blocks as it always did.

**Precedence**:
1. Hard-blocked `bashToolPatterns` (no `bypassable` flag) — always blocked, never bypassed by `allow: all` (the one exception: `rm`'s recursive/forced rules, which an explicit `delete-recursive` entry frees for the directories it covers, #1152)
2. Ask patterns (`ask: true`) — prompt for confirmation when a human is present; **fail closed when unattended** (see below)
3. Bypassable `bashToolPatterns` (`bypassable: true`) — check allowlist for required operation
4. `allowedPaths` (global + per-project merged) — if target matches with correct operation, skip path checks, EXCEPT that an entry re-permits a guarded path (control plane, zero-access, read-only, no-delete) only if it names it or its shape covers it at a canonical home (#1121/#1122)
5. `zeroAccessPaths` — block (unless allowlisted with `read`)
6. `readOnlyPaths` — block modifications (unless allowlisted with specific operation)
7. `noDeletePaths` — block deletions (unless allowlisted with `delete`)

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

**Heads-up:** a file in the override directory **replaces the bundled file of the same name** and every other bundled file stays on. Copy from `packages/damage-control-dev/src/damage_control/rules/` only to change a bundled file.

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

## A file that fails to parse is reported, not skipped (#1130, #1137)

A rule, tooldef or policy YAML that does not parse contributes **nothing** — every protection in it is absent, and a skip that says nothing looks identical to a healthy set. `tooldefs/gws.yaml` shipped like that (an unquoted `format: full|…` inside a plain scalar) until #1130.

- **stderr:** `agentwire damage-control: SKIPPED <file>:<line>: <yaml error>` on every load. The print is guarded: a dead stderr never changes a verdict.
- **`load_config` / `safety status`:** the merged config carries `_parse_failures` (`file`, `line`, `error`); `safety status` exposes it as `parse_failures`.
- **`agentwire doctor`:** `[!!] Rule/tooldef/policy files that FAILED TO PARSE` naming file and line, or `[ok] Every rule, tooldef and policy file parses`. This includes the global `damagecontrol.yml` and the nearest project `.damagecontrol.yml`, whose loaders stay fail-secure (a bad file leaves defaults in force) but now say so (`policy_file_parse_failures`).
- **Pin:** `tests/unit/test_rule_files_load.py` loads every shipped rule/tooldef file and asserts none is skipped.

Fix the YAML (quote any scalar containing `: `, `|` or `#`), then `agentwire safety install --yes` if the broken copy is the installed one — a pristine older release reads `outdated` and is brought forward.

### Writing your own tooldef (user-level policy, #1137)

agentwire ships policy only for tools nearly everyone has (git, gh, docker, cloud CLIs…). A tool you authed by hand for your own reasons — `gws` is the worked example — is yours to guard, in your user dirs, not ours:

| Drop it in | What it is |
|---|---|
| `~/.agentwire/tooldefs/<tool>.yaml` | A tooldef: `commands:` with `cmd:` (fixed prefix tokens; stops at the first `<placeholder>` or `[optional]`), `access: read`, `write` or `blocked`, and an optional `id:`. Each `write` line becomes an anchored **ask** rule on that prefix; `read` lines carve subcommands out of a shorter write prefix; `blocked` lines are documentation only. |
| `~/.agentwire/damage-control/<tool>.yaml` | Rules: `bashToolPatterns:` with a regex `pattern:`, `anchored: true`, `ask: true` (omit for a hard block), and a stable `id:` you can name in `unattended_allow`. Use this when the operation needs a *grammar* (a verb after any resource path) that a fixed prefix can't express. |

Things that bite:

- **A user dir replaces the bundled dir.** `~/.agentwire/tooldefs/` (and `damage-control/`) win as a whole once they hold any `*.yaml`. Run `agentwire safety install --yes` first so the bundled files sit beside yours; `agentwire doctor` flags missing bundled files as `[!!]`.
- **Rules are first-match**, rules before tooldefs, so a rule file shadows tooldef lines for the same command.
- **A parse failure is loud**: `SKIPPED <file>:<line>` on stderr and `[!!]` in `agentwire doctor`.
- **Heal leaves your file alone.** `safety install` only touches names the bundle ships; a same-named file that matches no shipped version is `unknown` and is held back (`--force` replaces it, keeping a `.bak`). A name the bundle used to ship and no longer does (gws, removed in #1137) is never deleted; `doctor` notes a pristine leftover with a quiet `[..]` and calls an edited one yours.
- **Unattended grants** name a rule `id:` (`unattended_allow: [<id>]` in the policy file or task). Give the intended write its own id.

Worked example, ready to copy: [`examples/gws-tooldef.yaml`](examples/gws-tooldef.yaml) (reference and reads) and [`examples/gws-rules.yaml`](examples/gws-rules.yaml) (verb-based enforcement: send, create, insert, patch, update, modify, trash, import, batch*, delete, sharing changes, `+helper` forms; reads, `schema`, `--dry-run` and `--help` allowed; calendar `events insert|quickAdd` is `ask` under id `gws.calendar-event-create`). They are documentation, not part of the loaded bundled set.

## Rule groups and packs (#1141)

Damage control used to install every bundled rule and tooldef everywhere, so an AWS rule applied to someone who never runs `aws`. Each bundled file now belongs to exactly one registry entry (`agentwire/safety/groups.py`; a sweep test fails on an orphan or a double owner):

| kind | on by default | who may change it |
|---|---|---|
| kernel group (`control-plane`, `core`) | always | nobody |
| default group (`git`, `uv`, `registry`, `agentwire`, `payloads`, `remote`, `mcp-tools`, `outbound`) | yes | human opt-out: `agentwire safety groups disable <g>` |
| pack (`aws`, `gcp`, `firebase`, `cloud-hosting`, `databases`, `containers`, `infrastructure`, `publish`) | no | human opt-in: `agentwire safety packs enable <p>` |

**State** lives in the protected `~/.agentwire/damagecontrol.yml` (`packs:` = enabled packs, `disabled_groups:`). **Mutations are human-only**: the command refuses without a tty (`tasks_cli._host_context_ok`, same gate as `tasks promote`) and `agentwire safety (packs|groups) (enable|disable)` is a `PROTECTED_COMMAND_PATTERNS` entry, so an agent's Bash tool is stopped first. `list`/`status` (CLI `--json`, MCP `safety_packs_list`/`safety_groups_list`) are read-only. Agents are told (CLAUDE.md, the `agentwire` role) to *suggest* a pack when the user's work touches that vendor.

**No deploy skew by construction.** The hooks never read this state; they load whatever is installed in `~/.agentwire/{damage-control,tooldefs}`. "Pack off" is "files not installed", so rule loading and hooks cannot disagree. (Pre-package text: the retired `heal` was the only writer; since M5 the hooks read the packs state at run time and nothing is copied.) It installed the wanted files through the usual three-way baselines, and for a disabled entry removes the installed copy **only if pristine** (byte-equal to a shipped version) — an edited file is reported (`*_kept_edited`) and left, because it may be the user's own. `rules_drift`/`tooldefs_drift` skip a disabled entry's files, so a deliberate "off" is never `[!!] missing`.

**Migration.** While `packs:` is absent and anything is already installed (every pre-packs install), the first heal records **every** pack as enabled and writes the key — not just packs with files present, because before packs heal reinstalled any missing bundled file, so a partial install must come out of an upgrade whole. A fresh machine (nothing installed) starts with every pack off. Upgrading changes no decision; the owner disables what they don't use. Read-only surfaces (`doctor`, `list`) show that same preview via `packs.effective_state`, so no reader reports a pack as off while its files are live. Pinned by an upgrade test that compares decisions before/after on an old all-files layout.

**Surfaces.** `doctor` prints enabled packs, available packs, vendor CLIs found with the pack off, and opted-out groups — `[..]`, informational, never scored. `agentwire init` lists packs and suggests those whose CLI is on `PATH`.

**An unparseable policy file fails toward protection and is never written (#1209 M5).** `read_state` catches `yaml.YAMLError`; a broken file means every group AND every pack is on (the hook cannot know which packs the owner decided, and a typo must not drop them). This differs deliberately from an *absent* `packs:` key, which is genuinely undecided and loads the default groups plus only the host's default packs (epic section 4.5 wording: a failure to read the policy never means "everything off"; here it means "everything on"). No migration is written over it, `packs|groups enable|disable` refuse, and `doctor` reports `[!!] ... does not parse`.

**Disabled group vs typo.** `DEFAULT_UNATTENDED_ALLOW` ids (`git.*`, `gh.*`, `uv.*`, `outbound.*`) stop existing when their group is disabled. `doctor` and `tasks review` say "group 'git' disabled", never "names no rule / typo" (`lint.split_missing_ids`).

**Generic destructive ops live in default groups, not packs.** Swept every pack for ops a fresh install would silently lose: `gh repo delete` moved from `cloud-hosting` to `git.yaml` (id now `git.gh-repo-delete-deletes-repository`); `npm unpublish` moved to the new default `registry` group, which also owns the `npm.yaml` tooldef (`npm publish` ask-tier). Left in packs on purpose, as vendor-specific (the block-tier `prisma migrate reset` / `flyway clean` stay in `databases` beside `dropdb` / `FLUSHALL`: the pack boundary is the vendor CLI, and moving two of them would split it for no consistent rule; the migration-runner binaries are in the pack's `binaries` so doctor/init suggest it): cloud/infra deletes and deploys, `docker`/`kubectl`, DB CLIs and migration runners, non-npm publish. **Limits, stated.** `gws` is not bundled; the owner-written example is in `docs/wiki/internals/examples/`. With no installed rules dir at all the loader falls back to the bundled dir (everything on) — a dev/test-only state.
