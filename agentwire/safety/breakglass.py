#!/usr/bin/env python3
"""Break-glass for the damage-control hooks: works when the AgentWire venv does not.

The hooks run ``<venv python> -I -m damage_control.hook <kind>`` with ``"onFailure": "block"``,
so a venv that is gone (``uv tool install --force`` died halfway, a managed Python was
uninstalled) makes Claude Code refuse EVERY tool call, including the ones you would use to
repair it. This file is the way out. It is standard library only and is copied to
``~/.agentwire/bin/damage-control-breakglass`` by ``agentwire hooks install``; run it with the
system ``python3`` from an ordinary terminal (not through Claude Code):

    python3 ~/.agentwire/bin/damage-control-breakglass status
    python3 ~/.agentwire/bin/damage-control-breakglass unregister

``unregister`` removes exactly the damage-control entries from ``~/.claude/settings.json`` (the
file is backed up first, written atomically, and an unparseable file is refused, never
rewritten). With them gone Claude Code stops calling the hooks, so **damage control is OFF**
until you repair the install and re-register:

    uv tool install --force --reinstall agentwire-dev   # or: agentwire rebuild
    agentwire hooks install

It is a human's tool: a policed agent cannot reach it, because the hook that guards
``~/.claude/settings.json`` and ``~/.agentwire`` blocks the writes it makes.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_MODULE = "damage_control.hook"
LEGACY_SUFFIX = "-tool-damage-control.py"
BACKUP_SUFFIX = ".breakglass.bak"


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def is_ours(hook) -> bool:
    """A hook dict this system registered: the current exec form, or a pre-#1209-M4 script path."""
    if not isinstance(hook, dict):
        return False
    args = hook.get("args")
    if isinstance(args, list) and HOOK_MODULE in args:
        return True
    return str(hook.get("command", "")).endswith(LEGACY_SUFFIX)


def load(path: Path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    parsed = json.loads(raw)  # ValueError propagates: an unparseable file is refused
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} is not a JSON object")
    return raw, parsed


def owned(settings) -> list:
    pre = (settings.get("hooks") or {}).get("PreToolUse") if isinstance(settings.get("hooks"), dict) else None
    found = []
    for entry in pre if isinstance(pre, list) else []:
        for hook in entry.get("hooks", []) if isinstance(entry, dict) and isinstance(entry.get("hooks"), list) else []:
            if is_ours(hook):
                found.append((entry.get("matcher"), hook))
    return found


def runs(interpreter: str) -> bool:
    try:
        return subprocess.run(
            [interpreter, "-I", "-c", "import damage_control"], capture_output=True, timeout=20,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_status() -> int:
    path = settings_path()
    try:
        _, settings = load(path)
    except (ValueError, OSError) as e:
        print(f"{path}: cannot read it ({e}); nothing to report")
        return 1
    entries = owned(settings)
    if not entries:
        print(f"No damage-control hooks are registered in {path}.")
        return 0
    interpreters = sorted({h.get("command", "") for _, h in entries if h.get("args")})
    print(f"{len(entries)} damage-control hook entries in {path}")
    code = 0
    for interpreter in interpreters:
        ok = runs(interpreter)
        print(f"  interpreter {interpreter}: {'ok' if ok else 'DEAD — every tool call is being blocked'}")
        code |= 0 if ok else 1
    if any(not h.get("args") for _, h in entries):
        print("  legacy script registrations present (pre-package install)")
    if code:
        print("Fix: run `unregister` below, repair the install, then `agentwire hooks install`.")
    return code


def cmd_unregister() -> int:
    path = settings_path()
    try:
        raw, settings = load(path)
    except (ValueError, OSError) as e:
        print(f"REFUSED: {path} cannot be read as JSON ({e}). Edit it by hand: delete the "
              f"PreToolUse entries whose command runs `-m {HOOK_MODULE}`.")
        return 1
    if not owned(settings):
        print("Nothing to remove.")
        return 0
    pre = settings["hooks"]["PreToolUse"]
    kept = []
    for entry in pre:
        hooks = entry.get("hooks") if isinstance(entry, dict) else None
        if isinstance(hooks, list):
            remaining = [h for h in hooks if not is_ours(h)]
            if len(remaining) != len(hooks):
                if remaining:
                    entry = dict(entry, hooks=remaining)
                else:
                    continue
        kept.append(entry)
    settings["hooks"]["PreToolUse"] = kept
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if raw is not None and not backup.exists():
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(settings, indent=2))
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"Removed the damage-control hooks from {path} (backup: {backup.name}).")
    print("DAMAGE CONTROL IS NOW OFF. Repair the install, then re-register:")
    print("  uv tool install --force --reinstall agentwire-dev   # or: agentwire rebuild")
    print("  agentwire hooks install")
    return 0


def main(argv) -> int:
    commands = {"status": cmd_status, "unregister": cmd_unregister}
    if len(argv) != 1 or argv[0] not in commands:
        print(__doc__)
        return 2
    return commands[argv[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
