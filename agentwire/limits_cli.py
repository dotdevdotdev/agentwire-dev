"""CLI for usage-limit recovery — ``agentwire limits ...``.

The watchdog is a stateless launchd job running ``agentwire limits tick``
every minute: sweep all tmux panes for the usage-limit dialog, park what's
found, and nudge parked sessions whose reset time has passed. ``install``
writes and loads the LaunchAgent; the plist is generated here (single
source of truth — no template file to drift).
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


def cmd_limits_tick(args) -> int:
    """One watchdog pass (called by launchd every minute).

    Runs the usage-limit sweep FIRST, then prompt routing (#276), then the
    polite-message inbox drain (#296) — the ordering guarantees a usage-limit
    dialog is parked before either sweep looks at the pane, and that the
    inbox only ever delivers to panes the prompt sweep already cleared.
    """
    from agentwire import inbox, prompt_router

    result = usage_limit.tick()
    prompts = prompt_router.tick()
    messages = inbox.tick()
    if getattr(args, "json", False):
        print(json.dumps({**result, "prompts": prompts, "messages": messages}))
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


def cmd_limits_install(args) -> int:
    """Install + load the launchd watchdog."""
    if sys.platform != "darwin":
        print("limits install is macOS-only (launchd)", file=sys.stderr)
        return 1

    agentwire_bin = _find_agentwire()
    content = _plist_content(agentwire_bin)

    if getattr(args, "dry_run", False):
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


def cmd_limits_uninstall(args) -> int:
    """Unload + remove the launchd watchdog."""
    if sys.platform != "darwin":
        print("limits uninstall is macOS-only (launchd)", file=sys.stderr)
        return 1
    if PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
        PLIST_PATH.unlink()
        print(f"Uninstalled {LAUNCHD_LABEL}")
    else:
        print("Watchdog not installed")
    return 0
