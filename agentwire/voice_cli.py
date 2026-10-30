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
import json
import re
import sys
from pathlib import Path

from .core import EFFORT_LEVELS, _output_json, _output_result
from .voice_hooks import SESSION

SERVICE_NAME = SESSION
ROLE = "voice-assistant"
MODEL = "claude-haiku-5-5"
DEFAULT_EFFORT = "medium"

#: What the portal's status chip can show; set by the session's speech hooks.
STATES = ("working", "done", "failed", "approval")

#: While an approval dialog is pending, the ENTIRE utterance is judged against
#: the dialog's intent by one short model call (``classify_reply``) — never a
#: first-word match. approve → the plain one-time Yes; deny → the dialog's
#: "No" (tell Claude what to do differently) with the full utterance as the
#: feedback; anything else, or a classifier failure, fails CLOSED: one spoken
#: "yes or no?" and the full text stays queued. Nothing the owner says is
#: dropped, and nothing is approved on a guess.
CLASSIFIER_MODEL = "claude-haiku-5-5"
CLASSIFIER_EFFORT = "low"
CLASSIFIER_TIMEOUT_S = 10
REPLY_VERDICTS = ("approve", "approve_and_more", "deny", "other")

#: Which dialog was already met with "say yes or no", so it is spoken once.
REMINDED_FILE = Path.home() / ".agentwire" / "voice" / "approval-reminded"

#: Sender name for the owner's queued requests: the assistant's ack hook treats
#: ``[MSG from owner · request]`` as a request from the owner.
OWNER_SENDER = "owner"


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


def classify_reply(intent: str, utterance: str, run=None) -> str:
    """One of ``REPLY_VERDICTS`` for *utterance* as a reply to *intent*.

    One tool-less ``claude -p`` call on Haiku at low effort (~2 s measured),
    bounded by ``CLASSIFIER_TIMEOUT_S``. Fails closed: a timeout, an error,
    or any reply that is not exactly a verdict is ``"other"``.
    """
    import subprocess

    prompt = (
        "A spoken reply must be matched to a pending approval.\n"
        f"Pending action: {intent}\n"
        f'Owner\'s entire utterance: "{utterance}"\n'
        "Answer with exactly one word:\n"
        "approve - they clearly approve, and say nothing more\n"
        "approve_and_more - they clearly approve AND say or ask something further\n"
        "deny - they reject it, or want something different done instead\n"
        "other - anything else, unclear, or unrelated\n"
        "If in any doubt, answer other."
    )
    cmd = ["claude", "-p", prompt, "--model", CLASSIFIER_MODEL, "--effort", CLASSIFIER_EFFORT,
           "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--disable-slash-commands"]
    try:
        result = (run or subprocess.run)(cmd, capture_output=True, text=True,
                                         stdin=subprocess.DEVNULL, timeout=CLASSIFIER_TIMEOUT_S)
    except Exception:  # noqa: BLE001  # timeout / missing binary: fail closed
        return "other"
    verdict = (result.stdout or "").strip().lower().strip(".")
    return verdict if result.returncode == 0 and verdict in REPLY_VERDICTS else "other"


def _pending_intent(info) -> str:
    """The announced approval's intent; the dialog's own text as fallback."""
    from .voice_hooks import APPROVAL_FILE

    try:
        intent = json.loads(APPROVAL_FILE.read_text()).get("intent")
    except (OSError, ValueError, AttributeError):
        intent = None
    return intent or info.body or info.summary or info.question


def approval_keys(options: list[dict], answer: str) -> list[str]:
    """Keys that pick *answer* in a numbered dialog.

    Yes is the plain, one-time "Yes" only — never a "Yes, and don't ask
    again" / allow-all option, which would widen a grant by voice. No is the
    "No…" option, or Escape when the dialog has none. Empty = no safe pick.
    """
    def label(opt: dict) -> str:
        return re.sub(r"[^a-z ]+", "", opt.get("label", "").lower()).strip()

    if answer == "yes":
        return [str(o["number"]) for o in options if label(o) == "yes"][:1]
    noes = [o for o in options if label(o).startswith("no")]
    noes.sort(key=lambda o: "tell claude" not in label(o))  # prefer "No, and tell Claude…"
    return [str(noes[0]["number"])] if noes else ["Escape"]


