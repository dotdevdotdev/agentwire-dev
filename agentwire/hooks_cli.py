"""CLI for hook/skill installation — ``agentwire hooks ...``.

Installs and heals agentwire-owned Claude Code integration: the permission
hook, idle handler, queue processor, slash commands, and global skills. These
files are agentwire-owned (no user edits to preserve), so any drift from the
packaged source is replaced.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from .claude_settings import SettingsError, edit_claude_settings, settings_path
from .utils.paths import claude_dir


# Resolved at CALL time, never frozen at import: an import-time constant keeps
# naming the real ~/.claude after a test redirects $HOME, which is how a unit
# test's `install_hooks(allow_foreign=True)` unlinked and relinked the owner's
# real hook symlinks into a worktree (#1118).
def claude_hooks_dir() -> Path:
    return claude_dir() / "hooks"


def claude_commands_dir() -> Path:
    return claude_dir() / "commands"


def claude_skills_dir() -> Path:
    return claude_dir() / "skills"


class LinkRefusedError(RuntimeError):
    """A machine-global symlink was refused; the message names source and state."""


def _is_real_dir(path: Path) -> bool:
    """A real directory (not a symlink). A concurrent install renaming over the
    slot can make the probe itself raise (EINVAL seen ~4% under 4 procs); that
    means "not a plain directory right now", never a failure."""
    try:
        return path.is_dir() and not path.is_symlink()
    except OSError:
        return False


def _sweep_stale_link_temps(target: Path) -> None:
    """Delete ``.<name>.<pid>.new`` temps whose creating process is gone (SIGKILLed
    mid-install). A live pid's temp belongs to a concurrent install: left alone."""
    for stale in target.parent.glob(f".{target.name}.*.new"):
        pid = stale.name[len(target.name) + 2:-len(".new")]
        if not pid.isdigit():
            continue
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            stale.unlink(missing_ok=True)
        except OSError:
            pass  # exists but not ours to signal: it is alive


def _link_into_claude(source: Path, target: Path, *, allow_foreign: bool, directory: bool = False) -> None:
    """The ONE place a machine-global symlink is created (#1118).

    Refuses loudly, naming the source and why, unless ``source`` is inside the
    canonical install (or ``allow_foreign`` says the caller means it). The
    check runs BEFORE the existing occupant is removed, so a refusal never
    leaves the slot empty.
    """
    from agentwire.safety import provenance as prov

    if not allow_foreign:
        refusal = prov.link_target_refusal(source)
        if refusal is not None:
            raise LinkRefusedError("\n".join(refusal))
    target.parent.mkdir(parents=True, exist_ok=True)
    # Create the new link under a temp name and rename it over the slot, so a
    # failure at any point leaves the old link (or nothing) — never a hole.
    _sweep_stale_link_temps(target)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(source.resolve(), target_is_directory=directory)
    try:
        if _is_real_dir(target):
            _remove_skill_target(target)  # a real directory can't be replaced by a rename
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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


def install_commands(force: bool = False, allow_foreign: bool = False) -> list[str]:
    """Symlink bundled slash commands into ~/.claude/commands/.

    Returns list of command names that were installed or updated.
    """
    try:
        commands_source = get_commands_source()
    except FileNotFoundError:
        return []

    claude_commands_dir().mkdir(parents=True, exist_ok=True)

    installed = []
    for src_file in commands_source.glob("*.md"):
        target = claude_commands_dir() / src_file.name
        if target.is_symlink() and target.resolve() == src_file.resolve() and not force:
            continue  # Already correctly symlinked
        _link_into_claude(src_file, target, allow_foreign=allow_foreign)
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


