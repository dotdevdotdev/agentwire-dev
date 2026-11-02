# Extending: profiles, the seam and the runtime package

How a host (an application embedding the engine) adds what is dangerous around itself, and how the hooks run. Moved verbatim from AgentWire's `docs/wiki/internals/damage-control.md`.

## The seam: Profile, Guard.decide, adapter (#1209 M3)

The engine knows what is dangerous in general; the host adds what is dangerous around itself.

- **`packages/damage-control-dev/src/damage_control/profile.py`** — `Profile`: protected paths, control-plane words, benign-path patterns,
  `home_alias`, protected commands, the unattended env names and default grant ids, extra rules
  dirs, MCP command templates, groups/packs, the grant hint and the block notifier (an argv run detached with the event as JSON on its stdin: `{"event": "unattended_block", "reason", "rule_id", "command"}`). It only ADDS to
  the kernel in `engine.py` (the project policy file, Claude Code's `settings.json` and hooks dir,
  the profile's own policy file), so no profile can remove or loosen a kernel entry, and the
  control-plane / protected-command checks still run before the kill switch.
- **A host's profile module** — e.g. AgentWire's `agentwire/safety_ext.py` (`build_profile`, `write_manifest`, `install`, `status`, `activate`): its WHOLE surface. In-process host code activates it; hooks load the manifest. The engine ships an empty default grant list. The profile fields are: protected paths and commands, `home_alias`, the unattended env names and default grant ids, the grant hint, the block notifier, extra rules dirs, MCP command templates, groups/packs and default packs, the config home, and the `audit` field (log dir, session/agent env names, session-record dir).
- **`packages/damage-control-dev/src/damage_control/policy.py`** — `Guard.decide(ToolCall, Context) -> Verdict`, the ladder that used to be
  copy-pasted into the five hook `main()`s. Pure: it returns the audit row, stderr lines and
  notification as data. The engine is passed in, so tests drive it with a stub engine
  (`tests/unit/test_policy_ladder.py`).
- **`packages/damage-control-dev/src/damage_control/adapter.py`** — payload -> `ToolCall`/`Context`, and `render(verdict)`, the one place a
  decision becomes an exit code. Each hook `main()` is `load config -> parse -> decide -> render`.
- **Sources** — `load_config` takes a list of rule dirs and one or more tooldef dirs, in precedence
  order (bundled, profile layers, host override). Merging is additive; a repeated rule id across
  sources is recorded under `_duplicate_rule_ids`; an `allowedPaths` entry in an extension source that
  would re-permit the control plane is dropped with a load failure.

The package modules must stay stdlib + (lazy) yaml and import nothing from `agentwire`
(`tests/unit/test_engine_standalone.py`, which also runs on the Python 3.9 CI job). The profile reaches
the hook as DATA: the host's installer writes its profile to a protected manifest
(AgentWire: `~/.agentwire/damagecontrol-profile.json`) and the registered command names it with `--profile`.

## Runtime package, exec-form hooks and break-glass (#1209 M4)

**Registration.** `damage-control-dev init` (AgentWire: `agentwire hooks install` → `safety_ext.install`, which runs the package's `init` under AgentWire's profile) writes, per matcher, one atomic
`settings.json` edit: `{type: command, command: <absolute interpreter>, args: [-I, -m, damage_control.hook,
<kind>, --profile, <manifest>], timeout, onFailure: block}`, replacing any pre-package script
registration in the same write. It first runs the SAME command as a selftest (every kind exits 0 on a
tool it does not gate; Bash blocks `rm -rf`); a failed selftest changes nothing. The interpreter it
recorded is in `~/.agentwire/damagecontrol-install.json`, which `doctor` re-verifies.
**The exec-form (`command` + `args`) registration shape is taken from the epic and was not exercised
against a live Claude Code in the build session; the first deploy should confirm one real tool call.**

**Fail-closed layers.** (1) `hook.py` imports only `os`/`sys`; any failure importing the engine, or a
module that exits 0 at import, is exit 2. (2) `-I` keeps the cwd and `PYTHON*` out of `sys.path` (a planted
`json.py` is ignored). (3) PyYAML is imported lazily and only for a human's YAML overlay. (4) Anything
before Python runs — interpreter gone, package gone — is `onFailure: block`, which refuses every tool call.
`tests/unit/test_hook_fail_closed_matrix.py` pins layers 1–3, each mutation-checked.

**Break-glass.** When layer 4 fires, run from an ordinary terminal (not through Claude Code):

```
python3 ~/.agentwire/bin/damage-control-breakglass status       # names the dead interpreter
python3 ~/.agentwire/bin/damage-control-breakglass unregister   # removes ONLY our hook entries (backup: settings.json.breakglass.bak)
uv tool install --force --reinstall agentwire-dev && agentwire hooks install   # or: agentwire doctor --fix
```

`unregister` leaves damage control OFF until the re-register. The script is stdlib-only and lives
outside the venv; it refuses an unparseable settings file. `tests/unit/test_breakglass.py` builds a real
venv, registers against it, deletes it and drives the script with another interpreter.

**Rules.** Authored as YAML, committed beside generated JSON (`build_rules_json.py`; drift fails
`test_rules_json_in_sync.py`). The hook reads JSON. Packs and groups are read from `damagecontrol.yml` on
every call, so `packs enable|disable` needs no heal; an absent `packs:` key means undecided and loads the default groups plus only the host's `default_packs`
(none for the generic profile; AgentWire's is `tmux`). #1209 M5 flipped this from "every pack"; a machine that has
decided its packs is unaffected.

**Installer (M5).** `damage-control-dev init|status|doctor|uninstall` (module `damage_control.cli`) registers the
exec-form hooks through `harness_config.edit_json_config` (parse, structural merge keyed on the package module name,
atomic write, re-read verify, concurrent-writer detection; unparseable input is refused byte-identical). `init`
refuses below Claude Code 2.1.295 (no `onFailure`), a downgrade, a dev/ephemeral source without `--allow-dev-source`,
and entries another installer owns (a different `--profile`); it self-tests the registered command before writing and
records `<config>/state/install.json` (interpreter, ruleset digest by stable id, per-scope `written_sha256` so
`uninstall` restores the original bytes only while the file is still exactly what init wrote). `init --refresh` prints
the ruleset diff by id; a removed rule, a lowered tier, a new allow-path or a new default grant is marked `!!`.
State lives under the profile's config dir (`~/.damage-control/state/` for the generic profile; `$DAMAGE_CONTROL_HOME/state/` when set), not XDG. agentwire's own `hooks install` is unchanged until M6.
