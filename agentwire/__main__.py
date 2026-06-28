"""CLI entry point for AgentWire."""

import argparse
import datetime
import importlib.resources
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# Load .env files (project first, then global config)
load_dotenv()  # .env in current directory
load_dotenv(Path.home() / ".agentwire" / ".env")  # Global config

from . import (  # noqa: E402  # must follow load_dotenv() above
    __version__,
    pane_manager,
)
from .core import (  # noqa: E402,F401  # E402: must follow load_dotenv(); F401: re-exported moved helpers
    _UNATTENDED_ENV_KEYS,
    CONFIG_DIR,
    KIND_DEFAULT_POSTURE,
    AgentCommand,
    _add_posture_harness_flags,
    _build_tmux_env_flags,
    _build_tmux_env_flags_shell,
    _check_portal_health,
    _check_tmux_installed,
    _default_portal_url,
    _display_parent,
    _get_agentwire_path,
    _get_all_machines,
    _get_machine_config,
    _get_portal_url,
    _get_session_project_path,
    _git_behind_origin,
    _install_global_tmux_hooks,
    _notify_portal_sessions_changed,
    _output_json,
    _output_result,
    _parse_session_target,
    _portal_auth_headers,
    _post_desktop_notification,
    _record_session_creator,
    _resolve_session_type_from_args,
    _run_remote,
    _set_session_name_env,
    _start_portal_local,
    _tmux_global_option,
    _with_unattended_env,
    build_agent_command,
    check_pip_environment,
    check_python_version,
    format_relative_time,
    generate_certs,
    get_kokoro_session_name,
    get_portal_session_name,
    get_source_dir,
    get_stt_session_name,
    get_tts_session_name,
    inject_session_env,
    load_config,
    load_session_metadata,
    parse_env_args,
    store_session_metadata,
    tmux_session_exists,
    tmux_session_has_agent,
    wait_for_shell_prompt,
)
from .project_config import (  # noqa: E402  # must follow load_dotenv() above
    ProjectConfig,
    SessionType,
    detect_default_agent_type,
    get_parent_from_config,
)
from .roles import (  # noqa: E402  # must follow load_dotenv() above
    inject_soul,
    load_roles,
)


def cmd_notify_parent(args) -> int:
    """Notify parent session (worker→orchestrator communication).

    Sends a prefixed text message to the parent session via tmux.
    The parent is determined from .agentwire.yml or --to flag.

    This is for session hierarchy communication. For outbound notifications
    to the user across devices, use `agentwire email` or `agentwire quo`.

    Notification targets (in priority order):
    1. --to SESSION if specified
    2. parent from .agentwire.yml if exists
    3. pane 0 of current session (if in worker pane)

    Examples:
        agentwire notify "Worker 1 completed task"
        agentwire notify --to agentwire "Build finished"
    """
    text = " ".join(args.text) if args.text else ""
    json_mode = getattr(args, 'json', False)

    if not text:
        return _output_result(False, json_mode, "Usage: agentwire notify-parent <message>")

    target_session = getattr(args, 'to', None)
    current_session = pane_manager.get_current_session()
    current_pane = pane_manager.get_current_pane_index()

    # If no explicit target, try parent from config
    if not target_session:
        parent = get_parent_from_config()
        if parent:
            target_session = parent

    # Build notification message (--raw sends verbatim — queued messages
    # already carry their own [WORKER SUMMARY ...] / [PROMPT ...] headers)
    if getattr(args, 'raw', False):
        notification = text
    else:
        source = current_session or "unknown"
        if current_pane is not None and current_pane > 0:
            notification = f"[NOTIFY from {source} pane {current_pane}] {text}"
        else:
            notification = f"[NOTIFY from {source}] {text}"

    if target_session:
        if target_session == current_session and current_pane == 0:
            return _output_result(False, json_mode, "Cannot notify own pane")
    elif current_pane is not None and current_pane > 0 and current_session:
        target_session = current_session
    else:
        return _output_result(
            False, json_mode,
            "No target session (set 'parent' in .agentwire.yml or use --to)")

    # safe_deliver refuses targets where a paste could do damage (live
    # dialog on screen, bare shell, parked session) and verifies the paste
    # actually landed. Callers (queue processor) retry on failure.
    from agentwire import prompt_router

    delivered, reason = prompt_router.safe_deliver(target_session, 0, notification)
    if json_mode:
        _output_json({
            "success": delivered,
            "target": target_session,
            "delivered": delivered,
            "reason": reason if not delivered else None,
        })
        return 0 if delivered else 1
    if not delivered:
        print(f"Notification not delivered to {target_session}: {reason}", file=sys.stderr)
        return 1
    if not getattr(args, 'quiet', False):
        print(f"Notified {target_session}")
    return 0


