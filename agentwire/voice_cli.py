"""``agentwire voice`` — the portal voice assistant service (#1198).

``agentwire-voice`` is a supervised **service of kind agent** (precedent:
``agentwire-notifications``): a root Claude Code session in the agentwire
source checkout running the ``voice-assistant`` role, so CLAUDE.md, the skills
and the agentwire MCP tools all load. It is classified as infrastructure
(``services._BUILTIN_SERVICE_SESSIONS`` + the portal's Services allowlist), so
it is listed and attachable but kept out of topology, cohorts, dangling-PR
checks and idle-nag.

Every entry point ends in the same place — text sent to ``agentwire-voice`` —
and this module is that one place: ``voice ask`` is what the portal button,
Hammerspoon and anything else call.

Lifecycle reuses the services registry rather than a parallel supervisor:

- ``install`` / ``uninstall`` write one FIXED ``services.custom`` entry into
  ``~/.agentwire/config.yaml`` (protected control plane), so they are
  human-only: a tty gate here plus a ``PROTECTED_COMMAND_PATTERNS`` entry that
  stops an agent's Bash tool first — the ``buddy service install`` precedent.
- ``start`` / ``stop`` / ``status`` are ``services up|down|status`` for that
  entry; ``restart`` is ``agentwire restart`` (same conversation, flags
  regenerated from the launch record). The entry sets ``resume: true``, so a
  watchdog respawn resumes too.

Model and effort (owner decision 2026-10-30): Haiku 5.5 at medium effort, low
as the speed fallback. They are per-session launch flags recorded in the
session's launch record — no other session's defaults and no user settings
change.
"""

import argparse
import sys

from .core import EFFORT_LEVELS, _output_json, _output_result
from .voice_hooks import SESSION

SERVICE_NAME = SESSION
ROLE = "voice-assistant"
MODEL = "claude-haiku-5-5"
DEFAULT_EFFORT = "medium"


def service_entry(*, project: str, effort: str, autostart: bool) -> dict:
    """The ``services.custom`` entry that supervises the voice assistant."""
    return {
        "name": SERVICE_NAME,
        "project": project,
        "roles": ROLE,
        "posture": "bypass",
        "model": MODEL,
        "effort": effort,
        "resume": True,
        # Stateful (a follow-up can refer to the last answer), so compact
        # rather than clear when the context bloats.
        "context_policy": "compact",
        "autostart": bool(autostart),
        "restart": "on-failure",
        "healthcheck": {"kind": "tmux_session", "interval": 60},
    }


def _host_gate(verb: str, json_mode: bool) -> "int | None":
    from .tasks_cli import _host_context_ok

    if _host_context_ok():
        return None
    return _output_result(
        False, json_mode,
        f"agentwire voice {verb} writes ~/.agentwire/config.yaml (protected control "
        "plane) — human-only. Run it from your own terminal, not through an agent.",
    )


def _service():
    """The installed entry, or None."""
    from . import services as services_mod
    from .config import load_config

    return next((s for s in services_mod.registry(load_config()) if s.name == SERVICE_NAME), None)


def _not_installed(json_mode: bool) -> int:
    return _output_result(
        False, json_mode,
        f"{SERVICE_NAME} is not installed. Run `agentwire voice install` from your own terminal.",
    )


def cmd_voice_install(args) -> int:
    """Write the service entry and (unless --no-start) start it."""
    from . import services as services_mod
    from .config_cli import _load_raw, _save_raw

    json_mode = getattr(args, "json", False)
    refused = _host_gate("install", json_mode)
    if refused is not None:
        return refused

    entry = service_entry(project=services_mod._source_dir(), effort=args.effort,
                          autostart=args.autostart)
    raw = _load_raw()
    try:
        outcome = services_mod.upsert_service_entry(raw, entry)
    except ValueError as exc:
        return _output_result(False, json_mode, f"Refusing to write: {exc}. Fix the file by hand first.")
    if outcome != "unchanged":
        _save_raw(raw)

    started, start_msg = None, "not started (--no-start)"
    if not args.no_start:
        services_mod.set_disabled(SERVICE_NAME, False)
        started, start_msg = services_mod.start_service(_service())

    if json_mode:
        _output_json({"success": started is not False, "service": SERVICE_NAME,
                      "entry": outcome, "started": started, "result": start_msg,
                      "model": MODEL, "effort": entry["effort"], "autostart": entry["autostart"]})
        return 0 if started is not False else 1
    out = sys.stdout if started is not False else sys.stderr
    print(f"Service {SERVICE_NAME}: {outcome} in ~/.agentwire/config.yaml", file=out)
    print(f"  model:     {MODEL} (effort {entry['effort']})", file=out)
    print(f"  start:     {start_msg}", file=out)
    print("  autostart: " + ("on — boots with the portal" if entry["autostart"]
                             else "off — started on demand by `agentwire voice ask`"), file=out)
    if started:
        print('Try it: agentwire voice ask "which sessions are busy"', file=out)
    return 0 if started is not False else 1