def install_skills(force: bool = False, copy: bool = False, allow_foreign: bool = False) -> dict[str, str]:
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

        target = claude_skills_dir() / name
        state = _managed_skill_state(target, source)
        if state == "ok" and not force:
            results[name] = "current"
            continue

        existed = target.exists() or target.is_symlink()
        if copy:
            claude_skills_dir().mkdir(parents=True, exist_ok=True)
            _remove_skill_target(target)
            shutil.copytree(source, target)
        else:
            _link_into_claude(source, target, allow_foreign=allow_foreign, directory=True)
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
        target = claude_skills_dir() / name
        if not source.exists():
            drift[name] = "source-unavailable"
            continue
        drift[name] = _managed_skill_state(target, source)
    return drift


def hook_matcher(hook_name: str) -> str:
    """The tool-name matcher a managed hook registers with.

    The outcome logger exists to label damage-control decisions, so it matches
    exactly the tools damage control matches (SSOT: ``DAMAGE_CONTROL_MATCHERS``);
    every other managed hook matches everything.
    """
    if hook_name == "outcome_logger.py":
        from agentwire.safety_commands import DAMAGE_CONTROL_MATCHERS

        return "|".join(DAMAGE_CONTROL_MATCHERS)
    return ".*"


def register_hook_in_settings(event: str, hook_name: str) -> bool:
    """Register a hook under `event` in Claude's settings.json.

    Returns True if settings were updated (added, or its matcher corrected to
    the current :func:`hook_matcher`), False if already configured. Raises
    ``SettingsError`` (file untouched) when settings.json is unparseable (#1220).

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
    # Use ~ for portability
    hook_command = f"~/.claude/hooks/{hook_name}"
    matcher = hook_matcher(hook_name)

    def mutate(settings: dict) -> bool:
        entries = settings.setdefault("hooks", {}).setdefault(event, [])

        # Check if already registered (check nested hooks array)
        for entry in entries:
            for h in entry.get("hooks", ()):
                if h.get("command") == hook_command:
                    if entry.get("matcher") == matcher or matcher == ".*":
                        # Already registered. A default-matcher hook keeps
                        # whatever matcher the owner hand-set.
                        return False
                    # Registered under an older matcher: correct it in place.
                    entry["matcher"] = matcher
                    return True

        entries.append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": hook_command}],
        })
        return True

    return edit_claude_settings(mutate)


class ManagedHook(NamedTuple):
    """An agentwire-owned file deployed by `hooks install`.

    ``events`` are the settings.json events it is registered under (none for
    non-hook files). ``label``/``required``/``why`` are what `doctor` prints, so
    the install list and the doctor list are one list.
    """

    name: str  # filename in agentwire/hooks/
    target_dir: Path
    events: tuple[str, ...]
    label: str
    required: bool
    why: str


def _managed_hook_files() -> list[ManagedHook]:
    return [
        ManagedHook(
            "agentwire-permission.sh", claude_hooks_dir(), ("PermissionRequest",),
            "Permission hook", False, "optional for prompted sessions",
        ),
        ManagedHook(
            "idle-handler.sh", claude_hooks_dir(), ("Notification",),
            "Idle notification hook", True,
            "required for worker notifications and scheduled tasks",
        ),
        # Records what happened AFTER damage-control decided: ran / failed /
        # denied. Joined to the audit rows by tool_use_id.
        ManagedHook(
            "outcome_logger.py", claude_hooks_dir(),
            ("PostToolUse", "PostToolUseFailure", "PermissionDenied"),
            "Outcome logger", True, "required for damage-control audit outcomes",
        ),
        ManagedHook(
            "queue-processor.sh", Path.home() / ".agentwire", (),
            "Queue processor", True, "required for notification queuing",
        ),
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


def _install_managed_file(
    source: Path, target: Path, force: bool = False, copy: bool = False, allow_foreign: bool = False,
) -> bool:
    """Install or refresh an agentwire-owned file (symlink by default).

    These files carry no user edits to preserve — any drift from the packaged
    source is replaced. Returns True if the target was created or updated.
    """
    if not force and _managed_file_state(target, source) == "ok":
        return False

    if copy:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        shutil.copy2(source, target)
        # Only the copied file gets a chmod. On the symlink path, chmod
        # FOLLOWS the link — and when the package is a source checkout, the
        # link points at a tracked file, so a chmod aimed at the install
        # lands in the repo and dirties every dev's tree (#947). Execution
        # through a symlink uses the target's mode, and the packaged hook
        # scripts ship 755, so the link needs no chmod of its own.
        target.chmod(0o755)
    else:
        _link_into_claude(source, target, allow_foreign=allow_foreign)
    return True


def install_hooks(
    force: bool = False,
    copy: bool = False,
    allow_foreign: bool = False,
) -> dict[str, str]:
    """Install/refresh all agentwire-owned hook files + settings.json registration.

    Returns {filename: "installed" | "updated" | "current" | "missing-source" |
    "refused-foreign"}.

    Refuses outright when run from a checkout that is not the canonically
    installed tool (#936). These targets are machine-global and installed as
    SYMLINKS by default, so `hooks install` from a worktree does not merely copy
    a stale file — it points every session's permission and idle hooks at a task
    branch, which then changes under them on every commit.
    """
    from agentwire.safety import provenance as prov

    state, canonical, running = prov.install_provenance()
    if state in prov.REFUSING_STATES and not allow_foreign:
        for line in prov.refusal_lines(canonical, running, "agentwire-owned hooks"):
            print(f"  {line}" if line else "")
        return {h.name: "refused-foreign" for h in _managed_hook_files()}

    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        print("  Warning: hooks directory not found, skipping hook installation")
        return {}

    results: dict[str, str] = {}
    for hook_name, target_dir, event, *_ in _managed_hook_files():
        source = hooks_source / hook_name
        if not source.exists():
            print(f"  Warning: {hook_name} not found in package, skipping")
            results[hook_name] = "missing-source"
            continue

        target = target_dir / hook_name
        existed = target.exists() or target.is_symlink()
        if _install_managed_file(source, target, force=force, copy=copy, allow_foreign=allow_foreign):
            results[hook_name] = "updated" if existed else "installed"
        else:
            results[hook_name] = "current"

        for ev in event:
            register_hook_in_settings(ev, hook_name)

    # Heal the full damage-control surface (hook scripts + rules + tooldefs +
    # PreToolUse matchers), not just the settings.json matchers. This closes the
    # documented post-rebuild gap: CLAUDE.md tells users to re-run `hooks install`
    # after a rebuild, so it must actually sync the DC files/rules — drift-aware,
    # bringing previously-shipped versions forward and never clobbering a
    # file it does not recognize.
    #
    # NOT quiet, and NOT silently swallowed: this heal can now legitimately
    # decline to act (a newer installed hook, a hand-edited rule), and a decline
    # an operator never sees is the #916/#936 failure mode itself — `hooks
    # install` printing success while the thing it installs stays stale.
    try:
        from agentwire.safety_commands import heal_damage_control
        dc = heal_damage_control(quiet=True, allow_foreign=allow_foreign)
        held = (
            dc.get("hooks_downgrade_refused", [])
            + dc.get("rules_unknown", [])
            + dc.get("tooldefs_unknown", [])
        )
        if held:
            print(f"  Damage control: {len(held)} file(s) HELD BACK: {', '.join(held)}")
            print("     Details/override: agentwire safety install --yes [--force]")
        for key, verb in (("rules_updated", "rule"), ("tooldefs_updated", "tooldef")):
            for name in dc.get(key, []):
                print(f"  Damage control: updated {verb} {name}")
        if dc.get("tooldefs_updated") or dc.get("tooldefs_installed"):
            from agentwire.safety_commands import unattended_grant_notice
            for line in unattended_grant_notice():
                print(line)
        if dc.get("settings_error"):
            raise SettingsError(dc["settings_error"])
    except SettingsError:
        raise
    except Exception as e:
        print(f"  Warning: damage-control heal failed: {e}")

    # Global skills (currently just /wiki) are agentwire-owned too, and rotted
    # silently because nothing ever resynced the hand-placed copies. Heal them
    # on the same install pass — drift-aware, like the hooks above.
    results.update(install_skills(force=force, copy=copy, allow_foreign=allow_foreign))

    return results


def cmd_hooks_install(args) -> int:
    """Install agentwire-owned hook files and slash commands for AgentWire integration."""
    allow_foreign = getattr(args, "allow_foreign_source", False)
    try:
        results = install_hooks(force=args.force, copy=args.copy, allow_foreign=allow_foreign)
    except (LinkRefusedError, SettingsError) as e:
        print(str(e))
        return 1
    if any(state == "refused-foreign" for state in results.values()):
        # A refusal must never be reportable as a successful install (#936).
        return 1
    for hook_name, target_dir, *_ in _managed_hook_files():
        state = results.get(hook_name)
        if state in ("installed", "updated"):
            print(f"{state.capitalize()} {hook_name} -> {target_dir / hook_name}")
        elif state == "current":
            print(f"{hook_name} already current.")

    try:
        installed_commands = install_commands(force=args.force, allow_foreign=allow_foreign)
    except LinkRefusedError as e:
        print(str(e))
        return 1
    if installed_commands:
        print(f"\nInstalled slash commands to {claude_commands_dir()}:")
        for name in installed_commands:
            print(f"  /{name}")
    else:
        print("Slash commands already installed.")

    refreshed_skills = False
    for name in _managed_global_skills():
        state = results.get(name)
        if state in ("installed", "updated"):
            print(f"{state.capitalize()} skill -> /{name} ({claude_skills_dir() / name})")
            refreshed_skills = True
    if not refreshed_skills:
        print("Skills already current.")

    return 0


def unregister_hook_from_settings(event: str, hook_name: str) -> bool:
    """Remove a hook registered under `event` from Claude's settings.json.

    Returns True if settings were updated, False if not found. Raises
    ``SettingsError`` (file untouched) when settings.json is unparseable.
    """
    hook_command = f"~/.claude/hooks/{hook_name}"

    def mutate(settings: dict) -> bool:
        entries = settings.get("hooks", {}).get(event)
        if not entries:
            return False
        kept = [
            entry for entry in entries
            if not any(h.get("command") == hook_command for h in entry.get("hooks", ()))
        ]
        if len(kept) == len(entries):
            return False  # Hook wasn't registered
        if kept:
            settings["hooks"][event] = kept
        else:
            del settings["hooks"][event]
            if not settings["hooks"]:
                del settings["hooks"]
        return True

    if not settings_path().exists():
        return False
    return edit_claude_settings(mutate)


def is_hook_registered(event: str, hook_name: str) -> bool:
    """Check if a hook is registered under `event` in Claude's settings.json."""
    settings_file = claude_dir() / "settings.json"
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
    for hook_name, target_dir, event, *_ in _managed_hook_files():
        target = target_dir / hook_name
        if target.exists() or target.is_symlink():
            target.unlink()
            print(f"Removed: {target}")
            removed_any = True
        for ev in event:
            try:
                unregistered = unregister_hook_from_settings(ev, hook_name)
            except SettingsError as e:
                print(str(e))
                return 1
            if unregistered:
                print(f"Unregistered {hook_name} ({ev}) from Claude settings.json")

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

    for hook_name, target_dir, event, *_ in _managed_hook_files():
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

        for ev in event:
            if is_hook_registered(ev, hook_name):
                print(f"  Registered: yes ({ev} in ~/.claude/settings.json)")
            else:
                print(f"  Registered ({ev}): NO - run 'agentwire hooks install' to fix")

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
    hooks_install.add_argument(
        "--allow-foreign-source", action="store_true",
        help="Let a checkout that is NOT the installed tool write these "
             "machine-global files. Installing from a task branch is what "
             "silently downgraded this machine's security hooks (#936)",
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
