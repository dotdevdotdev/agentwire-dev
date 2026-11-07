# Migrating an existing install to standalone damage-control-dev

Owner runbook (#1313, part of #1307). Do this **before** you rebuild agentwire from a main that contains #1316.

## Why

Today the guard hooks in `~/.claude/settings.json` run the engine from agentwire's own tool venv
(`~/.local/share/uv/tools/agentwire-dev/bin/python -I -m damage_control.hook ...`). Once agentwire stops
depending on `damage-control-dev` (#1316) and is rebuilt, that venv has no `damage_control`, and because
the hooks are registered `onFailure: block`, **every guarded tool call is refused**.

agentwire no longer registers, repairs or removes an engine registration. `agentwire hooks install` and
`agentwire doctor` leave existing guard entries in user settings exactly as they are. Moving the guard
to its own interpreter is therefore a manual, one-time step, and it is yours.

## Steps

1. **Install the engine standalone**, in its own tool environment (not agentwire's):

   ```bash
   uv tool install damage-control-dev
   damage-control-dev --version        # 0.6.0 or newer
   ```

2. **Re-register its hooks against that interpreter.** From a normal terminal, outside any agent session:

   ```bash
   damage-control-dev init --dry-run   # read the diff first
   damage-control-dev init
   damage-control-dev status
   ```

   `init` replaces the entries that point at agentwire's venv with ones pointing at the standalone
   interpreter, runs the hook selftest, and records the install. Its old `--profile` argument (the
   agentwire profile manifest under `~/.agentwire/`) is no longer needed.

3. **Enable the agentwire pack** (human terminal only; agents never run this):

   ```bash
   damage-control-dev packs list
   damage-control-dev packs enable agentwire
   ```

   This carries the agentwire-specific protections that used to live in this repo (the `~/.agentwire`
   control-plane and secret paths, the `agentwire tasks promote` block, outbound `email`/`quo` verbs,
   MCP tool policy). *Unverified from this repo:* the exact pack name and command are the ones in the
   damage-control-dev 0.6.0 documentation; check `packs list` and follow it if it differs.

4. **Verify** before touching agentwire:

   ```bash
   damage-control-dev doctor
   damage-control-dev check 'rm -rf /'                       # blocked
   damage-control-dev check 'cat ~/.agentwire/.env'          # blocked once the pack is on
   grep -n 'damage_control' ~/.claude/settings.json          # every entry names the standalone python
   ```

   Then start one throwaway Claude Code session and confirm a blocked command is still refused.

5. **Only then** rebuild agentwire:

   ```bash
   git pull --ff-only && agentwire rebuild && agentwire portal restart --dev
   ```

6. Optional cleanup of files agentwire used to write and no longer reads: `~/.agentwire/damagecontrol-profile.json`,
   `~/.agentwire/damagecontrol-install.json`, `~/.claude/hooks/outcome_logger.py` (and its `PostToolUse` /
   `PostToolUseFailure` / `PermissionDenied` entries in settings, which agentwire will no longer maintain).
   Leaving them is harmless.

## If you rebuilt first (every tool call is refused)

The hooks fail closed, so an agent cannot fix this; use a normal terminal.

```bash
python3 ~/.agentwire/bin/damage-control-breakglass status
python3 ~/.agentwire/bin/damage-control-breakglass unregister     # removes the guard entries so tools run again
```

Then do steps 1-4 above, and re-run the verification. `agentwire doctor --fix` no longer exists; the
standalone `damage-control-dev doctor` is the repair tool now.

## What changed in agentwire

- `agentwire safety ...`, the `safety_packs_list` / `safety_groups_list` MCP tools, `hooks needs-human`, the
  unattended-block email digest and the outcome logger are removed.
- `agentwire-permission.sh` no longer auto-approves under bypass or auto. A request that reaches it goes
  to the portal for a human, in every mode.
- `agentwire/safety/provenance.py` became `agentwire/provenance.py`; the "only the canonical install may
  write machine-global files" rule is unchanged, for agentwire's own hooks.
