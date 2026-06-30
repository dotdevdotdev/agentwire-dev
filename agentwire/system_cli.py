"""CLI for system-lifecycle commands.

The boot/build/voice surface that runs the local install rather than any one
session: ``up`` (boot all services then the dev session), ``dev``, ``init``,
``generate-certs``, ``listen`` (voice input), ``scratchpad``, ``services``,
``rebuild``, ``uninstall``. Pure relocation from ``__main__`` (#495).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from .core import (
    _build_tmux_env_flags,
    _get_portal_url,
    _git_behind_origin,
    _output_json,
    _output_result,
    _portal_auth_headers,
    _start_portal_local,
    build_agent_command,
    check_pip_environment,
    check_python_version,
    generate_certs,
    get_source_dir,
    load_config,
    tmux_session_exists,
    wait_for_shell_prompt,
)
from .project_config import detect_default_agent_type
from .roles import inject_soul, load_roles

UV_CACHE_DIR = Path.home() / ".cache" / "uv"


# === Dev Command ===

def cmd_dev(args) -> int:
    """Start or attach to the AgentWire dev/agentwire session."""
    session_name = "agentwire"
    project_dir = get_source_dir()

    if tmux_session_exists(session_name):
        print(f"Dev session exists. Attaching to '{session_name}'...")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    if not project_dir.exists():
        print(f"Project directory not found: {project_dir}", file=sys.stderr)
        return 1

    # Dev session uses the contributor role by default, plus the soul personality.
    # `contributor` is the universal helper persona (#620): repo-aware onboarding,
    # easy issue-filing, and the fork-based PR flow for non-owners. The owner layers
    # a local, untracked owner-override role on top (see the contributor role doc).
    role_names = inject_soul(["contributor"], load_config(), no_soul=getattr(args, 'no_soul', False))
    roles, missing = load_roles(role_names, project_dir)
    if missing:
        print(f"Warning: Roles not found: {', '.join(missing)}", file=sys.stderr)
        roles = None

    # Use bypass session type for dev session (full permissions)
    agent_type = detect_default_agent_type()
    session_type_str = f"{agent_type}-bypass"

    # Build agent command
    agent = build_agent_command(session_type_str, roles)

    agent_cmd = agent.command

    # Create session with env injected at creation time so the initial
    # shell sees the vars (see _build_tmux_env_flags docstring).
    print(f"Creating dev session '{session_name}' in {project_dir}...")
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name, "-c", str(project_dir),
        *_build_tmux_env_flags(agent.env),
    ])

    # Start agent with agentwire config
    if agent_cmd:
        wait_for_shell_prompt(session_name)
        subprocess.run([
            "tmux", "send-keys", "-t", session_name, agent_cmd, "Enter",
        ])

    print("Attaching... (Ctrl+B D to detach)")
    subprocess.run(["tmux", "attach-session", "-t", session_name])
    return 0


# === Scratchpad Commands ===

def _ping_scratchpad_changed() -> None:
    """Best-effort: tell a running portal the pad changed so clients refresh."""
    portal_url = _get_portal_url()
    if not portal_url:
        return
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        req = urllib.request.Request(
            f"{portal_url}/api/scratchpad/changed", data=b"{}",
            headers={"Content-Type": "application/json", **_portal_auth_headers()},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3, context=ctx)
    except Exception:
        pass  # portal down — file is still the source of truth


def cmd_scratchpad_list(args) -> int:
    """List scratch pad notes (newest first)."""
    from . import scratchpad
    json_mode = getattr(args, "json", False)
    notes = scratchpad.load_notes()
    if json_mode:
        _output_json({"success": True, "notes": notes})
        return 0
    if not notes:
        print("Scratch pad is empty.")
        return 0
    for n in notes:
        src = f" [{n['source']}]" if n.get("source") else ""
        first_line = n["text"].splitlines()[0][:80]
        more = " …" if ("\n" in n["text"] or len(n["text"]) > 80) else ""
        print(f"  {n['id']}{src}  {first_line}{more}")
    return 0


def cmd_scratchpad_add(args) -> int:
    """Add a note to the scratch pad."""
    from . import scratchpad
    json_mode = getattr(args, "json", False)
    try:
        note = scratchpad.add_note(args.text, source=getattr(args, "source", None))
    except ValueError as e:
        return _output_result(False, json_mode, str(e))
    _ping_scratchpad_changed()
    return _output_result(True, json_mode, f"Added note {note['id']}", note=note)


def cmd_scratchpad_remove(args) -> int:
    """Remove a note by id."""
    from . import scratchpad
    json_mode = getattr(args, "json", False)
    if not scratchpad.remove_note(args.id):
        return _output_result(False, json_mode, f"No note with id: {args.id}")
    _ping_scratchpad_changed()
    return _output_result(True, json_mode, f"Removed note {args.id}")


def cmd_scratchpad_clear(args) -> int:
    """Remove all notes."""
    from . import scratchpad
    json_mode = getattr(args, "json", False)
    count = scratchpad.clear_notes()
    _ping_scratchpad_changed()
    return _output_result(True, json_mode, f"Cleared {count} note(s)", count=count)


# === Services Commands ===

def _load_services_registry():
    """(config, registry) for the services commands."""
    from . import services as services_mod
    from .config import load_config as load_config_typed
    cfg = load_config_typed()
    return services_mod, services_mod.registry(cfg)


def _find_service(services_mod, reg, name: str):
    for svc in reg:
        if svc.name == name:
            return svc
    return None


def cmd_services_list(args) -> int:
    """List registered custom services (built-ins + config-defined)."""
    json_mode = getattr(args, "json", False)
    services_mod, reg = _load_services_registry()
    disabled = services_mod.load_disabled()

    entries = [{
        "name": svc.name,
        "project": svc.project,
        "autostart": svc.autostart,
        "restart": svc.restart,
        "healthcheck": {"kind": svc.healthcheck.kind, "interval": svc.healthcheck.interval},
        "roles": svc.roles,
        "type": svc.type,
        "disabled": svc.name in disabled,
    } for svc in reg]

    if json_mode:
        _output_json({"success": True, "services": entries})
        return 0

    if not entries:
        print("No custom services registered (services.custom in ~/.agentwire/config.yaml).")
        return 0
    for e in entries:
        flags = []
        if not e["autostart"]:
            flags.append("autostart off")
        if e["disabled"]:
            flags.append("disabled")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {e['name']}  restart={e['restart']}  "
              f"healthcheck={e['healthcheck']['kind']}/{e['healthcheck']['interval']}s{suffix}")
        if e["project"]:
            print(f"    project: {e['project']}")
    return 0


def cmd_services_status(args) -> int:
    """Health status for one or all custom services (runs healthchecks now).

    Exit 0 when everything that should be running is healthy, 1 otherwise.
    """
    json_mode = getattr(args, "json", False)
    name = getattr(args, "name", None)
    services_mod, reg = _load_services_registry()

    if name:
        svc = _find_service(services_mod, reg, name)
        if svc is None:
            return _output_result(False, json_mode, f"Unknown service: {name}")
        reg = [svc]

    statuses = [services_mod.service_status(svc) for svc in reg]
    # Disabled / autostart-off services aren't expected to be running
    all_ok = all(s["healthy"] or s["disabled"] or not s["autostart"] for s in statuses)

    if json_mode:
        # Always exit 0 in JSON mode — the payload carries all_healthy, and
        # callers (portal watchdog) need the data precisely when unhealthy.
        _output_json({"success": True, "all_healthy": all_ok, "services": statuses})
        return 0

    for s in statuses:
        if s["healthy"]:
            mark = "[ok]"
        elif s["disabled"] or not s["autostart"]:
            mark = "[..]"
        else:
            mark = "[!!]"
        extra = " (disabled)" if s["disabled"] else ("" if s["autostart"] else " (autostart off)")
        print(f"  {mark} {s['name']}: {s['detail']}{extra}")
    return 0 if all_ok else 1


def cmd_services_up(args) -> int:
    """Start a service (clears any 'down' state), or --all autostart services."""
    json_mode = getattr(args, "json", False)
    name = getattr(args, "name", None)
    services_mod, reg = _load_services_registry()

    if getattr(args, "all", False):
        from .config import load_config as load_config_typed
        results = services_mod.start_all_autostart(load_config_typed())
        ok = all(r.get("ok", True) for r in results)
        if json_mode:
            # Always exit 0 in JSON mode — per-service results carry the
            # failures; the portal autostart needs them either way.
            _output_json({"success": ok, "results": results})
            return 0
        for r in results:
            if "skipped" in r:
                print(f"  [..] {r['name']}: {r['skipped']}")
            else:
                print(f"  [{'ok' if r['ok'] else '!!'}] {r['name']}: {r['result']}")
        return 0 if ok else 1

    if not name:
        return _output_result(False, json_mode, "Service name required (or --all)")
    svc = _find_service(services_mod, reg, name)
    if svc is None:
        return _output_result(False, json_mode, f"Unknown service: {name}")

    services_mod.set_disabled(name, False)
    ok, msg = services_mod.start_service(svc)
    return _output_result(ok, json_mode, f"{name}: {msg}", name=name, result=msg)


def cmd_services_down(args) -> int:
    """Stop a service and keep it stopped (watchdog and up --all skip it)."""
    json_mode = getattr(args, "json", False)
    name = args.name
    services_mod, reg = _load_services_registry()
    if _find_service(services_mod, reg, name) is None:
        return _output_result(False, json_mode, f"Unknown service: {name}")

    # Disable BEFORE killing so the watchdog can't race a respawn
    services_mod.set_disabled(name, True)
    ok, msg = services_mod.stop_service(name)
    return _output_result(ok, json_mode, f"{name}: {msg} (disabled until 'services up {name}')",
                          name=name, result=msg, disabled=True)


def cmd_up(args) -> int:
    """Boot all AgentWire services, then start/attach the dev session.

    Brings up (detached): portal, TTS, STT, and any autostart custom
    services from config. The scheduler is auto-started by the portal
    (services.scheduler.autostart). Then runs `agentwire dev` to
    create + attach the main session.

    Per-service start is best-effort — a failure to start one service
    doesn't block the rest or the dev session.
    """
    from argparse import Namespace

    from .config import load_config as load_config_typed
    from .network import NetworkContext

    cfg_dict = load_config()
    cfg = load_config_typed()
    ctx = NetworkContext.from_config()

    def _is_local(name: str) -> bool:
        try:
            return ctx.is_local(name)
        except Exception:
            return True

    print("Bringing up AgentWire services...")

    # Portal (autostarts the scheduler on boot)
    if _is_local("portal"):
        portal_args = Namespace(
            dev=getattr(args, "dev", False),
            no_tts=getattr(args, "no_tts", False),
            no_stt=getattr(args, "no_stt", False),
            port=None, host=None,
            config=getattr(args, "config", None),
        )
        _start_portal_local(portal_args, attach=False)
    else:
        print("  Portal configured for a remote machine — skipping (start it there).")

    # TTS — only the custom tier has a local service to start
    tts_backend = cfg_dict.get("tts", {}).get("backend", "default")
    if getattr(args, "no_tts", False):
        print("  TTS skipped (--no-tts).")
    elif tts_backend != "custom":
        print("  TTS skipped (default tier — browser/OS voice, no service needed).")
    elif _is_local("tts"):
        from . import tts_cli
        tts_args = Namespace(port=None, host=None, backend=None)
        tts_cli._start_tts_local(tts_args, attach=False)
    else:
        print("  TTS configured for a remote machine — skipping (start it there).")

    # STT — only the custom tier has a local service to start
    if getattr(args, "no_stt", False):
        print("  STT skipped (--no-stt).")
    elif cfg_dict.get("stt", {}).get("backend", "default") == "custom":
        from . import tts_cli
        stt_args = Namespace(port=None, host=None, model=None, backend=None)
        tts_cli.cmd_stt_start(stt_args)
    else:
        print("  STT skipped (default tier — portal-owned Moonshine, "
              "auto-downloads on first boot; no service needed).")

    # Custom services (same shared path as portal-launch autostart)
    from . import services as services_mod
    print("Starting custom services...")
    for r in services_mod.start_all_autostart(cfg):
        if "skipped" in r:
            print(f"  [..] {r['name']}: {r['skipped']}")
        elif r.get("ok"):
            print(f"  [ok] {r['name']} ({r['result']})")
        else:
            print(f"  [!!] {r['name']}: {r['result']}", file=sys.stderr)

    print()
    # Finally, the dev session (creates + attaches the agentwire session)
    return cmd_dev(args)


# === Init Command ===

def cmd_init(args) -> int:
    """Initialize AgentWire configuration with interactive wizard.

    Default behavior: Run the wizard and end on the concrete portal-URL next
    steps, so a first-run evaluator lands on a working voice portal.
    Assisted mode (--assisted): also spawn the interactive Claude setup
    session at the end to configure TTS/STT and other services.
    """
    # Check Python version first
    if not check_python_version():
        return 1

    # Check for externally-managed environment (Ubuntu)
    if not check_pip_environment():
        print("Please set up a virtual environment before running init.")
        return 1

    from .onboarding import run_onboarding

    # Default ends on the portal-URL next steps; --assisted opts into the
    # interactive Claude setup session.
    return run_onboarding(skip_session=not args.assisted)


def cmd_generate_certs(args) -> int:
    """Generate SSL certificates."""
    return generate_certs()


# === Listen Commands ===

def cmd_listen_start(args) -> int:
    """Start voice recording."""
    from .listen import start_recording
    return start_recording()


def cmd_listen_stop(args) -> int:
    """Stop recording, transcribe, send to session or type at cursor."""
    from .listen import stop_recording
    session = args.session or "agentwire"
    type_at_cursor = getattr(args, 'type', False)
    transcribe_only = getattr(args, 'stdout', False)
    return stop_recording(session, voice_prompt=not args.no_prompt,
                          type_at_cursor=type_at_cursor, transcribe_only=transcribe_only)


def cmd_listen_cancel(args) -> int:
    """Cancel current recording."""
    from .listen import cancel_recording
    return cancel_recording()


def cmd_listen_toggle(args) -> int:
    """Toggle recording (start if not recording, stop if recording)."""
    from .listen import is_recording, start_recording, stop_recording
    session = args.session or "agentwire"
    if is_recording():
        return stop_recording(session, voice_prompt=not args.no_prompt)
    else:
        return start_recording()


# === Rebuild / Uninstall Commands ===

def cmd_rebuild(args) -> int:
    """Rebuild: clear uv cache, uninstall, reinstall from source.

    This is the correct way to pick up source changes when developing.
    `uv tool install . --force` does NOT work - it uses cached wheels.
    """
    force = getattr(args, "force", False)

    print("Rebuilding agentwire-dev...")
    print()

    # Resolve the source checkout up front so the git-drift guard and the
    # install step agree on which tree they're operating over.
    project_root = Path(__file__).parent.parent
    if not (project_root / "pyproject.toml").exists():
        project_root = get_source_dir()

    # Git-drift guard: rebuild is otherwise git-blind and will happily reinstall
    # stale code when local main was never pulled after a remote merge. Refuse
    # (unless --force) so the fix happens before the reinstall, not after.
    behind, err = _git_behind_origin(project_root)
    if err:
        print(f"  - Skipping git-drift check ({err})")
    elif behind and behind > 0:
        print(f"  [!!] Local checkout is {behind} commit(s) behind origin/main.")
        print(f"       {project_root}")
        print("       Rebuild would reinstall stale code. Run first:")
        print("         git pull --ff-only")
        if not force:
            print("       (or re-run with --force to rebuild anyway)")
            return 1
        print("       --force given: rebuilding from the behind checkout anyway.")
    else:
        print("  ✓ Checkout up to date with origin/main")
    print()

    # Step 1: Clear uv cache
    if UV_CACHE_DIR.exists():
        print(f"Clearing uv cache ({UV_CACHE_DIR})...")
        shutil.rmtree(UV_CACHE_DIR)
        print("  ✓ Cache cleared")
    else:
        print("  - No cache to clear")

    # Step 2: Uninstall
    print("Uninstalling agentwire-dev...")
    result = subprocess.run(
        ["uv", "tool", "uninstall", "agentwire-dev"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  ✓ Uninstalled")
    else:
        # Might not be installed, that's fine
        print("  - Not installed (continuing)")

    # Step 3: Reinstall from the source checkout resolved above.
    print(f"Installing from {project_root}...")
    result = subprocess.run(
        ["uv", "tool", "install", "."],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ✗ Install failed: {result.stderr}", file=sys.stderr)
        return 1

    print("  ✓ Installed")
    print()
    print("Rebuild complete. New version is active.")
    return 0


def cmd_uninstall(args) -> int:
    """Uninstall: clear uv cache and remove agentwire-dev tool."""
    print("Uninstalling agentwire-dev...")
    print()

    # Step 1: Clear uv cache
    if UV_CACHE_DIR.exists():
        print(f"Clearing uv cache ({UV_CACHE_DIR})...")
        shutil.rmtree(UV_CACHE_DIR)
        print("  ✓ Cache cleared")
    else:
        print("  - No cache to clear")

    # Step 2: Uninstall
    print("Uninstalling tool...")
    result = subprocess.run(
        ["uv", "tool", "uninstall", "agentwire-dev"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  ✓ Uninstalled")
    else:
        print(f"  - {result.stderr.strip() or 'Not installed'}")

    print()
    print("Uninstall complete.")
    print(f"To reinstall: cd {get_source_dir()} && uv tool install .")
    return 0


def register_system_parser(subparsers) -> None:
    # === init command ===
    init_parser = subparsers.add_parser("init", help="Interactive setup wizard")
    init_parser.add_argument(
        "--assisted", action="store_true",
        help="Spawn the interactive Claude setup session at the end "
             "(default: end on the portal-URL next steps)"
    )
    init_parser.set_defaults(func=cmd_init)

    # === dev command ===
    dev_parser = subparsers.add_parser(
        "dev", help="Start/attach to dev agentwire session"
    )
    dev_parser.add_argument("--no-soul", dest="no_soul", action="store_true", help="Skip soul personality role injection for this session")
    dev_parser.set_defaults(func=cmd_dev)

    # === up command ===
    up_parser = subparsers.add_parser(
        "up", help="Boot all services (portal, TTS, STT, scheduler, custom) then the dev session"
    )
    up_parser.add_argument("--dev", action="store_true", help="Run portal from source (uv run)")
    up_parser.add_argument("--no-tts", action="store_true", help="Skip starting the TTS server")
    up_parser.add_argument("--no-stt", action="store_true", help="Skip starting the STT server")
    up_parser.add_argument("--config", type=Path, default=None, help="Path to config file")
    up_parser.set_defaults(func=cmd_up)

    # === listen command group ===
    listen_parser = subparsers.add_parser("listen", help="Voice input recording")
    listen_parser.add_argument(
        "--session", "-s", type=str, default="agentwire",
        help="Target session (default: agentwire)"
    )
    listen_parser.add_argument(
        "--no-prompt", action="store_true",
        help="Don't prepend voice prompt hint"
    )
    listen_subparsers = listen_parser.add_subparsers(dest="listen_command")

    # listen start
    listen_start = listen_subparsers.add_parser("start", help="Start recording")
    listen_start.set_defaults(func=cmd_listen_start)

    # listen stop
    listen_stop = listen_subparsers.add_parser("stop", help="Stop and send")
    listen_stop.add_argument("--session", "-s", type=str, help="Target session")
    listen_stop.add_argument("--no-prompt", action="store_true")
    listen_stop.add_argument("--type", action="store_true", help="Type at cursor instead of sending to session")
    listen_stop.add_argument("--stdout", action="store_true", help="Print the raw transcript to stdout (no paste, no tmux send)")
    listen_stop.set_defaults(func=cmd_listen_stop)

    # listen cancel
    listen_cancel = listen_subparsers.add_parser("cancel", help="Cancel recording")
    listen_cancel.set_defaults(func=cmd_listen_cancel)

    # Default listen (no subcommand) = toggle
    listen_parser.set_defaults(func=cmd_listen_toggle)

    # === generate-certs (top-level shortcut) ===
    certs_parser = subparsers.add_parser(
        "generate-certs", help="Generate SSL certificates"
    )
    certs_parser.set_defaults(func=cmd_generate_certs)

    # === rebuild command ===
    rebuild_parser = subparsers.add_parser(
        "rebuild", help="Clear uv cache and reinstall from source (for development)"
    )
    rebuild_parser.add_argument(
        "--force", action="store_true",
        help="Rebuild even when the local checkout is behind origin/main",
    )
    rebuild_parser.set_defaults(func=cmd_rebuild)

    # === uninstall command ===
    uninstall_parser = subparsers.add_parser(
        "uninstall", help="Clear uv cache and uninstall the tool"
    )
    uninstall_parser.set_defaults(func=cmd_uninstall)

    # === scratchpad command group ===
    scratchpad_parser = subparsers.add_parser(
        "scratchpad",
        help="Shared scratch pad notes (portal drawer; agents add via MCP)",
        description=(
            "Persistent notes in ~/.agentwire/scratchpad.json, shared across all "
            "portal clients (the slide-in drawer, Alt+N) and agents. Mutations "
            "ping a running portal so open drawers refresh live."
        ),
    )
    scratchpad_subparsers = scratchpad_parser.add_subparsers(dest="scratchpad_command")

    scratchpad_list_parser = scratchpad_subparsers.add_parser("list", help="List notes")
    scratchpad_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    scratchpad_list_parser.set_defaults(func=cmd_scratchpad_list)

    scratchpad_add_parser = scratchpad_subparsers.add_parser("add", help="Add a note")
    scratchpad_add_parser.add_argument("text", help="Note text")
    scratchpad_add_parser.add_argument("--source", help="Provenance label (e.g. session name)")
    scratchpad_add_parser.add_argument("--json", action="store_true", help="Output JSON")
    scratchpad_add_parser.set_defaults(func=cmd_scratchpad_add)

    scratchpad_remove_parser = scratchpad_subparsers.add_parser("remove", help="Remove a note")
    scratchpad_remove_parser.add_argument("id", help="Note id (see list)")
    scratchpad_remove_parser.add_argument("--json", action="store_true", help="Output JSON")
    scratchpad_remove_parser.set_defaults(func=cmd_scratchpad_remove)

    scratchpad_clear_parser = scratchpad_subparsers.add_parser("clear", help="Remove all notes")
    scratchpad_clear_parser.add_argument("--json", action="store_true", help="Output JSON")
    scratchpad_clear_parser.set_defaults(func=cmd_scratchpad_clear)

    # === services command group ===
    services_parser = subparsers.add_parser(
        "services",
        help="Manage user-defined services (long-running registered sessions)",
        description=(
            "Custom services are long-running agentwire sessions registered in "
            "services.custom in ~/.agentwire/config.yaml. They autostart on portal "
            "launch and `agentwire up`, and the portal watchdog health-checks them "
            "(restart: never | on-failure | always, with backoff). "
            "The notifications bridge session is a built-in registry entry."
        ),
    )
    services_subparsers = services_parser.add_subparsers(dest="services_command")

    services_list_parser = services_subparsers.add_parser("list", help="List registered services")
    services_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    services_list_parser.set_defaults(func=cmd_services_list)

    services_status_parser = services_subparsers.add_parser(
        "status", help="Run healthchecks and report per-service status"
    )
    services_status_parser.add_argument("name", nargs="?", help="Service name (default: all)")
    services_status_parser.add_argument("--json", action="store_true", help="Output JSON")
    services_status_parser.set_defaults(func=cmd_services_status)

    services_up_parser = services_subparsers.add_parser(
        "up", help="Start a service (clears 'down' state)"
    )
    services_up_parser.add_argument("name", nargs="?", help="Service name")
    services_up_parser.add_argument("--all", action="store_true",
                                    help="Start all autostart services (skips downed ones)")
    services_up_parser.add_argument("--json", action="store_true", help="Output JSON")
    services_up_parser.set_defaults(func=cmd_services_up)

    services_down_parser = services_subparsers.add_parser(
        "down", help="Stop a service and keep it stopped"
    )
    services_down_parser.add_argument("name", help="Service name")
    services_down_parser.add_argument("--json", action="store_true", help="Output JSON")
    services_down_parser.set_defaults(func=cmd_services_down)
