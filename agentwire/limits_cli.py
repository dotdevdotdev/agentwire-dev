"""CLI for usage-limit recovery — ``agentwire limits ...``.

The watchdog is a stateless scheduled job running ``agentwire limits tick``
every minute: sweep all tmux panes for the usage-limit dialog, park what's
found, and nudge parked sessions whose reset time has passed. ``install``
picks the platform backend — a launchd LaunchAgent on macOS, a systemd
--user timer+service on Linux — and the unit content is generated here
(single source of truth — no template file to drift).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import usage_limit

LAUNCHD_LABEL = "dev.agentwire.usage-limit-watchdog"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "agentwire-usage-limit-watchdog.log"
TICK_INTERVAL = 60  # seconds — limits don't keep office hours

# Linux equivalent: a systemd --user timer + oneshot service (generated here,
# same single-source-of-truth stance as the plist).
SYSTEMD_UNIT = "agentwire-usage-limit-watchdog"
SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
SERVICE_PATH = SYSTEMD_DIR / f"{SYSTEMD_UNIT}.service"
TIMER_PATH = SYSTEMD_DIR / f"{SYSTEMD_UNIT}.timer"


def _plist_content(agentwire_bin: str) -> str:
    home = str(Path.home())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!--
  Usage-limit watchdog — detects the Claude Code usage-limit dialog in any
  tmux pane, parks the session (option 1: stop and wait), and nudges it
  back to work after the limit resets. Deterministic plain code, no agent.

  Managed by `agentwire limits install` / `agentwire limits uninstall`.
  Logs: tail -f {LOG_PATH}
-->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{agentwire_bin}</string>
        <string>limits</string>
        <string>tick</string>
    </array>

    <key>StartInterval</key>
    <integer>{TICK_INTERVAL}</integer>

    <key>WorkingDirectory</key>
    <string>{home}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>{home}</string>
    </dict>

    <key>StandardOutPath</key>
    <string>{LOG_PATH}</string>

    <key>StandardErrorPath</key>
    <string>{LOG_PATH}</string>

    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


def _systemd_service_content(agentwire_bin: str) -> str:
    home = str(Path.home())
    return f"""# Usage-limit watchdog — detects the Claude Code usage-limit dialog in any
# tmux pane, parks the session (option 1: stop and wait), and nudges it
# back to work after the limit resets. Deterministic plain code, no agent.
#
# Managed by `agentwire limits install` / `agentwire limits uninstall`.
# Logs: journalctl --user -u {SYSTEMD_UNIT}
[Unit]
Description=AgentWire usage-limit watchdog tick

[Service]
Type=oneshot
ExecStart={agentwire_bin} limits tick
WorkingDirectory={home}
Environment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME={home}
"""


def _systemd_timer_content() -> str:
    return f"""# Fires the usage-limit watchdog tick every {TICK_INTERVAL}s.
# Managed by `agentwire limits install` / `agentwire limits uninstall`.
[Unit]
Description=AgentWire usage-limit watchdog ({TICK_INTERVAL}s tick)

[Timer]
OnBootSec={TICK_INTERVAL}
OnUnitActiveSec={TICK_INTERVAL}
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


def _find_agentwire() -> str:
    found = shutil.which("agentwire")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "agentwire")


def _fmt_local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %I:%M%p")
    except (ValueError, TypeError):
        return str(iso)


WATCHDOG_EVENTS_FILE = Path.home() / ".agentwire" / "watchdog-events.jsonl"


