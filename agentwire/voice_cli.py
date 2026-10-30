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
import os
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


def classify_reply(intent: str, utterance: str, run=None) -> "tuple[str, str | None]":
    """``(verdict, scope)`` for *utterance* as a reply to *intent*.

    *verdict* is one of ``REPLY_VERDICTS``; *scope* (``hour`` / ``today`` /
    ``session``) only on an approval the owner meant to keep applying ("yes,
    approve those for today") — the model supplies the scope, never what it
    covers (that comes from the hook's record; see ``voice_grants``).

    One tool-less ``claude -p`` call on Haiku at low effort (~2.5 s measured),
    bounded by ``CLASSIFIER_TIMEOUT_S``. It answers three separate questions
    (yes/no/unclear, also asks for something, how long) and the verdict is
    derived from them in code: asked as one combined label, Haiku dropped the
    "also asks" half 2 times in 3 in the #1198 eval. Fails closed: a timeout,
    an error, or any reply off the exact format is ``("other", None)``.
    """
    import subprocess

    prompt = (
        "A spoken reply to a pending approval. Answer three questions about it.\n"
        f"Pending action: {intent}\n"
        f'Owner\'s entire utterance: "{utterance}"\n\n'
        "answer: is it a yes, a no, or unclear? (asking for a different action "
        "instead counts as no; a question or anything off-topic is unclear)\n"
        "also_asks: besides yes or no, does the utterance ask for ANY other action "
        "to be done, now or next? Saying how long a yes lasts is not an action.\n"
        "lasts: how long they say the yes applies: once (nothing said), hour, today "
        "(also 'always', 'all of these') or session.\n\n"
        "Reply in exactly this format and nothing else:\n"
        "answer: yes|no|unclear\n"
        "also_asks: yes|no\n"
        "lasts: once|hour|today|session"
    )
    cmd = ["claude", "-p", prompt, "--model", CLASSIFIER_MODEL, "--effort", CLASSIFIER_EFFORT,
           "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--disable-slash-commands"]
    try:
        result = (run or subprocess.run)(cmd, capture_output=True, text=True,
                                         stdin=subprocess.DEVNULL, timeout=CLASSIFIER_TIMEOUT_S)
    except Exception:  # noqa: BLE001  # timeout / missing binary: fail closed
        return "other", None
    if result.returncode != 0:
        return "other", None
    return parse_reply_fields(result.stdout or "")


def parse_reply_fields(text: str) -> "tuple[str, str | None]":
    """The classifier's three fields -> ``(verdict, scope)``. Deterministic and
    strict: a missing or unknown field is ``("other", None)`` (fail closed)."""
    from .voice_grants import SCOPES

    fields = {}
    for line in text.strip().lower().splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip().strip(".").strip()
    answer, also, lasts = fields.get("answer"), fields.get("also_asks"), fields.get("lasts")
    if answer not in ("yes", "no", "unclear") or also not in ("yes", "no") \
            or lasts not in ("once", *SCOPES):
        return "other", None
    if answer == "unclear":
        return "other", None
    if answer == "no":
        return "deny", None
    return ("approve_and_more" if also == "yes" else "approve"), (None if lasts == "once" else lasts)


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


def _from_an_agent() -> bool:
    """Is this ``voice ask`` running inside a Claude Code session?

    Claude Code sets ``CLAUDECODE=1`` for every tool subprocess (and hook).
    Answering an approval, or minting a standing grant, is the OWNER's to do
    (portal button, terminal, Hammerspoon) — never another agent's, or any
    session could approve the assistant's destructive dialogs by running
    ``agentwire voice ask "yes for today"``.
    """
    return os.environ.get("CLAUDECODE") == "1"


def _answer_pending_approval(text: str, json_mode: bool) -> "int | None":
    """If the assistant is blocked on an approval dialog, *text* answers it.

    None when no dialog is live (the caller delivers *text* as a request).
    Keys go through ``prompt_router.answer`` — compare-and-send on the
    dialog's content hash — so a dialog answered meanwhile in the portal is
    never answered twice. The full utterance is queued whenever it carries
    more than the bare answer; it is delivered once the dialog clears. An
    approval with a scope also records a standing grant for the dialog's rule
    (when the hook recorded it grantable).
    """
    from . import prompt_router, voice_grants
    from .voice_hooks import speak

    info = prompt_router.detect_prompt(prompt_router.capture(SERVICE_NAME))
    if info is None or info.kind != "permission":
        return None
    if _from_an_agent():
        return _output_result(False, json_mode,
                              "An approval is pending on agentwire-voice; only the owner answers it "
                              "(portal, terminal or Hammerspoon), not another agent.",
                              pending_approval=True)
    verdict, scope = classify_reply(_pending_intent(info), text)
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
    grant = None
    if scope:
        pending = voice_grants.pending_ask()
        if pending and pending.get("grantable") and pending.get("rule_id"):
            grant = voice_grants.add(pending["rule_id"], scope)
            speak(f"Approved. Standing OK {voice_grants.SCOPE_PHRASES[scope]}.", kind="approval")
        else:
            speak("Approved this once. That kind can't be a standing OK.", kind="approval")
    ok, detail = prompt_router.answer(SERVICE_NAME, 0, info.content_hash(), keys)
    # deny: the full utterance is the "what to do differently" feedback.
    # approve_and_more: the full utterance is the next request.
    queued = verdict in ("deny", "approve_and_more")
    if queued:
        _queue_request(text)
    return _output_result(ok, json_mode, f"Approval {answer}: {detail}",
                          answered=answer, verdict=verdict, queued=queued,
                          grant=grant and grant["rule_id"])