def cmd_open(args) -> int:
    """Open a URL or local file as an artifact window in the portal.

    Examples:
        agentwire open dashboard.html --title "Dashboard"
        agentwire open https://example.com --title "External"
        agentwire open test.html --artifact-id my-test --json
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    url = args.url
    title = args.title
    artifact_id = getattr(args, 'artifact_id', None)
    json_output = getattr(args, 'json', False)

    portal_url = _get_portal_url()

    body = {
        "type": "artifact",
        "url": url,
        "title": title,
    }
    if artifact_id:
        body["artifact_id"] = artifact_id

    try:
        resp = requests.post(
            f"{portal_url}/api/desktop/window/open",
            json=body,
            headers=_portal_auth_headers(),
            verify=False,
            timeout=10,
        )
        data = resp.json()

        if json_output:
            print(json.dumps(data))
        elif data.get("success"):
            print(f"Opened artifact window: {title} (id: {data.get('window_id', 'unknown')})")
        else:
            print(f"Failed: {data.get('error', 'Unknown error')}", file=sys.stderr)
            return 1

    except requests.exceptions.ConnectionError:
        msg = "Portal not reachable. Is it running? (agentwire portal status)"
        if json_output:
            print(json.dumps({"success": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1
    except Exception as e:
        if json_output:
            print(json.dumps({"success": False, "error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_notify_user(args) -> int:
    """Show the human a desktop toast on the portal (notify-user)."""
    text = " ".join(args.text) if args.text else ""
    json_mode = getattr(args, "json", False)
    if not text.strip():
        return _output_result(False, json_mode, "Usage: agentwire notify-user <text>")
    ok = _post_desktop_notification(
        text, session=getattr(args, "session", None),
        priority=getattr(args, "priority", "normal"),
    )
    return _output_result(ok, json_mode,
                          "Toast posted." if ok else "Failed to post toast (portal not reachable?)")


def cmd_research(args) -> int:
    """Resolve (or ensure) the Briefing Mode research dropbox for a session."""
    from .research import ensure_research_dir, research_dir

    json_mode = getattr(args, "json", False)
    session = getattr(args, "session", None) or pane_manager.get_current_session()
    if not session:
        return _output_result(False, json_mode, "No session (use -s or run inside a session)")
    sub = getattr(args, "research_command", None)
    path = ensure_research_dir(session) if sub == "ensure" else research_dir(session)
    if json_mode:
        _output_json({"success": True, "session": session, "path": str(path), "exists": path.exists()})
        return 0
    print(str(path))
    return 0


def cmd_wiki(args) -> int:
    """Deterministic mechanical ops for the LLM wiki (status/query/lint/new/done)."""
    from . import wiki

    json_mode = getattr(args, "json", False)
    root = getattr(args, "root", None)
    sub = getattr(args, "wiki_command", None)

    if sub is None:
        return _output_result(False, json_mode, "Specify a subcommand: status, query, lint, new, done")

    if sub == "status":
        data = wiki.status(root)
        if json_mode:
            _output_json({"success": True, **data})
            return 0
        print(wiki._status_text(data))
        return 0

    if sub == "query":
        results = wiki.query(args.query, root, limit=args.limit)
        if json_mode:
            _output_json({"success": True, "results": results})
            return 0
        if not results:
            print("No matching pages.")
            return 0
        for r in results:
            print(f"  [{r['score']:>3}] {r['rel']}\n        {r['snippet']}")
        return 0

    if sub == "lint":
        findings = wiki.lint(root)
        if json_mode:
            _output_json({"success": True, "findings": findings, "count": len(findings)})
        else:
            print(wiki._format_lint_text(findings))
        return 1 if (findings and getattr(args, "strict", False)) else 0

    if sub == "new":
        try:
            dest = wiki.new_page(args.category, args.name, root, title=getattr(args, "title", None))
        except (ValueError, FileExistsError) as e:
            return _output_result(False, json_mode, str(e))
        return _output_result(True, json_mode, f"Created {dest}", path=str(dest))

    if sub == "done":
        try:
            dest = wiki.done(args.rawfile, root)
        except (ValueError, FileNotFoundError) as e:
            return _output_result(False, json_mode, str(e))
        return _output_result(True, json_mode, f"Archived → {dest}", path=str(dest))

    return _output_result(False, json_mode, f"Unknown wiki subcommand: {sub}")


def cmd_notify(args) -> int:
    """Send a notification to the portal about session/pane state changes.

    Called by tmux hooks to notify the portal when sessions are created/closed,
    panes are created/killed, clients attach/detach, sessions are renamed, etc.
    The portal broadcasts these events to connected dashboard clients for real-time
    UI updates.
    """
    event = args.event
    session = getattr(args, 'session', None)
    pane = getattr(args, 'pane', None)
    pane_id = getattr(args, 'pane_id', None)
    old_name = getattr(args, 'old_name', None)
    new_name = getattr(args, 'new_name', None)
    json_mode = getattr(args, 'json', False)

    if not event:
        return _output_result(False, json_mode, "Event is required")

    portal_url = _get_portal_url()
    if not portal_url:
        return _output_result(False, json_mode, "Portal URL not configured")

    # Build payload
    payload = {"event": event}
    if session:
        payload["session"] = session
    if pane is not None:
        payload["pane"] = pane
    if pane_id is not None:
        payload["pane_id"] = pane_id
    if old_name is not None:
        payload["old_name"] = old_name
    if new_name is not None:
        payload["new_name"] = new_name

    try:
        # Use urllib to avoid requests dependency in core CLI

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{portal_url}/api/notify",
            data=data,
            headers={"Content-Type": "application/json", **_portal_auth_headers()},
            method="POST"
        )

        # Disable SSL verification for self-signed certs
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
            result = json.loads(response.read().decode())

        if result.get("success"):
            if json_mode:
                _output_json({"success": True, "event": event, "session": session,
                              "clients": result.get("clients", 0)})
            return 0
        else:
            return _output_result(False, json_mode, result.get("error", "Unknown error"))

    except Exception as e:
        # Don't fail loudly - hooks run in background and shouldn't block tmux
        if json_mode:
            _output_json({"success": False, "error": str(e)})
        return 1


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

    # Dev session uses agentwire role by default, plus the soul personality
    role_names = inject_soul(["agentwire"], load_config(), no_soul=getattr(args, 'no_soul', False))
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


# === Rebuild/Uninstall Commands ===

UV_CACHE_DIR = Path.home() / ".cache" / "uv"


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


# === MCP Server Command ===


def cmd_mcp(args) -> int:
    """Run the MCP server on stdio.

    Exposes AgentWire capabilities as MCP tools for external agents
    like MoltBot, Claude Desktop, etc.
    """
    from .mcp_server import run_server
    run_server()
    return 0


# =============================================================================
# Handoff Commands (shareable conversation bundles — ai-handoff.md + show-the-story.html)
# =============================================================================


_HANDOFF_TEMPLATE = """\
<session_bundle version="1">
<title>{{ short title — what this session is about }}</title>

<metadata>
- cwd: __CWD__
- repo_url: __REPO_URL__
- branch: __BRANCH__
- commit: __COMMIT__
- session_type: claude-bypass
- model: __MODEL__
- started_at: {{ ISO timestamp }}
- ended_at: {{ ISO timestamp }}
- user_identity: {{ who }}
- mcp_servers: {{ comma-separated list }}
</metadata>

