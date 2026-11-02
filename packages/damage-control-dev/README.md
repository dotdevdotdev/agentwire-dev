# damage-control-dev

Damage control for AI coding agents (Claude Code): a PreToolUse guard that blocks or asks before destructive commands and protected-path writes.

    damage-control-dev init       # register the hooks, self-test them, record the install
    damage-control-dev status     # read-only drift report
    damage-control-dev doctor --fix
    damage-control-dev uninstall

## Where things live

Everything the installer keeps is under the config dir, `~/.damage-control/` (or `$DAMAGE_CONTROL_HOME`):

- `damagecontrol.yml`: the protected policy (kill switch, `packs:`, `disabled_groups:`)
- `damage-control/`, `tooldefs/`: your rule overlays
- `state/install.json`: what `init` recorded (interpreter, ruleset digest, per-scope write hash); `state/backups/` holds first-write backups of your settings file
- `bin/damage-control-breakglass`: stdlib-only script that unregisters the hooks if the venv dies

A policy with no `packs:` key loads the default groups and no vendor packs; an unreadable policy loads everything.