def cmd_voice_approval(args) -> int:
    """Called by the Notification hook when the assistant hits an approval.

    A standing grant covering the dialog's rule (from the hook's own record,
    never the pane text) presses the plain one-time Yes and says "Approved,
    standing OK." Otherwise the approval request (*--announce*) is spoken and
    the chip shows it. Grants are only ever APPLIED here, never created.
    """
    from . import prompt_router, voice_grants
    from .voice_hooks import signal, speak

    info = prompt_router.detect_prompt(prompt_router.capture(SERVICE_NAME))
    pending = voice_grants.pending_ask()
    grant = (voice_grants.covering(pending.get("rule_id"))
             if info is not None and info.kind == "permission"
             and pending and pending.get("grantable") else None)
    keys = approval_keys(info.options, "yes") if grant else []
    if grant and keys:
        ok, detail = prompt_router.answer(SERVICE_NAME, 0, info.content_hash(), keys)
        if ok:
            voice_grants.log("used", rule_id=grant["rule_id"], scope=grant["scope"])
            speak("Approved, standing OK.", kind="approval")
            signal("working")
            return _output_result(True, args.json, f"Standing grant {grant['rule_id']}: {detail}",
                                  granted=grant["rule_id"])
    speak(args.announce, kind="approval", display=args.display)
    signal("approval")
    return _output_result(True, args.json, "Approval announced", granted=None)


def cmd_voice_grants(args) -> int:
    """List the standing grants, or clear them ("stop auto-approving")."""
    from . import voice_grants

    if args.clear:
        removed = voice_grants.clear(args.rule)
        _push_grant_count()
        return _output_result(True, args.json, f"Cleared {removed} standing grant(s).", cleared=removed)
    grants = voice_grants.active()
    if args.json:
        _output_json({"success": True, "grants": grants})
        return 0
    if not grants:
        print("No standing grants.")
    for g in grants:
        print(f"  {g['rule_id']}: {voice_grants.SCOPE_PHRASES.get(g['scope'], g['scope'])}")
    return 0


def _push_grant_count() -> None:
    """Refresh the portal chip's standing-grant badge (best-effort)."""
    from . import voice_grants
    from .core import _get_portal_url, portal_request

    try:
        portal_request("POST", f"{_get_portal_url()}/api/voice/state",
                       json={"grants": len(voice_grants.active())}, timeout=3)
    except Exception:  # noqa: BLE001  # portal down: the badge catches up on the next state push
        pass


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
    chip, never an error in the hook. Carries the standing-grant count so the
    chip shows when auto-approval is on.
    """
    from . import voice_grants
    from .core import _get_portal_url, portal_request
    try:
        r = portal_request("POST", f"{_get_portal_url()}/api/voice/state",
                           json={"state": args.state, "grants": len(voice_grants.active())},
                           timeout=3)
        ok = r.ok
    except Exception:  # noqa: BLE001  # portal down: nothing to update
        ok = False
    return _output_result(True, args.json, f"state {args.state}" + ("" if ok else " (portal unreachable)"),
                          state=args.state, delivered=ok)


def cmd_voice_digest(args) -> int:
    """Print the assistant's system digest (sessions with live state, projects,
    scheduler, notable config). Its SessionStart hook injects the same text."""
    from .voice_digest import build_assistant_digest

    text = build_assistant_digest()
    if args.json:
        _output_json({"success": True, "digest": text})
    else:
        print(text)
    return 0


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
    _verb("digest", cmd_voice_digest,
          "The assistant's system digest: sessions (busy/idle/blocked), projects, scheduler, config")
    st = _verb("state", cmd_voice_state, "Set the portal status chip (used by the speech hooks)")
    st.add_argument("state", choices=STATES)
    ap = _verb("approval", cmd_voice_approval,
               "Apply a standing grant to the live approval, or announce it (used by the speech hooks)")
    ap.add_argument("--announce", required=True, help="What to say when no grant covers it")
    ap.add_argument("--display", help="On-screen card for the announcement")
    gr = _verb("grants", cmd_voice_grants, "List standing approvals, or --clear them")
    gr.add_argument("--clear", action="store_true", help="Remove standing grants (all, or --rule)")
    gr.add_argument("--rule", help="With --clear: only this rule id")
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