def cmd_voice_uninstall(args) -> int:
    """Stop the service and remove its entry."""
    from . import services as services_mod
    from .config_cli import _load_raw, _save_raw

    json_mode = getattr(args, "json", False)
    refused = _host_gate("uninstall", json_mode)
    if refused is not None:
        return refused
    svc = _service()
    stop_msg = services_mod.stop_service(svc)[1] if svc else "not registered"
    raw = _load_raw()
    removed = services_mod.remove_service_entry(raw, SERVICE_NAME)
    if removed:
        _save_raw(raw)
    return _output_result(
        True, json_mode,
        f"Service {SERVICE_NAME}: {stop_msg}; "
        + ("entry removed from ~/.agentwire/config.yaml" if removed else "no entry to remove"),
        service=SERVICE_NAME, removed=removed, result=stop_msg,
    )


def _services_args(json_mode: bool) -> argparse.Namespace:
    return argparse.Namespace(name=SERVICE_NAME, json=json_mode, all=False)


def cmd_voice_start(args) -> int:
    from .system_cli import cmd_services_up

    if _service() is None:
        return _not_installed(args.json)
    return cmd_services_up(_services_args(args.json))


def cmd_voice_stop(args) -> int:
    from .system_cli import cmd_services_down

    if _service() is None:
        return _not_installed(args.json)
    return cmd_services_down(_services_args(args.json))


def cmd_voice_restart(args) -> int:
    from .restart_cli import cmd_restart

    if _service() is None:
        return _not_installed(args.json)
    return cmd_restart(argparse.Namespace(session=SERVICE_NAME, json=args.json, no_wait=False))


def cmd_voice_status(args) -> int:
    from . import services as services_mod
    from .core import load_session_metadata

    json_mode = args.json
    svc = _service()
    if svc is None:
        if json_mode:
            _output_json({"success": True, "installed": False, "service": SERVICE_NAME})
            return 0
        print(f"{SERVICE_NAME}: not installed (agentwire voice install)")
        return 1
    status = services_mod.service_status(svc)
    meta = load_session_metadata(SERVICE_NAME)
    payload = {
        "success": True, "installed": True, **status,
        # What the live session actually launched with, vs what the entry asks for.
        "model": meta.get("model") or svc.model,
        "effort": meta.get("effort") or svc.effort,
    }
    if json_mode:
        _output_json(payload)
        return 0
    mark = "[ok]" if status["healthy"] else "[..]"
    print(f"  {mark} {SERVICE_NAME}: {status['detail']}"
          + ("" if status["autostart"] else " (autostart off)"))
    print(f"      model {payload['model']}, effort {payload['effort']}")
    return 0 if status["healthy"] else 1


def cmd_voice_ask(args) -> int:
    """Send a request to the assistant, starting it first if needed.

    Returns once the text is delivered; the answer is spoken by the assistant.
    """
    from . import services as services_mod
    from .send_cli import cmd_send

    json_mode = args.json
    text = " ".join(args.text).strip()
    if not text:
        return _output_result(False, json_mode, 'Usage: agentwire voice ask "<request>"')
    svc = _service()
    if svc is None:
        return _not_installed(json_mode)
    # An explicit ask is intent: clear a `voice stop` so the watchdog manages it again.
    services_mod.set_disabled(SERVICE_NAME, False)
    ok, msg = services_mod.start_service(svc)
    if not ok:
        return _output_result(False, json_mode, f"Could not start {SERVICE_NAME}: {msg}")
    return cmd_send(argparse.Namespace(
        session=SERVICE_NAME, pane=None, prompt=[text], json=json_mode,
        wait_ready=True, verify=False, caller_session=None,
    ))


def register_voice_parser(subparsers) -> None:
    """Register ``agentwire voice``."""
    p = subparsers.add_parser(
        "voice",
        help=f"Portal voice assistant service ({SERVICE_NAME})",
        description=__doc__.split("\n\n")[0],
    )
    sub = p.add_subparsers(dest="voice_command")

    def _verb(name: str, func, help_text: str):
        v = sub.add_parser(name, help=help_text)
        v.add_argument("--json", action="store_true", help="Output JSON")
        v.set_defaults(func=func)
        return v

    ask = _verb("ask", cmd_voice_ask, "Send a request to the assistant (starts it if needed)")
    ask.add_argument("text", nargs="+", help="What to ask")
    _verb("status", cmd_voice_status, "Service health plus the model/effort it runs")
    _verb("start", cmd_voice_start, "Start the service")
    _verb("stop", cmd_voice_stop, "Stop the service (the next start is a fresh conversation)")
    _verb("restart", cmd_voice_restart, "Relaunch in place, same conversation")
    inst = _verb("install", cmd_voice_install,
                 "Add the service entry to config.yaml and start it (human-only)")
    inst.add_argument("--effort", choices=EFFORT_LEVELS, default=DEFAULT_EFFORT,
                      help=f"Reasoning effort (default {DEFAULT_EFFORT}; low is the speed fallback)")
    inst.add_argument("--autostart", action="store_true", help="Boot it with the portal")
    inst.add_argument("--no-start", action="store_true", help="Write the entry only")
    _verb("uninstall", cmd_voice_uninstall,
          "Stop the service and remove its entry (human-only)")
    p.set_defaults(func=lambda a: (p.print_help(), 1)[1])
