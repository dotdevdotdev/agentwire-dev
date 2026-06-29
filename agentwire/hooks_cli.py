"""CLI for hook/skill installation — ``agentwire hooks ...``.

Installs and heals agentwire-owned Claude Code integration: the permission
hook, idle handler, queue processor, slash commands, and global skills. These
files are agentwire-owned (no user edits to preserve), so any drift from the
packaged source is replaced.
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
import subprocess
from pathlib import Path

CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks"
CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def get_hooks_source() -> Path:
    """Get the path to the hooks directory in the installed package."""
    # First try: hooks directory inside the agentwire package
    package_dir = Path(__file__).parent
    hooks_dir = package_dir / "hooks"
    if hooks_dir.exists():
        return hooks_dir

    # Fallback: try importlib.resources (for installed packages)
    try:
        with importlib.resources.files("agentwire").joinpath("hooks") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find hooks directory in package")


def get_commands_source() -> Path:
    """Get the path to the commands directory in the installed package."""
    package_dir = Path(__file__).parent
    commands_dir = package_dir / "commands"
    if commands_dir.exists():
        return commands_dir

    try:
        with importlib.resources.files("agentwire").joinpath("commands") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find commands directory in package")


def install_commands(force: bool = False) -> list[str]:
    """Symlink bundled slash commands into ~/.claude/commands/.

    Returns list of command names that were installed or updated.
    """
    try:
        commands_source = get_commands_source()
    except FileNotFoundError:
        return []

    CLAUDE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)

    installed = []
    for src_file in commands_source.glob("*.md"):
        target = CLAUDE_COMMANDS_DIR / src_file.name
        if target.exists() or target.is_symlink():
            if target.is_symlink() and target.resolve() == src_file.resolve() and not force:
                continue  # Already correctly symlinked
            target.unlink()

        target.symlink_to(src_file.resolve())
        installed.append(src_file.stem)

    return installed


def get_skills_source() -> Path:
    """Get the path to the skills directory in the installed package.

    Mirrors get_commands_source(): resolves to the bundled package dir so the
    installed symlink is auto-current after `agentwire rebuild` (which reinstalls
    the wheel), never a transient checkout path.
    """
    package_dir = Path(__file__).parent
    skills_dir = package_dir / "skills"
    if skills_dir.exists():
        return skills_dir

    try:
        with importlib.resources.files("agentwire").joinpath("skills") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find skills directory in package")


def _managed_global_skills() -> list[str]:
    """Agentwire-owned skills that belong GLOBALLY in ~/.claude/skills/.

    Only `wiki` is global — the wiki store lives at ~/.agentwire/wiki/ and is
    usable from any session. The agentwire-* skills stay project-scoped (shipped
    via the repo's .claude/skills/, discovered per-project) and are NOT installed
    here. Third-party skills (cua-driver, shadcn-ui, …) are never touched.
    """
    return ["wiki"]


def _managed_skill_state(target: Path, source: Path) -> str:
    """Drift state of a managed global skill DIRECTORY: missing | stale | ok.

    Skills are directories, so unlike _managed_file_state this never compares
    bytes. A symlink is ok only when it resolves to the packaged source; a real
    directory (the hand-placed pre-#475 state) or a symlink pointing elsewhere is
    stale and must be removed before re-symlinking.
    """
    if target.is_symlink():
        if not target.exists():
            return "stale"  # dangling symlink
        return "ok" if target.resolve() == source.resolve() else "stale"
    if not target.exists():
        return "missing"
    return "stale"  # real dir/file occupying the slot


def _remove_skill_target(target: Path) -> None:
    """Clear whatever occupies a skill slot — symlink, real dir, or stray file."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def install_skills(force: bool = False, copy: bool = False) -> dict[str, str]:
    """Install/refresh agentwire-owned global skills into ~/.claude/skills/.

    Each managed skill is a directory installed as a symlink (or copied with
    --copy) pointing at the packaged source, drift-aware: a correct symlink is
    left alone, a real-dir / wrong-symlink target is replaced.

    Returns {name: "installed" | "updated" | "current" | "missing-source"}.
    """
    try:
        skills_source = get_skills_source()
    except FileNotFoundError:
        print("  Warning: skills directory not found, skipping skill installation")
        return {}

    results: dict[str, str] = {}
    for name in _managed_global_skills():
        source = skills_source / name
        if not source.exists():
            print(f"  Warning: skill '{name}' not found in package, skipping")
            results[name] = "missing-source"
            continue

        target = CLAUDE_SKILLS_DIR / name
        state = _managed_skill_state(target, source)
        if state == "ok" and not force:
            results[name] = "current"
            continue

        existed = target.exists() or target.is_symlink()
        CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        _remove_skill_target(target)
        if copy:
            shutil.copytree(source, target)
        else:
            target.symlink_to(source.resolve(), target_is_directory=True)
        results[name] = "updated" if existed else "installed"

    return results


def skill_drift() -> dict[str, str]:
    """Drift state of agentwire-owned global skills.

    Returns {name: ok|stale|missing|source-unavailable}. Mirrors
    safety_commands.*_drift() so `agentwire doctor` can flag a hand-placed or drifted
    skill the same way it flags hook drift.

    `source-unavailable` means the packaged skill can't be resolved in the
    running context — e.g. doctor invoked from a SOURCE checkout, where skills
    only exist inside the built wheel (`agentwire/skills/`), not on disk. That is
    NOT a drift problem: there is nothing to install from, so doctor skips it
    rather than crying "missing". `missing`/`stale` are reserved for the case
    where the source IS resolvable (installed tool) and the installed copy is
    genuinely absent or wrong.
    """
    try:
        skills_source = get_skills_source()
    except FileNotFoundError:
        return {name: "source-unavailable" for name in _managed_global_skills()}

    drift: dict[str, str] = {}
    for name in _managed_global_skills():
        source = skills_source / name
        target = CLAUDE_SKILLS_DIR / name
        if not source.exists():
            drift[name] = "source-unavailable"
            continue
        drift[name] = _managed_skill_state(target, source)
    return drift


def register_hook_in_settings(event: str, hook_name: str) -> bool:
    """Register a hook under `event` in Claude's settings.json.

    Returns True if settings were updated, False if already configured.

    Claude Code hook format:
    {
      "hooks": {
        "<event>": [
          {
            "matcher": ".*",
            "hooks": [
              {"type": "command", "command": "~/.claude/hooks/<hook_name>"}
            ]
          }
        ]
      }
    }
    """
    settings_file = Path.home() / ".claude" / "settings.json"
    # Use ~ for portability
    hook_command = f"~/.claude/hooks/{hook_name}"

    # Load existing settings or create new
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    # Ensure hooks structure exists
    if "hooks" not in settings:
        settings["hooks"] = {}
    if event not in settings["hooks"]:
        settings["hooks"][event] = []

    # Check if already registered (check nested hooks array)
    for entry in settings["hooks"][event]:
        if "hooks" in entry:
            for h in entry["hooks"]:
                if h.get("command") == hook_command:
                    return False  # Already registered

    # Add hook with correct Claude Code format
    hook_entry = {
        "matcher": ".*",
        "hooks": [
            {"type": "command", "command": hook_command}
        ]
    }
    settings["hooks"][event].append(hook_entry)

    # Write back
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(settings, indent=2))

    return True


# Agentwire-owned files deployed by `hooks install`. Each entry:
# (filename in agentwire/hooks/, target directory, settings.json event or None)
def _managed_hook_files() -> list[tuple[str, Path, str | None]]:
    return [
        ("agentwire-permission.sh", CLAUDE_HOOKS_DIR, "PermissionRequest"),
        ("idle-handler.sh", CLAUDE_HOOKS_DIR, "Notification"),
        ("queue-processor.sh", Path.home() / ".agentwire", None),
    ]


def _managed_file_state(target: Path, source: Path) -> str:
    """Drift state of an agentwire-managed installed file: missing | stale | ok.

    Symlinks are ok when they resolve to the packaged source; regular files
    are ok when their content matches it byte-for-byte.
    """
    if target.is_symlink():
        if not target.exists():
            return "stale"  # dangling symlink
        return "ok" if target.resolve() == source.resolve() else "stale"
    if not target.exists():
        return "missing"
    try:
        return "ok" if target.read_bytes() == source.read_bytes() else "stale"
    except OSError:
        return "stale"


def _install_managed_file(source: Path, target: Path, force: bool = False, copy: bool = False) -> bool:
    """Install or refresh an agentwire-owned file (symlink by default).

    These files carry no user edits to preserve — any drift from the packaged
    source is replaced. Returns True if the target was created or updated.
    """
    if not force and _managed_file_state(target, source) == "ok":
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    if copy:
        shutil.copy2(source, target)
    else:
        target.symlink_to(source.resolve())
    target.chmod(0o755)
    return True


def install_hooks(force: bool = False, copy: bool = False) -> dict[str, str]:
    """Install/refresh all agentwire-owned hook files + settings.json registration.

    Returns {filename: "installed" | "updated" | "current" | "missing-source"}.
    """
    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        print("  Warning: hooks directory not found, skipping hook installation")
        return {}

    results: dict[str, str] = {}
    for hook_name, target_dir, event in _managed_hook_files():
        source = hooks_source / hook_name
        if not source.exists():
            print(f"  Warning: {hook_name} not found in package, skipping")
            results[hook_name] = "missing-source"
            continue

        target = target_dir / hook_name
        existed = target.exists() or target.is_symlink()
        if _install_managed_file(source, target, force=force, copy=copy):
            results[hook_name] = "updated" if existed else "installed"
        else:
            results[hook_name] = "current"

        if event:
            register_hook_in_settings(event, hook_name)

    # Heal the full damage-control surface (hook scripts + rules + tooldefs +
    # PreToolUse matchers), not just the settings.json matchers. This closes the
    # documented post-rebuild gap: CLAUDE.md tells users to re-run `hooks install`
    # after a rebuild, so it must actually sync the DC files/rules — drift-aware,
    # never clobbering a customized rule.
    try:
        from agentwire.safety_commands import heal_damage_control
        heal_damage_control(quiet=True)
    except Exception:
        pass

    # Global skills (currently just /wiki) are agentwire-owned too, and rotted
    # silently because nothing ever resynced the hand-placed copies. Heal them
    # on the same install pass — drift-aware, like the hooks above.
    results.update(install_skills(force=force, copy=copy))

    return results


def cmd_hooks_install(args) -> int:
    """Install agentwire-owned hook files and slash commands for AgentWire integration."""
    results = install_hooks(force=args.force, copy=args.copy)
    for hook_name, target_dir, _event in _managed_hook_files():
        state = results.get(hook_name)
        if state in ("installed", "updated"):
            print(f"{state.capitalize()} {hook_name} -> {target_dir / hook_name}")
        elif state == "current":
            print(f"{hook_name} already current.")

    installed_commands = install_commands(force=args.force)
    if installed_commands:
        print(f"\nInstalled slash commands to {CLAUDE_COMMANDS_DIR}:")
        for name in installed_commands:
            print(f"  /{name}")
    else:
        print("Slash commands already installed.")

    refreshed_skills = False
    for name in _managed_global_skills():
        state = results.get(name)
        if state in ("installed", "updated"):
            print(f"{state.capitalize()} skill -> /{name} ({CLAUDE_SKILLS_DIR / name})")
            refreshed_skills = True
    if not refreshed_skills:
        print("Skills already current.")

    return 0


def unregister_hook_from_settings(event: str, hook_name: str) -> bool:
    """Remove a hook registered under `event` from Claude's settings.json.

    Returns True if settings were updated, False if not found.
    """
    settings_file = Path.home() / ".claude" / "settings.json"
    hook_command = f"~/.claude/hooks/{hook_name}"

    if not settings_file.exists():
        return False

    try:
        settings = json.loads(settings_file.read_text())
    except json.JSONDecodeError:
        return False

    if "hooks" not in settings or event not in settings["hooks"]:
        return False

    # Filter out entries containing our hook
    original_len = len(settings["hooks"][event])
    new_entries = []
    for entry in settings["hooks"][event]:
        if "hooks" in entry:
            # Check if any hook in this entry matches ours
            has_our_hook = any(h.get("command") == hook_command for h in entry["hooks"])
            if not has_our_hook:
                new_entries.append(entry)
        else:
            new_entries.append(entry)

    settings["hooks"][event] = new_entries

    if len(settings["hooks"][event]) == original_len:
        return False  # Hook wasn't registered

    # Clean up empty structures
    if not settings["hooks"][event]:
        del settings["hooks"][event]
    if not settings["hooks"]:
        del settings["hooks"]

    # Write back
    settings_file.write_text(json.dumps(settings, indent=2))
    return True


def is_hook_registered(event: str, hook_name: str) -> bool:
    """Check if a hook is registered under `event` in Claude's settings.json."""
    settings_file = Path.home() / ".claude" / "settings.json"
    hook_command = f"~/.claude/hooks/{hook_name}"

    if not settings_file.exists():
        return False

    try:
        settings = json.loads(settings_file.read_text())
    except json.JSONDecodeError:
        return False

    if "hooks" not in settings or event not in settings["hooks"]:
        return False

    # Check nested hooks array for our command
    for entry in settings["hooks"][event]:
        if "hooks" in entry:
            for h in entry["hooks"]:
                if h.get("command") == hook_command:
                    return True
    return False


def cmd_hooks_uninstall(args) -> int:
    """Remove all agentwire-owned hook files and their settings.json registration."""
    removed_any = False
    for hook_name, target_dir, event in _managed_hook_files():
        target = target_dir / hook_name
        if target.exists() or target.is_symlink():
            target.unlink()
            print(f"Removed: {target}")
            removed_any = True
        if event and unregister_hook_from_settings(event, hook_name):
            print(f"Unregistered {hook_name} from Claude settings.json")

    if not removed_any:
        print("No hooks installed")

    return 0


def cmd_hooks_status(args) -> int:
    """Check agentwire-owned hook files and tmux portal sync hooks."""
    print("=== AgentWire Hooks ===")
    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        hooks_source = None

    for hook_name, target_dir, event in _managed_hook_files():
        target = target_dir / hook_name
        print(f"{hook_name}:")

        if not (target.exists() or target.is_symlink()):
            print("  Status: not installed — run 'agentwire hooks install'")
            continue

        kind = "symlink" if target.is_symlink() else "copy"
        if hooks_source and (hooks_source / hook_name).exists():
            state = _managed_file_state(target, hooks_source / hook_name)
            drift = "" if state == "ok" else " — STALE, run 'agentwire hooks install'"
        else:
            drift = " — packaged source not found, drift unknown"
        print(f"  Status: installed ({kind}){drift}")
        location = f"{target} -> {target.resolve()}" if target.is_symlink() else str(target)
        print(f"  Location: {location}")

        if event:
            if is_hook_registered(event, hook_name):
                print(f"  Registered: yes ({event} in ~/.claude/settings.json)")
            else:
                print("  Registered: NO - run 'agentwire hooks install' to fix")

    # Tmux portal sync hooks
    print("\n=== Tmux Portal Sync Hooks ===")
    try:
        # Check global hooks first
        global_result = subprocess.run(
            ["tmux", "show-hooks", "-g"],
            capture_output=True,
            text=True,
        )
        global_hooks = global_result.stdout.strip()

        print("Global hooks:")
        has_global_created = "session-created" in global_hooks
        has_global_closed = "session-closed" in global_hooks

        if has_global_created or has_global_closed:
            parts = []
            if has_global_created:
                parts.append("session-created")
            if has_global_closed:
                parts.append("session-closed")
            print(f"  {', '.join(parts)}")
        else:
            print("  none (run 'agentwire portal restart' to install)")

        # Get list of sessions for per-session hooks
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("\nNo tmux sessions running")
            return 0

        sessions = result.stdout.strip().split("\n") if result.stdout.strip() else []

        if sessions:
            print("\nPer-session hooks:")
            for session in sessions:
                hooks_result = subprocess.run(
                    ["tmux", "show-hooks", "-t", session],
                    capture_output=True,
                    text=True,
                )
                hooks_output = hooks_result.stdout.strip()

                has_session_closed = "session-closed" in hooks_output
                has_kill_pane = "after-kill-pane" in hooks_output

                status_parts = []
                if has_session_closed:
                    status_parts.append("session-closed")
                if has_kill_pane:
                    status_parts.append("after-kill-pane")

                if status_parts:
                    print(f"  {session}: {', '.join(status_parts)}")
                else:
                    print(f"  {session}: none")

    except Exception as e:
        print(f"Error checking tmux hooks: {e}")

    return 0


def register_hooks_parser(subparsers) -> None:
    hooks_parser = subparsers.add_parser(
        "hooks", help="Manage agentwire hook files (permission, idle handler, queue processor)"
    )
    hooks_subparsers = hooks_parser.add_subparsers(dest="hooks_command")

    # hooks install
    hooks_install = hooks_subparsers.add_parser(
        "install", help="Install/refresh agentwire hook files and slash commands"
    )
    hooks_install.add_argument(
        "--force", "-f", action="store_true", help="Reinstall even when already current"
    )
    hooks_install.add_argument(
        "--copy", action="store_true", help="Copy files instead of symlinking"
    )
    hooks_install.set_defaults(func=cmd_hooks_install)

    # hooks uninstall
    hooks_uninstall = hooks_subparsers.add_parser(
        "uninstall", help="Remove agentwire hook files and their registration"
    )
    hooks_uninstall.set_defaults(func=cmd_hooks_uninstall)

    # hooks status
    hooks_status = hooks_subparsers.add_parser(
        "status", help="Check hook installation status"
    )
    hooks_status.set_defaults(func=cmd_hooks_status)
