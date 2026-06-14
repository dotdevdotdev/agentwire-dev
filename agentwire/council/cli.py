"""CLI handlers for ``agentwire council ...``.

Argparse wiring lives in ``agentwire/__main__.py``; handlers receive an
``argparse.Namespace`` and return an exit code. Every handler supports
``--json`` so the MCP layer can shell out and parse structured output.

Subcommands:

- ``start``   — spin up the orchestrator + lens soul sessions (a *sitting*)
- ``stop``    — kill the sitting's sessions, clear state (history kept)
- ``status``  — sitting + per-session liveness + open prompts
- ``ask``     — fan a prompt out to every soul (creates the inbox first)
- ``collect`` — block until every soul has filed take/ack/pass, or timeout
- ``reply``   — file a soul's reply (souls run this via Bash)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

from agentwire.council import inbox, state

# --- output helpers -----------------------------------------------------------


def _emit(args, payload: dict[str, Any], human: str = "", exit_code: int = 0) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    elif human:
        print(human)
    return exit_code


def _emit_error(args, message: str, exit_code: int = 1) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"success": False, "error": message}, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)
    return exit_code


# --- side-effecting helpers (monkeypatched in tests) ---------------------------


def list_live_sessions() -> set[str]:
    """Names of all running agentwire sessions."""
    result = subprocess.run(
        ["agentwire", "list", "--sessions", "--json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return set()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    sessions = data.get("sessions", []) if isinstance(data, dict) else []
    return {s.get("name", "") for s in sessions if isinstance(s, dict)} - {""}


def create_session(name: str, roles: list[str], session_type: str, model: str | None) -> None:
    """Create one council session via ``agentwire new`` in the shared workspace.

    Raises ``RuntimeError`` on failure.
    """
    cmd = [
        "agentwire", "new",
        "-s", name,
        "-p", str(state.WORKSPACE_DIR),
        "--roles", ",".join(roles),
        "--type", session_type,
        "--allow-shared-dir",
        "--json",
    ]
    if model:
        cmd += ["--model", model]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"agentwire new failed for {name} (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )


def kill_session(name: str) -> bool:
    """Kill a session; True if the command succeeded."""
    result = subprocess.run(
        ["agentwire", "kill", "-s", name, "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0


def send_to_session(session: str, message: str) -> None:
    """Inject a message into a session's pane 0."""
    from agentwire import pane_manager

    pane_manager.send_to_target(f"{session}.0", message, enter=True)


def capture_session(session: str, lines: int = 60) -> str:
    from agentwire import pane_manager

    return pane_manager.capture_pane(session, 0, lines=lines)


def send_verified(session: str, message: str, marker: str, retries: int = 1) -> bool:
    """Send a message and verify the ``marker`` landed (see session_ready)."""
    from agentwire import session_ready

    return session_ready.send_verified(session, message, marker, retries=retries)


def wait_ready(session: str, timeout: float = 45.0) -> bool:
    """Wait until a council session's agent is ready for input."""
    from agentwire.session_ready import wait_for_session_ready

    return wait_for_session_ready(session, timeout=timeout)


def current_session() -> str | None:
    from agentwire import pane_manager

    return pane_manager.get_current_session()


# --- workspace ------------------------------------------------------------------


def _write_workspace(session_type: str) -> None:
    """Workspace dir all council sessions run in.

    ``parent: agentwire-council`` routes any ``agentwire notify`` from a soul
    to the orchestrator for free.
    """
    state.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (state.WORKSPACE_DIR / ".agentwire.yml").write_text(
        f"type: {session_type}\nparent: {state.ORCHESTRATOR_SESSION}\n"
    )


# --- handlers -------------------------------------------------------------------


