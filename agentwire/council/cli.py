"""CLI handlers for ``agentwire council ...``.

Argparse wiring lives in ``agentwire/__main__.py``; handlers receive an
``argparse.Namespace`` and return an exit code. Every handler supports
``--json`` so the MCP layer can shell out and parse structured output.

Sittings are **namespaced by ``<name>``** (``agentwire/council/state.py``), so
independent councils run concurrently. Targeting, every command:

    explicit ``--name`` → else cwd-repo-slug *if it matches a live sitting*
    → else the sole live sitting → else error + list candidates.

The system auto-picks only when unambiguous; it refuses (never guesses by
recency) otherwise, and **every command echoes which sitting it acted on**.

Subcommands:

- ``start``   — spin up the orchestrator + lens soul sessions (a *sitting*)
- ``stop``    — kill the sitting's sessions, clear state (history kept)
- ``status``  — sitting + per-session liveness + open prompts
- ``ask``     — fan a prompt out to every soul (creates the inbox first)
- ``collect`` — block until every soul has filed take/ack/pass, or timeout
- ``reply``   — file a soul's reply (souls run this via Bash)
- ``list``    — every live/known sitting, oldest-first
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from agentwire.council import inbox, state

# --- output helpers -----------------------------------------------------------


def _emit(
    args,
    payload: dict[str, Any],
    human: str = "",
    exit_code: int = 0,
    council: str | None = None,
    echo_suffix: str = "",
) -> int:
    """Emit JSON or human output, echoing which sitting was acted on.

    ``council`` is the resolved sitting name — non-negotiable to surface:
    targeting you can't see is targeting you can't trust.
    """
    if council is not None:
        payload = {"council": council, **payload}
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    elif human:
        if council is not None:
            suffix = f" {echo_suffix}" if echo_suffix else ""
            print(f"→ council '{council}'{suffix}")
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


def create_session(
    name: str, roles: list[str], session_type: str, model: str | None, cwd: str
) -> None:
    """Create one council session via ``agentwire new`` in the sitting's
    workspace. Raises ``RuntimeError`` on failure.
    """
    cmd = [
        "agentwire", "new",
        "-s", name,
        "-p", cwd,
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


# --- targeting / resolution -----------------------------------------------------


def live_sitting_names(live: set[str]) -> list[str]:
    """Names whose orchestrator session is alive in ``live``."""
    out = []
    for name in state.list_sittings():
        sitting = state.read_sitting(name)
        if sitting is not None and sitting.orchestrator in live:
            out.append(name)
    return out


def resolve_name(args) -> tuple[str | None, str | None]:
    """Resolve which sitting an operate-on command targets.

    Returns ``(name, error)``; exactly one is non-None. Order: explicit
    ``--name`` → cwd-slug if it matches a live sitting → the sole live sitting
    → else error (0 = none here; N = ambiguous, list the names). Never guesses
    by recency. Liveness of an explicit ``--name`` is the caller's check.
    """
    explicit = getattr(args, "name", None)
    if explicit:
        if not state.valid_name(explicit):
            return None, f"invalid council name: {explicit!r}"
        return explicit, None

    live_names = live_sitting_names(list_live_sessions())
    cwd_slug = state.default_name()
    if cwd_slug in live_names:
        return cwd_slug, None
    if len(live_names) == 1:
        return live_names[0], None
    if not live_names:
        return None, "no council for this repo — run 'agentwire council start'"
    return None, (
        "multiple councils live — disambiguate with --name: "
        + ", ".join(sorted(live_names))
    )


# --- first-run legacy zombie sweep ----------------------------------------------


def _sweep_legacy_once() -> list[str]:
    """Tear down the pre-namespace global layout, exactly once.

    Pre-namespace, the council was a singleton: orchestrator ``agentwire-council``,
    lens sessions ``council-<lens>``, a global ``~/.agentwire/council/sitting.json``
    + ``workspace/``. After upgrade those panes keep burning tokens invisibly —
    this exact zombie state has been hit live. Kill them and remove the orphaned
    global state; ``prompts/`` history is kept. Marker-guarded so it runs once.
    """
    marker = state.COUNCIL_ROOT / ".namespaced"
    if marker.exists():
        return []

    legacy_sitting = state.COUNCIL_ROOT / "sitting.json"
    targets: set[str] = {"agentwire-council"}
    targets.update(f"council-{lens}" for lens in state.DEFAULT_ROSTER)
    if legacy_sitting.exists():
        try:
            data = json.loads(legacy_sitting.read_text())
            if isinstance(data, dict):
                targets.update(
                    v for v in data.get("sessions", {}).values() if isinstance(v, str)
                )
                orch = data.get("orchestrator")
                if isinstance(orch, str):
                    targets.add(orch)
        except (json.JSONDecodeError, OSError):
            pass

    live = list_live_sessions()
    killed = [t for t in sorted(targets) if t and t in live and kill_session(t)]

    for orphan in (legacy_sitting, state.COUNCIL_ROOT / "workspace" / ".agentwire.yml"):
        try:
            orphan.unlink()
        except (FileNotFoundError, OSError):
            pass

    state.COUNCIL_ROOT.mkdir(parents=True, exist_ok=True)
    marker.write_text(state.now_iso())
    return killed


# --- workspace ------------------------------------------------------------------


def _write_workspace(name: str, session_type: str) -> None:
    """Workspace dir the sitting's sessions run in.

    ``parent: agentwire-council-<name>`` routes any ``agentwire notify-parent`` from a
    soul to the sitting's own orchestrator for free.
    """
    ws = state.workspace_dir(name)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".agentwire.yml").write_text(
        f"type: {session_type}\nparent: {state.orchestrator_for(name)}\n"
    )


# --- handlers -------------------------------------------------------------------


def cmd_council_start(args) -> int:
    _sweep_legacy_once()

    explicit = getattr(args, "name", None)
    name = explicit or state.default_name()
    if not state.valid_name(name):
        return _emit_error(args, f"invalid council name: {name!r}")

    roster_arg = getattr(args, "roster", None)
    roster = (
        [r.strip() for r in roster_arg.split(",") if r.strip()]
        if roster_arg
        else list(state.DEFAULT_ROSTER)
    )
    for lens in roster:
        if not state.valid_lens(lens):
            return _emit_error(args, f"invalid lens name: {lens!r}")

    orchestrator = state.orchestrator_for(name)

    existing = state.read_sitting(name)
    if existing is not None:
        live = list_live_sessions()
        still_up = [
            s for s in (*existing.sessions.values(), existing.orchestrator) if s in live
        ]
        if still_up and not getattr(args, "force", False):
            return _emit_error(
                args,
                f"council '{name}' is already live ({', '.join(still_up)}) — "
                "use 'agentwire council stop' or --force",
            )
        # Stale or --force: tear down what's left before restarting.
        for s in still_up:
            kill_session(s)
        state.clear_sitting(name)

    session_type = getattr(args, "type", None) or "claude-bypass"
    model = getattr(args, "model", None)
    _write_workspace(name, session_type)
    workspace = str(state.workspace_dir(name))

    # Advisory: name the live sittings when seating beyond the first.
    other_live = live_sitting_names(list_live_sessions())
    advisory = ""
    if other_live:
        all_live = sorted({*other_live, name})
        advisory = f"{len(all_live)} councils live: {', '.join(all_live)}"

    sessions: dict[str, str] = {}
    failed: list[dict] = []

    try:
        create_session(orchestrator, ["council-orchestrator"], session_type, model, workspace)
    except RuntimeError as e:
        return _emit_error(args, f"failed to start orchestrator: {e}")

    for lens in roster:
        session = state.session_for(name, lens)
        try:
            create_session(
                session, ["council-member", f"council-{lens}"], session_type, model, workspace
            )
            sessions[lens] = session
        except RuntimeError as e:
            failed.append({"soul": lens, "error": str(e)})

    # Sessions boot concurrently; wait for each to be input-ready so an
    # immediate `council ask` doesn't paste into a half-booted pane.
    not_ready = [s for s in (orchestrator, *sessions.values()) if not wait_ready(s)]

    state.write_sitting(
        name,
        state.Sitting(
            orchestrator=orchestrator,
            roster=[lens for lens in roster if lens in sessions],
            sessions=sessions,
            started_at=state.now_iso(),
            cwd=workspace,
            session_type=session_type,
        ),
    )

    payload = {
        "success": not failed and not not_ready,
        "orchestrator": orchestrator,
        "sessions": sessions,
        "failed": failed,
        "not_ready": not_ready,
        "advisory": advisory,
    }
    human = (
        f"Council sitting started: {orchestrator} + "
        f"{len(sessions)} souls ({', '.join(sessions)})"
    )
    if advisory:
        human += f"\n{advisory}"
    if failed:
        human += f"\nfailed: {', '.join(f['soul'] for f in failed)}"
    if not_ready:
        human += f"\nnot ready after wait: {', '.join(not_ready)}"
    return _emit(args, payload, human, exit_code=0 if payload["success"] else 1, council=name)


def cmd_council_stop(args) -> int:
    _sweep_legacy_once()
    name, err = resolve_name(args)
    if err:
        return _emit_error(args, err)
    sitting = state.read_sitting(name)
    if sitting is None:
        return _emit_error(args, f"no council sitting '{name}'")

    live = list_live_sessions()
    killed: list[str] = []
    not_running: list[str] = []
    for session in (*sitting.sessions.values(), sitting.orchestrator):
        if session in live and kill_session(session):
            killed.append(session)
        else:
            not_running.append(session)
    state.clear_sitting(name)

    payload = {"success": True, "killed": killed, "not_running": not_running}
    return _emit(
        args,
        payload,
        f"Council '{name}' stopped ({len(killed)} sessions killed). Prompt history kept.",
        council=name,
    )


def cmd_council_status(args) -> int:
    _sweep_legacy_once()
    name, err = resolve_name(args)
    if err:
        return _emit(args, {"success": True, "running": False, "error": err}, err)
    sitting = state.read_sitting(name)
    if sitting is None:
        return _emit(
            args,
            {"success": True, "running": False},
            f"No council sitting '{name}'.",
        )

    live = list_live_sessions()
    souls = [
        {"soul": lens, "session": session, "alive": session in live}
        for lens, session in sitting.sessions.items()
    ]
    prompts = []
    for pid in range(1, sitting.next_prompt_id):
        pending = inbox.pending_souls(name, pid, sitting.roster)
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
        f"Council '{name}' (started {sitting.started_at})",
        f"  orchestrator: {sitting.orchestrator} "
        f"[{'alive' if payload['orchestrator_alive'] else 'DOWN'}]",
    ]
    for s in souls:
        lines.append(f"  {s['soul']}: {s['session']} [{'alive' if s['alive'] else 'DOWN'}]")
    for p in prompts:
        status = "complete" if p["complete"] else f"pending: {', '.join(p['pending'])}"
        lines.append(f"  prompt #{p['id']}: {status}")
    return _emit(args, payload, "\n".join(lines), council=name)


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
    _sweep_legacy_once()
    name, err = resolve_name(args)
    if err:
        return _emit_error(args, err)
    sitting = state.read_sitting(name)
    if sitting is None:
        return _emit_error(args, f"no council sitting '{name}'")

    prompt_text = getattr(args, "prompt", None) or _prompt_text_from(args)
    if not prompt_text or not prompt_text.strip():
        return _emit_error(args, "no prompt text (positional, --file, or stdin)")
    prompt_text = prompt_text.strip()

    prompt_id = state.allocate_prompt_id(name)
    inbox.create_prompt(name, prompt_id, prompt_text, sitting.roster)  # inbox before any send

    prompt_path = inbox.prompt_dir(name, prompt_id) / "prompt.md"
    message = (
        f"[COUNCIL PROMPT #{prompt_id}]\n"
        f"{prompt_text}\n\n"
        f"Reply through your lens with exactly one of:\n"
        f'  agentwire council reply --name {name} --prompt {prompt_id} --take --text "<your take>"\n'
        f"  agentwire council reply --name {name} --prompt {prompt_id} --ack\n"
        f"  agentwire council reply --name {name} --prompt {prompt_id} --pass\n"
        f"Full prompt on disk: {prompt_path}"
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
    return _emit(
        args,
        payload,
        human,
        exit_code=0 if sent_to else 1,
        council=name,
        echo_suffix=f"(prompt #{prompt_id})",
    )


def cmd_council_collect(args) -> int:
    _sweep_legacy_once()
    name, err = resolve_name(args)
    if err:
        return _emit_error(args, err)
    sitting = state.read_sitting(name)
    if sitting is None:
        return _emit_error(args, f"no council sitting '{name}'")

    prompt_id = getattr(args, "prompt", None) or state.latest_prompt_id(name)
    if not prompt_id:
        return _emit_error(args, "no prompts asked yet")

    result = inbox.collect(
        name,
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
    return _emit(
        args, result, "\n".join(lines), council=name, echo_suffix=f"(prompt #{prompt_id})"
    )


def _infer_soul(sitting: state.Sitting, session: str | None) -> str | None:
    """Recover the lens for a session via the sitting's SSOT map.

    Never ``.split('-')`` a session string — ``sitting.sessions`` is the only
    authority for the lens→session mapping.
    """
    if not session:
        return None
    for lens, sess in sitting.sessions.items():
        if sess == session:
            return lens
    return None


def cmd_council_reply(args) -> int:
    _sweep_legacy_once()
    kinds = [k for k in inbox.KINDS if getattr(args, k.replace("-", "_"), False)]
    if len(kinds) != 1:
        return _emit_error(args, "specify exactly one of --take / --ack / --pass")
    kind = kinds[0]

    name, err = resolve_name(args)
    if err:
        return _emit_error(args, err)
    sitting = state.read_sitting(name)
    if sitting is None:
        return _emit_error(args, f"no council sitting '{name}'")

    prompt_id = getattr(args, "prompt", None) or state.latest_prompt_id(name)
    if not prompt_id:
        return _emit_error(args, "no prompts asked yet")

    soul = getattr(args, "soul", None) or _infer_soul(sitting, current_session())
    if not soul:
        return _emit_error(args, "could not infer soul — pass --soul <lens>")

    # Only --take falls back to stdin; ack/pass must not block on a pipe.
    text = (
        (_prompt_text_from(args) if kind == "take" else getattr(args, "text", None)) or ""
    )
    if kind == "take" and not text.strip():
        return _emit_error(args, "--take requires text (--text, --file, or stdin)")

    try:
        path, is_followup = inbox.write_reply(name, prompt_id, soul, kind, text)
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
        council=name,
    )


def _age(iso: str) -> str:
    """Compact humanized age (``5m`` / ``2h`` / ``3d``) for the list table."""
    try:
        started = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "?"
    secs = int((datetime.now(timezone.utc) - started).total_seconds())
    if secs < 60:
        return f"{max(secs, 0)}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def cmd_council_list(args) -> int:
    _sweep_legacy_once()
    live = list_live_sessions()
    rows = []
    for name in state.list_sittings():
        sitting = state.read_sitting(name)
        if sitting is None:
            continue
        all_sessions = [*sitting.sessions.values(), sitting.orchestrator]
        rows.append(
            {
                "name": name,
                "cwd": sitting.cwd,
                "started_at": sitting.started_at,
                "live_sessions": sum(1 for s in all_sessions if s in live),
                "total_sessions": len(all_sessions),
                "prompts": max(sitting.next_prompt_id - 1, 0),
            }
        )
    rows.sort(key=lambda r: r["started_at"])  # oldest-first

    payload = {"success": True, "councils": rows}
    if not rows:
        return _emit(args, payload, "No council sittings.")
    header = f"{'NAME':<24} {'AGE':>5} {'LIVE':>7} {'PROMPTS':>8}  CWD"
    lines = [header]
    for r in rows:
        live_col = f"{r['live_sessions']}/{r['total_sessions']}"
        lines.append(
            f"{r['name']:<24} {_age(r['started_at']):>5} {live_col:>7} "
            f"{r['prompts']:>8}  {r['cwd']}"
        )
    return _emit(args, payload, "\n".join(lines))
