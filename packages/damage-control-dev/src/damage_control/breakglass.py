#!/usr/bin/env python3
"""Break-glass for the damage-control hooks: works when the venv they run from does not.

The hooks run ``<venv python> -I -m damage_control.hook <kind>`` with ``"onFailure": "block"``,
so a venv that is gone (``uv tool install --force`` died halfway, a managed Python was
uninstalled) makes Claude Code refuse EVERY tool call, including the ones you would use to
repair it. This file is the way out. It is standard library only and ``init`` copies it to
``<config dir>/bin/damage-control-breakglass``. Run it with the system ``python3`` from an
ordinary terminal (not through Claude Code):

    python3 damage-control-breakglass status     [--settings PATH]
    python3 damage-control-breakglass unregister [--settings PATH]

``unregister`` removes exactly the damage-control entries (the file is backed up first,
written atomically, and an unparseable file is refused, never rewritten). With them gone
Claude Code stops calling the hooks, so **damage control is OFF** until you repair the
install and run ``damage-control-dev init``.

It is a human's tool: the hook guards ``~/.claude/settings.json``, so a policed agent
cannot rewrite it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_MODULE = "damage_control.hook"
BACKUP_SUFFIX = ".breakglass.bak"


def default_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


def is_ours(hook) -> bool:
    args = hook.get("args") if isinstance(hook, dict) else None
    return isinstance(args, list) and HOOK_MODULE in args


def load(path: Path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    parsed = json.loads(raw.decode("utf-8-sig"))  # ValueError propagates: an unparseable file is refused
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} is not a JSON object")
    return raw, parsed


def owned(settings) -> list:
    hooks = settings.get("hooks")
    pre = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    found = []
    for entry in pre if isinstance(pre, list) else []:
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        for hook in inner if isinstance(inner, list) else []:
            if is_ours(hook):
                found.append((entry.get("matcher"), hook))
    return found


def runs(interpreter: str) -> bool:
    try:
        return subprocess.run([interpreter, "-I", "-c", "import damage_control"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_status(path: Path) -> int:
    try:
        _, settings = load(path)
    except (ValueError, OSError) as e:
        print(f"{path}: cannot read it ({e}); nothing to report")
        return 1
    entries = owned(settings)
    if not entries:
        print(f"No damage-control hooks are registered in {path}.")
        return 0
    print(f"{len(entries)} damage-control hook entries in {path}")
    code = 0
    for interpreter in sorted({h.get("command", "") for _, h in entries}):
        ok = runs(interpreter)
        print(f"  interpreter {interpreter}: {'ok' if ok else 'DEAD — every tool call is being blocked'}")
        code |= 0 if ok else 1
    if code:
        print("Fix: run `unregister`, repair the install, then `damage-control-dev init`.")
    return code


def cmd_unregister(path: Path) -> int:
    try:
        raw, settings = load(path)
    except (ValueError, OSError) as e:
        print(f"REFUSED: {path} cannot be read as JSON ({e}). Edit it by hand: delete the "
              f"PreToolUse entries whose command runs `-m {HOOK_MODULE}`.")
        return 1
    if not owned(settings):
        print("Nothing to remove.")
        return 0
    kept = []
    for entry in settings["hooks"]["PreToolUse"]:
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        if isinstance(inner, list):
            remaining = [h for h in inner if not is_ours(h)]
            if len(remaining) != len(inner):
                if not remaining:
                    continue
                entry = dict(entry, hooks=remaining)
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
    print("  damage-control-dev init")
    return 0


def main(argv) -> int:
    args = list(argv)
    path = default_settings()
    if "--settings" in args:
        i = args.index("--settings")
        if i + 1 >= len(args):
            print(__doc__)
            return 2
        path = Path(args[i + 1])
        del args[i:i + 2]
    commands = {"status": cmd_status, "unregister": cmd_unregister}
    if len(args) != 1 or args[0] not in commands:
        print(__doc__)
        return 2
    return commands[args[0]](path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