def cmd_council_start(args) -> int:
    roster_arg = getattr(args, "roster", None)
    roster = (
        [r.strip() for r in roster_arg.split(",") if r.strip()]
        if roster_arg
        else list(state.DEFAULT_ROSTER)
    )
    for lens in roster:
        if not state.valid_lens(lens):
            return _emit_error(args, f"invalid lens name: {lens!r}")

    sitting = state.read_sitting()
    if sitting is not None:
        live = list_live_sessions()
        still_up = [s for s in (*sitting.sessions.values(), sitting.orchestrator) if s in live]
        if still_up and not getattr(args, "force", False):
            return _emit_error(
                args,
                f"a council sitting is already active ({', '.join(still_up)}) — "
                "use 'agentwire council stop' or --force",
            )
        # Stale or --force: tear down what's left before restarting.
        for s in still_up:
            kill_session(s)
        state.clear_sitting()

    session_type = getattr(args, "type", None) or "claude-bypass"
    model = getattr(args, "model", None)
    _write_workspace(session_type)

    sessions: dict[str, str] = {}
    failed: list[dict] = []

    try:
        create_session(
            state.ORCHESTRATOR_SESSION, ["council-orchestrator"], session_type, model
        )
    except RuntimeError as e:
        return _emit_error(args, f"failed to start orchestrator: {e}")

    for lens in roster:
        name = state.session_for(lens)
        try:
            create_session(name, ["council-member", f"council-{lens}"], session_type, model)
            sessions[lens] = name
        except RuntimeError as e:
            failed.append({"soul": lens, "error": str(e)})

    # Sessions boot concurrently; wait for each to be input-ready so an
    # immediate `council ask` doesn't paste into a half-booted pane.
    not_ready = [
        s
        for s in (state.ORCHESTRATOR_SESSION, *sessions.values())
        if not wait_ready(s)
    ]

    state.write_sitting(
        state.Sitting(
            orchestrator=state.ORCHESTRATOR_SESSION,
            roster=[lens for lens in roster if lens in sessions],
            sessions=sessions,
            started_at=state.now_iso(),
            session_type=session_type,
        )
    )

    payload = {
        "success": not failed and not not_ready,
        "orchestrator": state.ORCHESTRATOR_SESSION,
        "sessions": sessions,
        "failed": failed,
        "not_ready": not_ready,
    }
    human = (
        f"Council sitting started: {state.ORCHESTRATOR_SESSION} + "
        f"{len(sessions)} souls ({', '.join(sessions)})"
    )
    if failed:
        human += f"\nfailed: {', '.join(f['soul'] for f in failed)}"
    if not_ready:
        human += f"\nnot ready after wait: {', '.join(not_ready)}"
    return _emit(args, payload, human, exit_code=0 if payload["success"] else 1)


def cmd_council_stop(args) -> int:
    sitting = state.read_sitting()
    if sitting is None:
        return _emit_error(args, "no active council sitting")

    live = list_live_sessions()
    killed: list[str] = []
    not_running: list[str] = []
    for name in (*sitting.sessions.values(), sitting.orchestrator):
        if name in live and kill_session(name):
            killed.append(name)
        else:
            not_running.append(name)
    state.clear_sitting()

    payload = {"success": True, "killed": killed, "not_running": not_running}
    return _emit(
        args,
        payload,
        f"Council sitting stopped ({len(killed)} sessions killed). Prompt history kept.",
    )


def cmd_council_status(args) -> int:
    sitting = state.read_sitting()
    if sitting is None:
        return _emit(
            args,
            {"success": True, "running": False},
            "No active council sitting.",
        )

    live = list_live_sessions()
    souls = [
        {"soul": lens, "session": name, "alive": name in live}
        for lens, name in sitting.sessions.items()
    ]
    prompts = []
    for pid in range(1, sitting.next_prompt_id):
        pending = inbox.pending_souls(pid, sitting.roster)
        prompts.append(
            {
                "id": pid,
                "complete": not pending,
                "replied": [s for s in sitting.roster if s not in pending],
                "pending": pending,
            }
        )

    payload = {
        "success": True,
        "running": True,
        "orchestrator": sitting.orchestrator,
        "orchestrator_alive": sitting.orchestrator in live,
        "started_at": sitting.started_at,
        "souls": souls,
        "prompts": prompts,
    }
    lines = [
        f"Council sitting (started {sitting.started_at})",
        f"  orchestrator: {sitting.orchestrator} "
        f"[{'alive' if payload['orchestrator_alive'] else 'DOWN'}]",
    ]
    for s in souls:
        lines.append(f"  {s['soul']}: {s['session']} [{'alive' if s['alive'] else 'DOWN'}]")
    for p in prompts:
        status = "complete" if p["complete"] else f"pending: {', '.join(p['pending'])}"
        lines.append(f"  prompt #{p['id']}: {status}")
    return _emit(args, payload, "\n".join(lines))


def _prompt_text_from(args) -> str | None:
    """Prompt/reply body from positional/--text, --file, or stdin."""
    text = getattr(args, "text", None)
    if text:
        return text
    file_arg = getattr(args, "file", None)
    if file_arg:
        try:
            return open(file_arg).read()
        except OSError:
            return None
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return None