<environment>
{{ panes, channels, scheduler state, anything else the receiver can't see from cwd alone }}
</environment>

<instructions>
{{ The CLI prefilled this section with collected CLAUDE.md / rules / memory.
   Leave it alone unless you need to redact secrets. }}
__INSTRUCTIONS_BLOCK__
</instructions>

<project_state>
<git_status>
__GIT_STATUS__
</git_status>
<git_log>
__GIT_LOG__
</git_log>
<git_diff>
__GIT_DIFF__
</git_diff>
{{ optionally <file path="..."> blocks for key files }}
</project_state>

<conversation_summary>
<goal>{{ one sentence: what this session set out to do }}</goal>
<tldr>{{ one paragraph the receiver reads first }}</tldr>
<decisions>
<decision>
<title>{{ short name of decision }}</title>
<rationale>{{ why it was made }}</rationale>
<alternatives>
- {{ alternative 1 }}
- {{ alternative 2 }}
</alternatives>
</decision>
</decisions>
<dead_ends>
<dead_end>
<title>{{ thing tried }}</title>
<why>{{ why rejected — saves the receiver from retracing }}</why>
</dead_end>
</dead_ends>
<open_threads>
<thread>
<title>{{ unresolved item }}</title>
<note>{{ where to pick up }}</note>
</thread>
</open_threads>
<stats>
- turns: {{ N }}
- files_touched: {{ N }}
- tools_used: {{ N }}
- duration_minutes: {{ N }}
</stats>
</conversation_summary>

<journey>
<beat title="{{ short name }}">
<quote>{{ optional verbatim line from the conversation }}</quote>
<what_happened>{{ what changed at this beat }}</what_happened>
</beat>
</journey>

<recent_turns>
{{ Last ~10-20 turns, filtered: drop tool noise, keep user turns + decision-making
   assistant turns. Use markdown blockquotes or simple labelled blocks. }}
</recent_turns>

<handoff>
<one_sentence>{{ what the next agent should do first }}</one_sentence>
<resume_at>{{ specific file path / TODO / step number }}</resume_at>
<caveats>
- {{ permission boundary, e.g. "do not push" }}
- {{ env-specific note }}
</caveats>
</handoff>

<theme>
{
  "name": "{{ short slug, e.g. evening-debug }}",
  "mood": "{{ honest read of the session's emotional tone }}",
  "palette": {
    "bg": "#0e0f13",
    "surface": "#1a1d24",
    "fg": "#e2e8f0",
    "muted": "#64748b",
    "accent": "#5eead4",
    "accent_2": "#fbbf24",
    "border": "#2a2f3a"
  },
  "fonts": {
    "heading": "ui-monospace, 'JetBrains Mono', monospace",
    "body": "ui-sans-serif, system-ui, sans-serif"
  },
  "motion": "subtle"
}
</theme>
</session_bundle>
"""


def _handoff_artifacts_dir() -> Path:
    try:
        from .config import load_config
        cfg = load_config()
        return Path(str(cfg.artifacts.dir)).expanduser()
    except Exception:
        return Path.home() / ".agentwire" / "artifacts"


def _handoff_slug(title_hint: str | None = None) -> str:
    import re as _re
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if title_hint:
        clean = _re.sub(r"[^a-z0-9]+", "-", title_hint.lower()).strip("-")[:40]
        if clean:
            return f"handoff-{ts}-{clean}"
    return f"handoff-{ts}"


def _build_template(cwd: Path) -> str:
    """Pre-fill template with collected git + instructions chain."""
    from .handoff import git_state, instructions

    snap = git_state.snapshot(cwd)
    chain = instructions.collect(cwd)

    instructions_block_parts = []
    for instr in chain:
        instructions_block_parts.append(
            f'<file path="{instr.path}" kind="{instr.kind}">\n{instr.content}\n</file>'
        )
    instructions_block = "\n".join(instructions_block_parts) if instructions_block_parts else \
        "{{ no CLAUDE.md or memory files found — this bundle is portable but light on instructions }}"

    substitutions = {
        "__CWD__": str(cwd),
        "__REPO_URL__": snap.get("remote_url") or "{{ none }}",
        "__BRANCH__": snap.get("branch") or "{{ unknown }}",
        "__COMMIT__": snap.get("commit") or "{{ unknown }}",
        "__MODEL__": "claude-opus-4-7",
        "__INSTRUCTIONS_BLOCK__": instructions_block,
        "__GIT_STATUS__": snap.get("status") or "(clean)",
        "__GIT_LOG__": snap.get("log") or "(no commits)",
        "__GIT_DIFF__": snap.get("diff") or "(no uncommitted diff)",
    }
    text = _HANDOFF_TEMPLATE
    for key, value in substitutions.items():
        text = text.replace(key, value)
    return text


def cmd_handoff_init(args) -> int:
    """Create a handoff bundle directory and emit a pre-filled ai-handoff.md template.

    The agent then edits ai-handoff.md to add the session-specific summary,
    decisions, journey, and theme — the parts only the agent has context for.
    """
    json_mode = getattr(args, 'json', False)
    title_hint = getattr(args, 'title', None)
    output_dir = getattr(args, 'output_dir', None)

    base = Path(output_dir).expanduser() if output_dir else _handoff_artifacts_dir()
    bundle_dir = base / _handoff_slug(title_hint)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    cwd = Path.cwd()
    template_text = _build_template(cwd)
    handoff_path = bundle_dir / "ai-handoff.md"
    handoff_path.write_text(template_text, encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "cwd": str(cwd),
        "title_hint": title_hint or "",
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    if json_mode:
        _output_json({
            "success": True,
            "bundle_dir": str(bundle_dir),
            "ai_handoff_path": str(handoff_path),
            "manifest_path": str(bundle_dir / "manifest.json"),
            "instructions": (
                "Edit ai-handoff.md to fill in the {{ ... }} placeholders. "
                "Then run: agentwire handoff render <bundle_dir> --story"
            ),
        })
    else:
        print(f"Bundle dir: {bundle_dir}")
        print(f"Edit:       {handoff_path}")
        print()
        print("Next: fill in the placeholders, then run:")
        print(f"  agentwire handoff render {bundle_dir} --story")
    return 0


def cmd_handoff_render(args) -> int:
    """Render show-the-story.html from an existing ai-handoff.md."""
    from .handoff.parser import HandoffParseError, parse_file
    from .handoff.renderer import render_to_file

    json_mode = getattr(args, 'json', False)
    target = Path(args.path).expanduser()

    if target.is_dir():
        md_path = target / "ai-handoff.md"
        bundle_dir = target
    else:
        md_path = target
        bundle_dir = target.parent

    if not md_path.exists():
        msg = f"ai-handoff.md not found at {md_path}"
        if json_mode:
            _output_json({"success": False, "error": msg})
        else:
            print(msg, file=sys.stderr)
        return 1

    try:
        bundle = parse_file(md_path)
    except HandoffParseError as e:
        msg = f"Invalid ai-handoff.md: {e}"
        if json_mode:
            _output_json({"success": False, "error": msg, "path": str(md_path)})
        else:
            print(msg, file=sys.stderr)
        return 1

    outputs: dict[str, str] = {"ai_handoff_path": str(md_path)}

    if getattr(args, 'story', True):
        story_path = bundle_dir / "show-the-story.html"
        render_to_file(bundle, story_path)
        outputs["show_the_story_path"] = str(story_path)

    if json_mode:
        _output_json({"success": True, "bundle_dir": str(bundle_dir), **outputs})
    else:
        print(f"Bundle dir: {bundle_dir}")
        for k, v in outputs.items():
            print(f"  {k}: {v}")
    return 0


def cmd_handoff_list(args) -> int:
    """List past handoff bundles in the artifacts directory."""
    json_mode = getattr(args, 'json', False)
    base = _handoff_artifacts_dir()

    bundles = []
    if base.exists():
        for d in sorted(base.glob("handoff-*")):
            if not d.is_dir():
                continue
            handoff_md = d / "ai-handoff.md"
            story_html = d / "show-the-story.html"
            manifest_path = d / "manifest.json"
            manifest_data = {}
            if manifest_path.exists():
                try:
                    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            bundles.append({
                "name": d.name,
                "path": str(d),
                "ai_handoff_exists": handoff_md.exists(),
                "show_the_story_exists": story_html.exists(),
                "created_at": manifest_data.get("created_at", ""),
                "title_hint": manifest_data.get("title_hint", ""),
                "cwd": manifest_data.get("cwd", ""),
            })

    if json_mode:
        _output_json({"success": True, "bundles": bundles, "count": len(bundles)})
    else:
        if not bundles:
            print(f"No handoff bundles in {base}")
            return 0
        print(f"Handoff bundles in {base}:")
        for b in bundles:
            badge = []
            if b["ai_handoff_exists"]:
                badge.append("md")
            if b["show_the_story_exists"]:
                badge.append("html")
            print(f"  {b['name']:<60} [{','.join(badge) or '-'}] {b['title_hint']}")
    return 0


# === Hooks Commands ===

CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks"


# =============================================================================
# Roles Commands
# =============================================================================


def cmd_roles_list(args) -> int:
    """List available roles from all sources."""
    from .roles import parse_role_file

    json_mode = getattr(args, 'json', False)

    # Collect roles from all sources
    roles_data = []

    # User roles (~/.agentwire/roles/)
    user_roles_dir = Path.home() / ".agentwire" / "roles"
    if user_roles_dir.exists():
        for role_file in user_roles_dir.glob("*.md"):
            role = parse_role_file(role_file)
            if role:
                roles_data.append({
                    "name": role.name,
                    "description": role.description,
                    "source": "user",
                    "path": str(role_file),
                    "disallowed_tools": role.disallowed_tools,
                })

    # Bundled roles (agentwire/roles/)
    try:
        bundled_dir = Path(__file__).parent / "roles"
        if bundled_dir.exists():
            for role_file in bundled_dir.glob("*.md"):
                # Skip if user already has this role
                if any(r["name"] == role_file.stem for r in roles_data):
                    continue
                role = parse_role_file(role_file)
                if role:
                    roles_data.append({
                        "name": role.name,
                        "description": role.description,
                        "source": "bundled",
                        "path": str(role_file),
                        "disallowed_tools": role.disallowed_tools,
                        })
    except Exception:
        pass

    if json_mode:
        _output_json({"roles": roles_data})
        return 0

    if not roles_data:
        print("No roles found.")
        print("Create roles in: ~/.agentwire/roles/")
        return 0

    # Print table
    print("Available Roles:")
    print()
    print(f"{'Name':<20} {'Source':<10} {'Description':<40}")
    print("-" * 70)
    for r in sorted(roles_data, key=lambda x: x["name"]):
        desc = r["description"][:37] + "..." if len(r["description"]) > 40 else r["description"]
        print(f"{r['name']:<20} {r['source']:<10} {desc:<40}")

    print()
    print("User roles: ~/.agentwire/roles/")
    print("Use 'agentwire roles show <name>' for details")
    return 0


def cmd_projects_list(args) -> int:
    """List discovered projects."""
    from .projects import get_projects

    json_mode = getattr(args, 'json', False)
    machine_filter = getattr(args, 'machine', None)

    projects = get_projects(machine=machine_filter)

    if json_mode:
        _output_json({"projects": projects})
        return 0

    if not projects:
        print("No projects found.")
        print("Projects need a .agentwire.yml file in their directory.")
        return 0

    # Print table
    print(f"Discovered Projects ({len(projects)}):\n")
    print(f"{'Name':<25} {'Type':<15} {'Path':<40}")
    print("-" * 80)
    for p in projects:
        # Truncate long paths
        path = p["path"]
        if len(path) > 40:
            path = "..." + path[-37:]
        machine_suffix = f" @{p['machine']}" if p['machine'] != 'local' else ""
        print(f"{p['name']:<25} {p['type']:<15} {path:<40}{machine_suffix}")

    print()
    return 0


_VALID_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def cmd_projects_create(args) -> int:
    """Create a new local project: make the directory, optionally git-init or clone, and write .agentwire.yml."""
    from .config import get_config
    from .project_config import ensure_gitignored, save_project_config

    name = (args.name or "").strip()
    clone_url = (getattr(args, "from_url", None) or "").strip() or None
    do_git_init = bool(getattr(args, "git_init", False))
    json_mode = getattr(args, "json", False)

    def _fail(msg: str) -> int:
        if json_mode:
            _output_json({"success": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    if not name:
        return _fail("Project name is required")
    if not _VALID_PROJECT_NAME.match(name) or "/" in name or ".." in name:
        return _fail("Invalid project name (allowed: letters, digits, '.', '_', '-')")

    projects_dir = get_config().projects.dir.expanduser().resolve()
    projects_dir.mkdir(parents=True, exist_ok=True)
    project_path = projects_dir / name

    if project_path.exists():
        return _fail(f"Project already exists at {project_path}")

    try:
        if clone_url:
            result = subprocess.run(
                ["git", "clone", clone_url, str(project_path)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                return _fail(f"git clone failed: {result.stderr.strip() or 'unknown error'}")
        else:
            project_path.mkdir(parents=True, exist_ok=False)
            if do_git_init:
                result = subprocess.run(
                    ["git", "init"],
                    cwd=str(project_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    return _fail(f"git init failed: {result.stderr.strip() or 'unknown error'}")
    except subprocess.TimeoutExpired:
        return _fail("Operation timed out")
    except OSError as e:
        return _fail(f"Failed to create directory: {e}")

    gitignore_updated = ensure_gitignored(project_path)
    config = ProjectConfig(type=SessionType.from_str("claude-bypass"), roles=[], voice=None)
    if not save_project_config(config, project_path):
        return _fail("Created project directory but failed to write .agentwire.yml")

    payload = {
        "success": True,
        "name": name,
        "path": str(project_path),
        "machine": "local",
        "cloned": bool(clone_url),
        "git_init": do_git_init and not clone_url,
    }
    if json_mode:
        _output_json(payload)
    else:
        print(f"Created project '{name}' at {project_path}")
        if clone_url:
            print(f"  Cloned from: {clone_url}")
        elif do_git_init:
            print("  Initialized empty git repository")
        if gitignore_updated:
            print("  Added .agentwire.yml to .gitignore (personal config — keep it untracked)")
    return 0


def cmd_roles_show(args) -> int:
    """Show details for a specific role."""
    from .roles import discover_role, parse_role_file

    name = args.name
    json_mode = getattr(args, 'json', False)

    # Discover role
    role_path = discover_role(name)
    if not role_path:
        if json_mode:
            _output_json({"error": f"Role '{name}' not found"})
        else:
            print(f"Role '{name}' not found.", file=sys.stderr)
            print("Available locations:")
            print(f"  User: ~/.agentwire/roles/{name}.md")
            print(f"  Bundled: agentwire/roles/{name}.md")
        return 1

    role = parse_role_file(role_path)
    if not role:
        if json_mode:
            _output_json({"error": "Failed to parse role file"})
        else:
            print(f"Failed to parse role file: {role_path}", file=sys.stderr)
        return 1

    if json_mode:
        _output_json({
            "name": role.name,
            "description": role.description,
            "path": str(role_path),
            "tools": role.tools,
            "disallowed_tools": role.disallowed_tools,
            "color": role.color,
            "instructions": role.instructions,
        })
        return 0

    print(f"Role: {role.name}")
    print(f"Description: {role.description or '(none)'}")
    print(f"Path: {role_path}")
    if role.tools:
        print(f"Tools (whitelist): {', '.join(role.tools)}")
    if role.disallowed_tools:
        print(f"Disallowed Tools: {', '.join(role.disallowed_tools)}")
    print()
    if role.instructions:
        print("Instructions:")
        print("-" * 40)
        print(role.instructions)
        print("-" * 40)
    else:
        print("Instructions: (none)")

    return 0


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


CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"


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


CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


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
    cli_safety.*_drift() so `agentwire doctor` can flag a hand-placed or drifted
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
        from agentwire.cli_safety import heal_damage_control
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


# === Tunnel Commands ===


def cmd_tunnels_up(args) -> int:
    """Create all required tunnels."""
    from .network import NetworkContext
    from .tunnels import TunnelManager

    ctx = NetworkContext.from_config()
    manager = TunnelManager()
    required = ctx.get_required_tunnels()

    if not required:
        print("No tunnels required for this machine's configuration.")
        print("(All services run locally or no remote services configured)")
        return 0

    print("Creating tunnels for this machine...\n")

    all_success = True
    for i, spec in enumerate(required, 1):
        # Get service name for display
        service_name = _get_service_for_tunnel(ctx, spec)

        print(f"[{i}/{len(required)}] {service_name} (localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port})")

        status = manager.create_tunnel(spec, ctx)

        if status.status == "up":
            if status.error:
                # Tunnel up but service not responding
                print(f"      ! Tunnel created (PID {status.pid})")
                print(f"      ! Warning: {status.error}")
            else:
                print(f"      + Tunnel created (PID {status.pid})")
        else:
            all_success = False
            print(f"      x Failed: {status.error}")
            _print_tunnel_help(spec, status.error)

        print()

    if all_success:
        print("All tunnels up. Services should be reachable.")
    else:
        print("Some tunnels failed. Check errors above.")
        return 1

    return 0


def cmd_tunnels_down(args) -> int:
    """Tear down all tunnels."""
    from .tunnels import TunnelManager

    manager = TunnelManager()
    count = manager.destroy_all_tunnels()

    if count == 0:
        print("No active tunnels to tear down.")
    else:
        print(f"Killed {count} tunnel(s).")

    return 0


def cmd_tunnels_status(args) -> int:
    """Show tunnel health."""
    from .network import NetworkContext
    from .tunnels import TunnelManager

    ctx = NetworkContext.from_config()
    manager = TunnelManager()

    # Get both required and active tunnels
    required = ctx.get_required_tunnels()
    active = manager.list_tunnels()

    print("AgentWire Tunnels")
    print("-" * 55)

    if not required and not active:
        print("\nNo tunnels configured or active.")
        print("(All services run locally or no remote services configured)")
        return 0

    # Show required tunnels
    for spec in required:
        service_name = _get_service_for_tunnel(ctx, spec)

        print(f"\n{service_name} (localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port})")

        status = manager.check_tunnel(spec)

        if status.status == "up":
            print(f"  Status: + UP (PID {status.pid})")
        elif status.status == "down":
            print("  Status: - DOWN")
        else:
            print("  Status: x ERROR")
            if status.error:
                print(f"  Error: {status.error}")

    # Show any orphaned tunnels (active but not required)
    required_ids = {s.id for s in required}
    orphaned = [t for t in active if t.spec.id not in required_ids]
    if orphaned:
        print("\n" + "-" * 55)
        print("\nOrphaned tunnels (active but no longer required):")
        for t in orphaned:
            print(f"  localhost:{t.spec.local_port} -> {t.spec.remote_machine}:{t.spec.remote_port}")
            print(f"    PID: {t.pid}, Status: {t.status}")

    print("\n" + "-" * 55)

    # Show next steps
    down_tunnels = [s for s in required if manager.check_tunnel(s).status != "up"]
    if down_tunnels:
        print("To create missing tunnels: agentwire tunnels up")

    return 0


def cmd_tunnels_check(args) -> int:
    """Verify tunnels are working with health checks."""
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health

    ctx = NetworkContext.from_config()
    manager = TunnelManager()
    required = ctx.get_required_tunnels()

    if not required:
        print("No tunnels required for this machine.")
        return 0

    print("Checking tunnel health...\n")

    all_healthy = True
    for spec in required:
        service_name = _get_service_for_tunnel(ctx, spec)
        status = manager.check_tunnel(spec)

        if status.status == "up":
            # Also test the actual service through the tunnel
            url = f"http://localhost:{spec.local_port}/health"
            healthy, err = test_service_health(url, timeout=3)

            if healthy:
                print(f"+ {service_name}: healthy")
            else:
                print(f"! {service_name}: tunnel up but service not responding")
                if err:
                    print(f"  {err}")
                all_healthy = False
        elif status.status == "down":
            print(f"x {service_name}: down")
            all_healthy = False
        else:
            print(f"x {service_name}: error - {status.error}")
            all_healthy = False

    if all_healthy:
        print("\nAll tunnels healthy.")
        return 0
    else:
        print("\nSome tunnels need attention. Run: agentwire tunnels up")
        return 1


def _get_service_for_tunnel(ctx, spec) -> str:
    """Get human-readable service name for a tunnel spec."""
    # Check which service this tunnel is for
    for service_name in ["portal", "tts"]:
        service_config = getattr(ctx.config.services, service_name, None)
        if service_config and service_config.machine == spec.remote_machine and service_config.port == spec.remote_port:
            return f"Portal -> {service_name.upper()}" if service_name != "portal" else "Portal"

    return f"Tunnel to {spec.remote_machine}"


def _print_tunnel_help(spec, error: str) -> None:
    """Print helpful diagnostics for tunnel errors."""
    if not error:
        return

    error_lower = error.lower()

    print("\n      Possible causes:")

    if "port" in error_lower and "in use" in error_lower:
        print("        1. Another process is using this port")
        print("        2. A previous tunnel wasn't cleaned up")
        print("\n      To diagnose:")
        print(f"        lsof -i :{spec.local_port}    # Find process using port")
        print("        agentwire tunnels down        # Clean up stale tunnels")

    elif "permission denied" in error_lower:
        print("        1. SSH key not authorized on remote machine")
        print("        2. Wrong user configured")
        print("\n      To fix:")
        print(f"        ssh-copy-id {spec.remote_machine}")

    elif "host key" in error_lower:
        print("        1. Remote machine was reinstalled/changed")
        print("        2. Possible security issue (man-in-the-middle)")
        print("\n      If expected, fix with:")
        print(f"        ssh-keygen -R {spec.remote_machine}")

    elif "connection refused" in error_lower:
        print("        1. SSH server not running on remote")
        print("        2. Firewall blocking port 22")
        print("\n      To diagnose:")
        print(f"        ssh {spec.remote_machine} echo ok")

    elif "timed out" in error_lower or "no route" in error_lower:
        print("        1. Machine is powered off or unreachable")
        print("        2. Network connectivity issue")
        print("\n      To diagnose:")
        print(f"        ping {spec.remote_machine}")

    elif "not responding" in error_lower:
        print("        1. Remote service not started")
        print("        2. Remote service on wrong port")
        print("\n      To diagnose:")
        print(f"        ssh {spec.remote_machine} 'lsof -i :{spec.remote_port}'")


class VersionAction(argparse.Action):
    """Custom version action that checks Python version and pip environment."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        # Print version
        print(f"agentwire {__version__}")
        print(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

        # Check version compatibility
        version_ok = check_python_version()
        env_ok = check_pip_environment()

        if version_ok and env_ok:
            print("\n✓ System is ready for AgentWire")
        else:
            print("\n⚠️  Please resolve the issues above before installing/running AgentWire")

        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser with every registered subcommand.

    Extracted from ``main()`` so the parser tree can be constructed without
    dispatching, which the CLI smoke tests rely on to enumerate subcommands
    and invoke their ``--help`` (see ``tests/unit/test_cli_smoke.py``).
    """
    parser = argparse.ArgumentParser(
        prog="agentwire",
        description="Multi-session voice web interface for AI coding agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Command Categories:
  Getting Started:
    init             Interactive setup wizard
    portal           Manage the web portal
    new              Create a new Claude Code session
    say              Speak text via TTS

  Sessions:
    list             List panes or sessions
    info             Get session information
    kill             Kill a session or pane
    spawn            Spawn a worker pane in current session
    worktree         Create a git worktree + session
    send             Send prompt to a session or pane
    output           Read session or pane output

  Voice:
    listen           Voice input recording
    voiceclone       Record and upload voice clones
    tts              Manage TTS server
    stt              Manage STT server

  Diagnostics:
    doctor           Auto-diagnose and fix common issues
    network          Network diagnostics and status
    safety           Damage control security commands
    hooks            Manage agentwire hook files

  Advanced:
    council          Multi-soul council operations
    scheduler        Manage the task scheduler
    ensure           Run named task with reliable session management
    limits           Usage-limit recovery management
"""
    )
    parser.add_argument(
        "--version",
        action=VersionAction,
        help="Show version and check system compatibility",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # === init command ===
    init_parser = subparsers.add_parser("init", help="Interactive setup wizard")
    init_parser.add_argument(
        "--assisted", action="store_true",
        help="Spawn the interactive Claude setup session at the end "
             "(default: end on the portal-URL next steps)"
    )
    init_parser.set_defaults(func=cmd_init)

    # === quo command ===
    from agentwire.channels.quo import cmd_quo
    quo_parser = subparsers.add_parser("quo", help="Send SMS via Quo (OpenPhone)")
    quo_parser.add_argument("--body", "-b", type=str, help="Message body (or pipe via stdin)")
    quo_parser.add_argument("--to", type=str, help="Recipient phone number (+E.164 format)")
    quo_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    quo_parser.set_defaults(func=cmd_quo)

    # === tunnels command group ===
    tunnels_parser = subparsers.add_parser("tunnels", help="Manage SSH tunnels for service routing")
    tunnels_subparsers = tunnels_parser.add_subparsers(dest="tunnels_command")

    # tunnels up
    tunnels_up = tunnels_subparsers.add_parser("up", help="Create all required tunnels")
    tunnels_up.set_defaults(func=cmd_tunnels_up)

    # tunnels down
    tunnels_down = tunnels_subparsers.add_parser("down", help="Tear down all tunnels")
    tunnels_down.set_defaults(func=cmd_tunnels_down)

    # tunnels status
    tunnels_status = tunnels_subparsers.add_parser("status", help="Show tunnel health")
    tunnels_status.set_defaults(func=cmd_tunnels_status)

    # tunnels check
    tunnels_check = tunnels_subparsers.add_parser("check", help="Verify tunnels are working")
    tunnels_check.set_defaults(func=cmd_tunnels_check)

    # === notify command (worker→parent) ===
    notify_cmd_parser = subparsers.add_parser("notify-parent", help="Notify parent session (worker→orchestrator)")
    notify_cmd_parser.add_argument("text", nargs="*", help="Notification message")
    notify_cmd_parser.add_argument("--to", type=str, metavar="SESSION", help="Target session (default: parent from .agentwire.yml)")
    notify_cmd_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress output")
    notify_cmd_parser.add_argument("--raw", action="store_true",
                                   help="Send the message verbatim (no [NOTIFY from ...] prefix)")
    notify_cmd_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_cmd_parser.set_defaults(func=cmd_notify_parent)

    # === open command (artifact windows) ===
    open_parser = subparsers.add_parser("open", help="Open a URL or local file as an artifact window in the portal")
    open_parser.add_argument("url", help="URL or filename to open (filenames served from ~/.agentwire/artifacts/)")
    open_parser.add_argument("--title", "-t", type=str, default="Artifact", help="Window title")
    open_parser.add_argument("--artifact-id", type=str, help="Unique window ID (auto-generated if omitted)")
    open_parser.add_argument("--json", action="store_true", help="Output JSON")
    open_parser.set_defaults(func=cmd_open)

    # === email command ===
    from agentwire.channels.email import cmd_email
    email_parser = subparsers.add_parser("email", help="Send branded email notification via Resend")
    email_parser.add_argument(
        "--to", action="append", default=None,
        help="Recipient email. Repeat or pass comma-separated for multiple recipients (default: from config).",
    )
    email_parser.add_argument("--subject", "-s", type=str, help="Email subject")
    email_parser.add_argument("--body", "-b", type=str, help="Email body - markdown supported (or pipe via stdin)")
    email_parser.add_argument("--attach", "-a", type=str, action="append", help="Attach file (can use multiple times)")
    email_parser.add_argument("--plain", action="store_true", help="Send plain text only (no HTML template)")
    email_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    email_parser.set_defaults(func=cmd_email)

    # === push command (#483) ===
    from agentwire.channels.push import cmd_push
    push_parser = subparsers.add_parser(
        "push", help="Web Push (VAPID) for the PWA — generate keys / check status"
    )
    push_sub = push_parser.add_subparsers(dest="push_cmd")
    push_sub.add_parser("keygen", help="Generate a VAPID keypair for ~/.agentwire/.env")
    push_sub.add_parser("status", help="Show push readiness + subscription count")
    push_parser.set_defaults(func=cmd_push)

    # === fetch command ===
    from agentwire.fetch import cmd_fetch
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch a URL via Jina Reader — handles JS-rendered pages, returns clean markdown.",
    )
    fetch_parser.add_argument("url", help="URL to fetch")
    fetch_parser.add_argument(
        "--limit", "-l", type=int, default=8000,
        help="Max characters to return (default: 8000, 0 = no limit)",
    )
    fetch_parser.set_defaults(func=cmd_fetch)

    # === notify command ===
    notify_parser = subparsers.add_parser("notify-event", help="Broadcast a portal lifecycle event (session/pane state change); usually called by tmux hooks")
    notify_parser.add_argument(
        "event",
        help="Event type: session_closed, session_created, pane_died, pane_created, "
             "client_attached, client_detached, session_renamed, pane_focused, window_activity"
    )
    notify_parser.add_argument("-s", "--session", help="Session name")
    notify_parser.add_argument("--pane", type=int, help="Pane index (for pane events)")
    notify_parser.add_argument("--pane-id", help="Pane ID from tmux (for pane events via hooks)")
    notify_parser.add_argument("--old-name", help="Old session name (for session_renamed)")
    notify_parser.add_argument("--new-name", help="New session name (for session_renamed)")
    notify_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_parser.set_defaults(func=cmd_notify)

    # notify-user: human-facing desktop toast (the CLI twin of MCP notify_user)
    notify_user_parser = subparsers.add_parser("notify-user", help="Show the human a desktop toast on the portal")
    notify_user_parser.add_argument("text", nargs="+", help="Toast text (supports a safe markdown subset: bold, links, line breaks)")
    notify_user_parser.add_argument("-s", "--session", help="Session this relates to (shown as a badge)")
    notify_user_parser.add_argument("--priority", default="normal", choices=["normal", "high"], help="Toast priority")
    notify_user_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_user_parser.set_defaults(func=cmd_notify_user)

    # research: Briefing Mode dropbox path resolver
    research_parser = subparsers.add_parser("research", help="Resolve the Briefing Mode research dropbox path for a session")
    research_sub = research_parser.add_subparsers(dest="research_command")
    for _verb, _help in (("dir", "Print the dropbox path (not created)"),
                         ("ensure", "Create + print the dropbox path")):
        _rp = research_sub.add_parser(_verb, help=_help)
        _rp.add_argument("-s", "--session", default=None, help="Anchor session (default: current)")
        _rp.add_argument("--json", action="store_true", help="Output as JSON")
        _rp.set_defaults(func=cmd_research)
    research_parser.add_argument("-s", "--session", default=None, help="Anchor session (default: current)")
    research_parser.add_argument("--json", action="store_true", help="Output as JSON")
    research_parser.set_defaults(func=cmd_research)

    # wiki: deterministic mechanical ops for the LLM wiki
    from . import wiki as _wiki_mod
    wiki_parser = subparsers.add_parser("wiki", help="Deterministic mechanical ops for the LLM wiki")
    wiki_parser.add_argument("--root", type=Path, default=None,
                             help=f"Wiki root (default: {_wiki_mod.DEFAULT_WIKI_ROOT})")
    wiki_sub = wiki_parser.add_subparsers(dest="wiki_command")

    _ws = wiki_sub.add_parser("status", help="Page counts, unprocessed raw, lint summary")
    _ws.add_argument("--json", action="store_true", help="Output as JSON")
    _ws.set_defaults(func=cmd_wiki)

    _wq = wiki_sub.add_parser("query", help="Ranked deterministic search (caller synthesizes)")
    _wq.add_argument("query")
    _wq.add_argument("--limit", type=int, default=10)
    _wq.add_argument("--json", action="store_true", help="Output as JSON")
    _wq.set_defaults(func=cmd_wiki)

    _wl = wiki_sub.add_parser("lint", help="Structural + ground-truth checks (never auto-fixes)")
    _wl.add_argument("--strict", action="store_true", help="Exit 1 when issues found")
    _wl.add_argument("--json", action="store_true", help="Output as JSON")
    _wl.set_defaults(func=cmd_wiki)

    _wn = wiki_sub.add_parser("new", help="Scaffold wiki/<category>/<name>.md")
    _wn.add_argument("category", choices=_wiki_mod.CATEGORIES)
    _wn.add_argument("name")
    _wn.add_argument("--title", default=None)
    _wn.add_argument("--json", action="store_true", help="Output as JSON")
    _wn.set_defaults(func=cmd_wiki)

    _wd = wiki_sub.add_parser("done", help="Archive raw/<f> → raw/processed/<f>")
    _wd.add_argument("rawfile")
    _wd.add_argument("--json", action="store_true", help="Output as JSON")
    _wd.set_defaults(func=cmd_wiki)

    wiki_parser.set_defaults(func=cmd_wiki)

    # === dev command ===
    dev_parser = subparsers.add_parser(
        "dev", help="Start/attach to dev agentwire session"
    )
    dev_parser.add_argument("--no-soul", dest="no_soul", action="store_true", help="Skip soul personality role injection for this session")
    dev_parser.set_defaults(func=cmd_dev)

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

    # === handoff command group ===
    handoff_parser = subparsers.add_parser(
        "handoff",
        help="Shareable conversation bundles (ai-handoff.md + show-the-story.html)",
    )
    handoff_subparsers = handoff_parser.add_subparsers(dest="handoff_command")

    handoff_init_parser = handoff_subparsers.add_parser(
        "init",
        help="Create a bundle dir + pre-filled ai-handoff.md template",
    )
    handoff_init_parser.add_argument(
        "--title", help="Short title hint, used in the bundle slug"
    )
    handoff_init_parser.add_argument(
        "--output-dir", help="Override artifacts dir (default: ~/.agentwire/artifacts)"
    )
    handoff_init_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_init_parser.set_defaults(func=cmd_handoff_init)

    handoff_render_parser = handoff_subparsers.add_parser(
        "render",
        help="Render show-the-story.html from an existing ai-handoff.md",
    )
    handoff_render_parser.add_argument(
        "path", help="Bundle dir or path to ai-handoff.md"
    )
    handoff_render_parser.add_argument(
        "--story", action="store_true", default=True,
        help="Render show-the-story.html (default: on)",
    )
    handoff_render_parser.add_argument(
        "--no-story", dest="story", action="store_false",
        help="Skip HTML render (only validates the markdown)",
    )
    handoff_render_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_render_parser.set_defaults(func=cmd_handoff_render)

    handoff_list_parser = handoff_subparsers.add_parser(
        "list", help="List past handoff bundles"
    )
    handoff_list_parser.add_argument(
        "--json", action="store_true", help="JSON output"
    )
    handoff_list_parser.set_defaults(func=cmd_handoff_list)

    # === roles command group ===
    roles_parser = subparsers.add_parser(
        "roles", help="Manage composable roles"
    )
    roles_subparsers = roles_parser.add_subparsers(dest="roles_command")

    # roles list
    roles_list = roles_subparsers.add_parser("list", help="List available roles")
    roles_list.add_argument("--json", action="store_true", help="Output as JSON")
    roles_list.set_defaults(func=cmd_roles_list)

    # roles show <name>
    roles_show = roles_subparsers.add_parser("show", help="Show role details")
    roles_show.add_argument("name", help="Role name")
    roles_show.add_argument("--json", action="store_true", help="Output as JSON")
    roles_show.set_defaults(func=cmd_roles_show)

    # === projects command group ===
    projects_parser = subparsers.add_parser(
        "projects", help="Discover and list projects"
    )
    projects_subparsers = projects_parser.add_subparsers(dest="projects_command")

    # projects list
    projects_list = projects_subparsers.add_parser("list", help="List discovered projects")
    projects_list.add_argument("--machine", help="Filter by machine ID (e.g., 'local', 'mac-studio')")
    projects_list.add_argument("--json", action="store_true", help="Output as JSON")
    projects_list.set_defaults(func=cmd_projects_list)

    # projects create
    projects_create = projects_subparsers.add_parser(
        "create",
        help="Create a new local project under projects.dir with a default .agentwire.yml",
    )
    projects_create.add_argument("name", help="Project name (directory name under projects.dir)")
    projects_create.add_argument(
        "--from", dest="from_url", help="Clone from this git URL instead of creating an empty directory"
    )
    projects_create.add_argument(
        "--git-init", action="store_true", help="Run 'git init' in the new project (ignored when --from is given)"
    )
    projects_create.add_argument("--json", action="store_true", help="Output as JSON")
    projects_create.set_defaults(func=cmd_projects_create)

    # === hooks command group ===
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

    # === mcp command ===
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Run MCP server for external agent integration",
        description="Expose AgentWire as an MCP server for tools like MoltBot, Claude Desktop, etc.",
    )
    mcp_parser.set_defaults(func=cmd_mcp)

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

    # === Extracted command groups (each registrar owns its own subparser) ===
    # Phase 1 of #495 appends one entry here per extracted domain.
    #   - limits: usage-limit recovery (detect dialog, park, auto-resume)
    #   - diff:   structured git diff for the mobile Review window
    #   - prompts: prompt routing (rides the limits watchdog)
    #   - msg:    polite agent-to-agent inbox (rides the watchdog)
    from . import diff_cli, limits_cli, msg_cli, pane_cli, prompts_cli, send_cli  # noqa: I001  # session_cli kept on its own line below to minimize Phase 1 #495 merge conflicts
    from . import session_cli
    from . import channels_cli, portal_cli, tts_cli
    from . import scheduler_cli, ensure_cli
    from . import doctor_cli, history_cli, machine_cli, safety_cli
    from .council import cli as council_cli

    _REGISTRARS = [  # noqa: N806  # registry constant; Phase 1 of #495 appends here
        diff_cli.register_diff_parser,
        prompts_cli.register_prompts_parser,
        msg_cli.register_msg_parser,
        limits_cli.register_limits_parser,
        pane_cli.register_pane_parser,
        send_cli.register_send_parser,
        session_cli.register_session_parser,
        portal_cli.register_portal_parser,
        tts_cli.register_tts_parser,
        channels_cli.register_channels_parser,
        scheduler_cli.register_scheduler_parser,
        ensure_cli.register_ensure_parser,
        doctor_cli.register_doctor_parser,
        safety_cli.register_safety_parser,
        machine_cli.register_machine_parser,
        history_cli.register_history_parser,
    ]
    for _reg in _REGISTRARS:
        _reg(subparsers)

    # === council command group ===
    council_parser = subparsers.add_parser(
        "council",
        help="Multi-soul council: fan a prompt out to lens sessions, synthesize",
        description=(
            "An agentwire-council orchestrator session fans prompts out to a "
            "roster of lens souls (brain, conscience, gut, critic, historian, "
            "devils-advocate), collects their takes through a file inbox, and "
            "synthesizes. See docs/wiki/council.md."
        ),
    )
    council_subparsers = council_parser.add_subparsers(dest="council_command")

    # Targeting is shared: --name picks the sitting; absent, the cwd-repo-slug
    # if it matches a live sitting, else the sole live sitting, else error.
    _name_help = "Sitting name (default: cwd-repo-slug / sole live sitting)"

    # council start
    c_start = council_subparsers.add_parser(
        "start", help="Start a sitting: orchestrator + all soul sessions"
    )
    c_start.add_argument(
        "--name", help="Sitting name (default: cwd-repo-slug)"
    )
    c_start.add_argument(
        "--roster", help="Comma-separated lens names (default: full bundled roster)"
    )
    c_start.add_argument("--type", help="Session type (default: claude-bypass)")
    c_start.add_argument("--model", help="Model override for all council sessions")
    c_start.add_argument(
        "--force", action="store_true", help="Tear down a live sitting first"
    )
    c_start.add_argument("--json", action="store_true", help="Output JSON")
    c_start.set_defaults(func=council_cli.cmd_council_start)

    # council stop
    c_stop = council_subparsers.add_parser(
        "stop", help="Kill the sitting's sessions (prompt history kept)"
    )
    c_stop.add_argument("--name", help=_name_help)
    c_stop.add_argument("--json", action="store_true", help="Output JSON")
    c_stop.set_defaults(func=council_cli.cmd_council_stop)

    # council status
    c_status = council_subparsers.add_parser(
        "status", help="Sitting state, session liveness, open prompts"
    )
    c_status.add_argument("--name", help=_name_help)
    c_status.add_argument("--json", action="store_true", help="Output JSON")
    c_status.set_defaults(func=council_cli.cmd_council_status)

    # council list
    c_list = council_subparsers.add_parser(
        "list", help="Every known sitting: name, cwd, age, live sessions, prompts"
    )
    c_list.add_argument("--json", action="store_true", help="Output JSON")
    c_list.set_defaults(func=council_cli.cmd_council_list)

    # council ask
    c_ask = council_subparsers.add_parser(
        "ask", help="Fan a prompt out to every soul in the sitting"
    )
    c_ask.add_argument("prompt", nargs="?", help="Prompt text (or --file / stdin)")
    c_ask.add_argument("--name", help=_name_help)
    c_ask.add_argument("--file", help="Read prompt text from a file")
    c_ask.add_argument("--json", action="store_true", help="Output JSON")
    c_ask.set_defaults(func=council_cli.cmd_council_ask)

    # council collect
    c_collect = council_subparsers.add_parser(
        "collect", help="Wait for every soul's take/ack/pass (or timeout)"
    )
    c_collect.add_argument("--name", help=_name_help)
    c_collect.add_argument("--prompt", type=int, help="Prompt id (default: latest)")
    c_collect.add_argument(
        "--timeout", type=float, default=120, help="Soft timeout in seconds (default: 120)"
    )
    c_collect.add_argument(
        "--no-wait", action="store_true", help="Snapshot once, don't block"
    )
    c_collect.add_argument("--json", action="store_true", help="Output JSON")
    c_collect.set_defaults(func=council_cli.cmd_council_collect)

    # council reply (run by souls)
    c_reply = council_subparsers.add_parser(
        "reply", help="File a soul's reply: --take / --ack / --pass"
    )
    c_reply.add_argument("--name", help=_name_help)
    c_reply.add_argument("--prompt", type=int, help="Prompt id (default: latest)")
    c_reply.add_argument(
        "--take", action="store_true", help="Substantive take (text required)"
    )
    c_reply.add_argument(
        "--ack", action="store_true", help="Researching — follow-up coming"
    )
    c_reply.add_argument(
        "--pass", action="store_true", help="Nothing to add through this lens"
    )
    c_reply.add_argument("--soul", help="Lens name (default: inferred from session)")
    c_reply.add_argument("--text", help="Reply text")
    c_reply.add_argument("--file", help="Read reply text from a file")
    c_reply.add_argument("--json", action="store_true", help="Output JSON")
    c_reply.set_defaults(func=council_cli.cmd_council_reply)

    return parser


def _find_subparser(parser: argparse.ArgumentParser, *names: str):
    """Walk the subparser tree by command name(s); return the parser or None."""
    current = parser
    for name in names:
        sub = None
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                sub = action.choices.get(name)
                break
        if sub is None:
            return None
        current = sub
    return current


# Command groups whose bare invocation (no subcommand) prints group help.
_GROUP_COMMANDS = [
    "portal", "tts", "stt", "tunnels", "machine", "history", "handoff",
    "wiki", "hooks", "projects", "safety", "network", "listen",
    "voiceclone", "roles", "task", "lock", "scheduler", "council", "limits",
]


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    if (
        args.command in _GROUP_COMMANDS
        and getattr(args, f"{args.command}_command", None) is None
    ):
        _find_subparser(parser, args.command).print_help()
        return 0

    if (
        args.command == "safety"
        and getattr(args, "safety_command", None) == "tooldefs"
        and getattr(args, "tooldefs_command", None) is None
    ):
        _find_subparser(parser, "safety", "tooldefs").print_help()
        return 0

    if hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