def _log_stage_failure(stage: str, exc: BaseException) -> None:
    """Append a stage-failure record to the watchdog events log (best-effort).

    The watchdog runs unattended under launchd; a stage raising must never go
    silent. We log to stderr (captured in the launchd log) AND to a jsonl event
    so the failure is both immediately visible and durably auditable.
    """
    record = {
        "ts": datetime.now().astimezone().isoformat(),
        "event": "stage_failed",
        "stage": stage,
        "error": f"{type(exc).__name__}: {exc}",
    }
    print(f"watchdog stage '{stage}' failed: {record['error']}", file=sys.stderr)
    try:
        WATCHDOG_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(WATCHDOG_EVENTS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _run_stage(stage: str, fn, default):
    """Run one watchdog stage in isolation.

    A raise in one stage is logged and swallowed so the remaining stages still
    run this cycle — without this guard the first raise propagates out of the
    CLI dispatcher, the process exits, and every later stage is silently skipped
    until the next minute's tick (see #490).
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — isolate stages, never starve the rest
        _log_stage_failure(stage, exc)
        return default


def cmd_limits_tick(args) -> int:
    """One watchdog pass (called by launchd every minute).

    Runs the usage-limit sweep FIRST, then prompt routing (#276), then the
    polite-message inbox drain (#296), then context auto-management (#442) —
    the ordering guarantees a usage-limit dialog is parked before any sweep
    looks at the pane, that the inbox only ever delivers to panes the prompt
    sweep already cleared, and that auto-``/clear`` runs LAST so it never fights
    a pending paste/prompt for the same idle, empty box.

    Each stage runs inside :func:`_run_stage` so an exception in one subsystem
    is logged and skipped rather than aborting the whole cycle (#490).
    """
    from agentwire import inbox, prompt_router, session_context

    result = _run_stage(
        "usage_limit", usage_limit.tick,
        {"skipped": None, "parked": [], "resumed": [], "waiting": []})
    prompts = _run_stage(
        "prompt_router", prompt_router.tick, {"routed": [], "deferred": []})
    messages = _run_stage(
        "inbox", inbox.tick, {"flushed": [], "deferred": []})
    context = _run_stage(
        "session_context", session_context.tick, {"acted": [], "deferred": []})
    if getattr(args, "json", False):
        print(json.dumps({
            **result, "prompts": prompts, "messages": messages, "context": context,
        }))
        return 0
    if result.get("skipped"):
        print(result["skipped"])
    else:
        if result["parked"]:
            print(f"parked: {', '.join(result['parked'])}")
        if result["resumed"]:
            print(f"resumed: {', '.join(result['resumed'])}")
        if result["waiting"]:
            print(f"waiting on reset: {', '.join(result['waiting'])}")
        if not (result["parked"] or result["resumed"] or result["waiting"]):
            print("no usage-limit activity")
    routed = prompts.get("routed") or []
    deferred = prompts.get("deferred") or []
    if routed:
        print("prompts routed: " + ", ".join(
            f"{e['session']}.{e['pane']}→{e['parent']}" for e in routed))
    if deferred:
        print("prompts deferred: " + ", ".join(
            f"{e['session']}.{e['pane']}" for e in deferred))
    flushed = messages.get("flushed") or []
    msg_deferred = messages.get("deferred") or []
    if flushed:
        print("messages delivered: " + ", ".join(
            f"{e['session']}×{e['delivered']}" for e in flushed))
    if msg_deferred:
        print("messages deferred: " + ", ".join(
            f"{e['session']}({e['reason']})" for e in msg_deferred))
    ctx_acted = context.get("acted") or []
    ctx_deferred = context.get("deferred") or []
    if ctx_acted:
        print("context auto-managed: " + ", ".join(
            f"{e['session']}→{e['command']}@{e['remaining_pct']}%" for e in ctx_acted))
    if ctx_deferred:
        print("context deferred: " + ", ".join(
            f"{e['session']}({e['deferred']})" for e in ctx_deferred))
    return 0


def cmd_limits_status(args) -> int:
    """Show currently parked sessions."""
    parked = usage_limit.list_parked()
    if getattr(args, "json", False):
        print(json.dumps({"parked": parked}, indent=2))
        return 0
    if not parked:
        print("No sessions parked on usage limits")
        return 0
    print(f"{len(parked)} session(s) parked on usage limits:\n")
    for state in parked:
        print(f"  {state['session']}")
        if state.get("task"):
            print(f"    task:    {state['task']}")
        print(f"    parked:  {_fmt_local(state.get('parked_at', ''))}")
        print(f"    resets:  {_fmt_local(state.get('reset_at', ''))}")
        print(f"    resumes: {_fmt_local(state.get('resume_at', ''))}"
              + ("" if state.get("notified") else "  (notification pending)"))
    return 0


def cmd_limits_resume(args) -> int:
    """Manually resume a parked session now."""
    session = args.session
    state = usage_limit.read_park_state(session)
    if state is None:
        print(f"Session '{session}' is not parked", file=sys.stderr)
        return 1
    if usage_limit.resume_session(state, force=getattr(args, "force", False)):
        print(f"Resumed {session}")
        return 0
    print(f"Failed to resume {session} (see usage-limit-events.jsonl)", file=sys.stderr)
    return 1


def _install_launchd(dry_run: bool) -> int:
    agentwire_bin = _find_agentwire()
    content = _plist_content(agentwire_bin)

    if dry_run:
        print(f"# would write: {PLIST_PATH}")
        print(f"# then: launchctl unload/load {PLIST_PATH}")
        print(content)
        return 0

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(content)

    # Reload: unload is a no-op error if not loaded — ignore it.
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"Installed {LAUNCHD_LABEL} (tick every {TICK_INTERVAL}s)")
    print(f"  plist: {PLIST_PATH}")
    print(f"  logs:  tail -f {LOG_PATH}")
    return 0


def _install_systemd(dry_run: bool) -> int:
    agentwire_bin = _find_agentwire()
    service = _systemd_service_content(agentwire_bin)
    timer = _systemd_timer_content()

    if dry_run:
        print(f"# would write: {SERVICE_PATH}")
        print(service)
        print(f"# would write: {TIMER_PATH}")
        print(timer)
        print(f"# then: systemctl --user daemon-reload && systemctl --user enable --now {SYSTEMD_UNIT}.timer")
        return 0

    if shutil.which("systemctl") is None:
        print("systemctl not found — limits install needs systemd --user on Linux",
              file=sys.stderr)
        return 1

    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_PATH.write_text(service)
    TIMER_PATH.write_text(timer)

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"systemctl enable failed: {result.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"Installed {SYSTEMD_UNIT}.timer (tick every {TICK_INTERVAL}s)")
    print(f"  units: {SERVICE_PATH}")
    print(f"         {TIMER_PATH}")
    print(f"  logs:  journalctl --user -u {SYSTEMD_UNIT} -f")
    return 0


def cmd_limits_install(args) -> int:
    """Install + start the watchdog scheduler (launchd on macOS, systemd --user on Linux)."""
    dry_run = getattr(args, "dry_run", False)
    if sys.platform == "darwin":
        return _install_launchd(dry_run)
    if sys.platform.startswith("linux"):
        return _install_systemd(dry_run)
    print(f"limits install has no scheduler backend for {sys.platform} "
          "(supported: macOS launchd, Linux systemd --user)", file=sys.stderr)
    return 1


def cmd_limits_uninstall(args) -> int:
    """Stop + remove the watchdog scheduler for this platform."""
    if sys.platform == "darwin":
        if PLIST_PATH.exists():
            subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
            PLIST_PATH.unlink()
            print(f"Uninstalled {LAUNCHD_LABEL}")
        else:
            print("Watchdog not installed")
        return 0
    if sys.platform.startswith("linux"):
        if not (SERVICE_PATH.exists() or TIMER_PATH.exists()):
            print("Watchdog not installed")
            return 0
        if shutil.which("systemctl"):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"],
                capture_output=True,
            )
        for path in (TIMER_PATH, SERVICE_PATH):
            if path.exists():
                path.unlink()
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print(f"Uninstalled {SYSTEMD_UNIT}")
        return 0
    print(f"limits uninstall has no scheduler backend for {sys.platform}", file=sys.stderr)
    return 1


def register_limits_parser(subparsers) -> None:
    """Register the `limits` command group (usage-limit recovery)."""
    limits_parser = subparsers.add_parser(
        "limits",
        help="Usage-limit recovery: detect the limit dialog, park, auto-resume",
        description=(
            "Deterministic recovery from Claude Code usage-limit dialogs: a "
            "watchdog (launchd on macOS, systemd --user timer on Linux) ticks "
            "every minute, parks sessions sitting on "
            "the dialog (option 1: stop and wait), emails the owner, and "
            "nudges the session back to work after the limit resets. "
            "See docs/wiki/usage-limit-recovery.md."
        ),
    )
    limits_subparsers = limits_parser.add_subparsers(dest="limits_command")

    # limits tick
    l_tick = limits_subparsers.add_parser(
        "tick", help="One watchdog pass: sweep panes, resume what's due"
    )
    l_tick.add_argument("--json", action="store_true", help="Output JSON")
    l_tick.set_defaults(func=cmd_limits_tick)

    # limits status
    l_status = limits_subparsers.add_parser("status", help="Show parked sessions")
    l_status.add_argument("--json", action="store_true", help="Output JSON")
    l_status.set_defaults(func=cmd_limits_status)

    # limits resume
    l_resume = limits_subparsers.add_parser(
        "resume", help="Manually resume a parked session now"
    )
    l_resume.add_argument("-s", "--session", required=True, help="Parked session name")
    l_resume.add_argument(
        "--force", action="store_true",
        help="Archive the park state even if nudge delivery can't be verified",
    )
    l_resume.set_defaults(func=cmd_limits_resume)

    # limits install / uninstall
    l_install = limits_subparsers.add_parser(
        "install", help="Install + start the watchdog (launchd / systemd --user, 60s tick)"
    )
    l_install.add_argument(
        "--dry-run", action="store_true",
        help="Print the plist / systemd units without installing",
    )
    l_install.set_defaults(func=cmd_limits_install)

    l_uninstall = limits_subparsers.add_parser(
        "uninstall", help="Stop + remove the watchdog scheduler"
    )
    l_uninstall.set_defaults(func=cmd_limits_uninstall)