def cmd_council_ask(args) -> int:
    sitting = state.read_sitting()
    if sitting is None:
        return _emit_error(args, "no active council sitting — run 'agentwire council start'")

    prompt_text = getattr(args, "prompt", None) or _prompt_text_from(args)
    if not prompt_text or not prompt_text.strip():
        return _emit_error(args, "no prompt text (positional, --file, or stdin)")
    prompt_text = prompt_text.strip()

    prompt_id = state.allocate_prompt_id()
    inbox.create_prompt(prompt_id, prompt_text, sitting.roster)  # inbox before any send

    message = (
        f"[COUNCIL PROMPT #{prompt_id}]\n"
        f"{prompt_text}\n\n"
        f"Reply through your lens with exactly one of:\n"
        f'  agentwire council reply --prompt {prompt_id} --take --text "<your take>"\n'
        f"  agentwire council reply --prompt {prompt_id} --ack\n"
        f"  agentwire council reply --prompt {prompt_id} --pass"
    )

    marker = f"[COUNCIL PROMPT #{prompt_id}]"
    live = list_live_sessions()
    sent_to: list[str] = []
    failed: list[dict] = []
    for lens, session in sitting.sessions.items():
        if session not in live:
            failed.append({"soul": lens, "error": "session not running"})
            continue
        try:
            if send_verified(session, message, marker):
                sent_to.append(lens)
            else:
                failed.append({"soul": lens, "error": "delivery not confirmed in pane"})
        except Exception as e:  # tmux failures shouldn't kill the whole fan-out
            failed.append({"soul": lens, "error": str(e)})

    payload = {
        "success": bool(sent_to),
        "prompt_id": prompt_id,
        "sent_to": sent_to,
        "failed": failed,
    }
    human = f"Prompt #{prompt_id} fanned out to {len(sent_to)} souls."
    if failed:
        human += f" Failed: {', '.join(f['soul'] for f in failed)}"
    return _emit(args, payload, human, exit_code=0 if sent_to else 1)


def cmd_council_collect(args) -> int:
    sitting = state.read_sitting()
    if sitting is None:
        return _emit_error(args, "no active council sitting")

    prompt_id = getattr(args, "prompt", None) or state.latest_prompt_id()
    if not prompt_id:
        return _emit_error(args, "no prompts asked yet")

    result = inbox.collect(
        prompt_id,
        sitting.roster,
        timeout=float(getattr(args, "timeout", 120)),
        wait=not getattr(args, "no_wait", False),
    )
    result["success"] = True

    status = (
        "complete" if result["complete"] else f"pending: {', '.join(result['pending'])}"
    )
    lines = [f"Prompt #{prompt_id}: {status}"]
    for r in result["replies"]:
        lines.append(f"\n--- {r['soul']} ({r['kind']}) ---\n{r['text']}")
    return _emit(args, result, "\n".join(lines))


def cmd_council_reply(args) -> int:
    kinds = [k for k in inbox.KINDS if getattr(args, k.replace("-", "_"), False)]
    if len(kinds) != 1:
        return _emit_error(args, "specify exactly one of --take / --ack / --pass")
    kind = kinds[0]

    sitting = state.read_sitting()
    if sitting is None:
        return _emit_error(args, "no active council sitting")

    prompt_id = getattr(args, "prompt", None) or state.latest_prompt_id()
    if not prompt_id:
        return _emit_error(args, "no prompts asked yet")

    soul = getattr(args, "soul", None)
    if not soul:
        session = current_session()
        if session and session.startswith("council-"):
            soul = session[len("council-") :]
    if not soul:
        return _emit_error(args, "could not infer soul — pass --soul <lens>")

    # Only --take falls back to stdin; ack/pass must not block on a pipe.
    text = (
        (_prompt_text_from(args) if kind == "take" else getattr(args, "text", None)) or ""
    )
    if kind == "take" and not text.strip():
        return _emit_error(args, "--take requires text (--text, --file, or stdin)")

    try:
        path, is_followup = inbox.write_reply(prompt_id, soul, kind, text)
    except (ValueError, FileNotFoundError) as e:
        return _emit_error(args, str(e))

    nudged = None
    if is_followup:
        # Deterministic nudge — doesn't rely on the soul remembering to notify.
        # Verified like the fan-out; the follow-up is on disk regardless, so a
        # failed nudge only delays relay until the next collect.
        try:
            nudged = send_verified(
                sitting.orchestrator,
                f"[COUNCIL FOLLOW-UP] {soul} filed a follow-up on prompt "
                f"#{prompt_id} — run council_collect({prompt_id}) and relay it.",
                "[COUNCIL FOLLOW-UP]",
            )
        except Exception:
            nudged = False

    payload = {
        "success": True,
        "prompt_id": prompt_id,
        "soul": soul,
        "kind": kind,
        "followup": is_followup,
        "nudged": nudged,
        "path": str(path),
    }
    return _emit(
        args,
        payload,
        f"Filed {'follow-up ' if is_followup else ''}{kind} from {soul} on prompt #{prompt_id}.",
    )