def _queue_request(text: str) -> None:
    """Hold *text* as the owner's next request. The msg drain delivers it only
    once no dialog is live and the input box is empty — i.e. after the dialog
    clears — so it is never pasted into the dialog and never lost."""
    from . import inbox

    inbox.enqueue(SERVICE_NAME, text, kind="request", sender=OWNER_SENDER)


def _remind_once(dialog_hash: str) -> None:
    from .voice_hooks import speak

    try:
        if REMINDED_FILE.read_text().strip() == dialog_hash:
            return
    except OSError:
        pass
    try:
        REMINDED_FILE.parent.mkdir(parents=True, exist_ok=True)
        REMINDED_FILE.write_text(dialog_hash)
    except OSError:
        pass
    speak("An approval is waiting. Say yes or no.", kind="approval")


def _answer_pending_approval(text: str, json_mode: bool) -> "int | None":
    """If the assistant is blocked on an approval dialog, *text* answers it.

    None when no dialog is live (the caller delivers *text* as a request).
    Keys go through ``prompt_router.answer`` — compare-and-send on the
    dialog's content hash — so a dialog answered meanwhile in the portal is
    never answered twice. The full utterance is queued whenever it carries
    more than the bare answer; it is delivered once the dialog clears.
    """
    from . import prompt_router
    from .voice_hooks import speak

    info = prompt_router.detect_prompt(prompt_router.capture(SERVICE_NAME))
    if info is None or info.kind != "permission":
        return None
    verdict = classify_reply(_pending_intent(info), text)
    if verdict == "other":
        _queue_request(text)
        _remind_once(info.content_hash())
        return _output_result(False, json_mode,
                              "An approval is pending — say yes or no. Your request is queued.",
                              pending_approval=True, verdict=verdict, queued=True)
    answer = "no" if verdict == "deny" else "yes"
    keys = approval_keys(info.options, answer)
    if not keys:
        # No plain one-time Yes on this dialog: never pick a broader grant by voice.
        speak("I can't approve that one by voice. It's waiting in the portal.", kind="approval")
        _queue_request(text)
        return _output_result(False, json_mode, "No plain one-time Yes option; answer it in the portal.",
                              pending_approval=True, verdict=verdict, queued=True)
    ok, detail = prompt_router.answer(SERVICE_NAME, 0, info.content_hash(), keys)
    # deny: the full utterance is the "what to do differently" feedback.
    # approve_and_more: the full utterance is the next request.
    queued = verdict in ("deny", "approve_and_more")
    if queued:
        _queue_request(text)
    return _output_result(ok, json_mode, f"Approval {answer}: {detail}",
                          answered=answer, verdict=verdict, queued=queued)


def cmd_voice_ask(args) -> int:
    """Send a request to the assistant, starting it first if needed.

    While an approval dialog is live, the request IS the answer: "yes" / "no"
    picks the option, anything else is refused with a spoken reminder.
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
    if msg == "already running":
        answered = _answer_pending_approval(text, json_mode)
        if answered is not None:
            return answered
    return cmd_send(argparse.Namespace(
        session=SERVICE_NAME, pane=None, prompt=[text], json=json_mode,
        wait_ready=True, verify=False, caller_session=None,
    ))


def cmd_voice_state(args) -> int:
    """Push the assistant's state to the portal's status chip (best-effort).

    Called by the session's speech hooks; a portal that is down costs a stale
    chip, never an error in the hook.
    """
    from .core import _get_portal_url, portal_request

    try:
        r = portal_request("POST", f"{_get_portal_url()}/api/voice/state",
                           json={"state": args.state}, timeout=3)
        ok = r.ok
    except Exception:  # noqa: BLE001  # portal down: nothing to update
        ok = False
    return _output_result(True, args.json, f"state {args.state}" + ("" if ok else " (portal unreachable)"),
                          state=args.state, delivered=ok)


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
    st = _verb("state", cmd_voice_state, "Set the portal status chip (used by the speech hooks)")
    st.add_argument("state", choices=STATES)
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
