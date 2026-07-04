"""MCP tools — council domain."""

from .mcp_core import (
    mcp,
    run_agentwire_cmd,
)


def _council_tag(data: dict) -> str:
    """A ``[council: <name>]`` echo prefix so every tool surfaces which
    sitting it acted on (the CLI returns the resolved name as ``council``)."""
    name = data.get("council")
    return f"[council: {name}] " if name else ""


@mcp.tool()
def council_start(name: str = "", roster: str = "", model: str = "") -> str:
    """Start a council sitting: orchestrator + one session per lens soul.

    Sittings are namespaced — spins up an ``agentwire-council-<name>``
    orchestrator and ``council-<name>-<lens>`` sessions (default roster:
    brain, conscience, gut, critic, historian, devils-advocate). Independent
    sittings run concurrently; sessions stay warm until ``council_stop``.

    Args:
        name: Sitting name (empty = derived from the current repo/dir)
        roster: Comma-separated lens names (empty = full default roster)
        model: Model override for all council sessions

    Returns:
        Orchestrator + soul session names, or failure details.
    """
    args = ["council", "start"]
    if name:
        args += ["--name", name]
    if roster:
        args += ["--roster", roster]
    if model:
        args += ["--model", model]
    # Start waits for every session to boot — well past the default timeout.
    data = run_agentwire_cmd(args, timeout=300)
    if not data.get("success"):
        return f"Failed to start council: {data.get('error') or data}"
    sessions = data.get("sessions", {})
    lines = [f"{_council_tag(data)}Council sitting started: {data.get('orchestrator')}"]
    if data.get("advisory"):
        lines.append(f"  ({data['advisory']})")
    lines += [f"  {lens}: {sname}" for lens, sname in sessions.items()]
    for f in data.get("failed") or []:
        lines.append(f"  ! {f['soul']}: {f['error']}")
    return "\n".join(lines)


@mcp.tool()
def council_stop(name: str = "", minutes: bool = True, synthesis: str = "") -> str:
    """Stop a council sitting: kill all soul sessions + the orchestrator.

    Prompt history under ``~/.agentwire/council/<name>/prompts/`` is kept, and
    by default the sitting's minutes artifact is rendered on the way out
    (when any prompt exists) — pass your synthesis so the record includes it.

    Args:
        name: Sitting name (empty = cwd-repo-slug / sole live sitting)
        minutes: Render the minutes artifact (default True; skipped anyway
            when the sitting has no prompt history)
        synthesis: Your synthesis for the minutes — text, or a path to a file

    Returns:
        Which sessions were killed, plus the minutes path when rendered.
    """
    args = ["council", "stop"]
    if name:
        args += ["--name", name]
    if not minutes:
        args += ["--no-minutes"]
    if synthesis:
        args += ["--synthesis", synthesis]
    data = run_agentwire_cmd(args, timeout=60)
    if not data.get("success"):
        return f"Failed to stop council: {data.get('error', 'Unknown error')}"
    killed = data.get("killed") or []
    out = f"{_council_tag(data)}Council stopped. Killed: {', '.join(killed) or '(none)'}"
    if data.get("minutes"):
        out += f"\nMinutes: {data['minutes']}"
    return out


@mcp.tool()
def council_status(name: str = "") -> str:
    """Show a council sitting: session liveness and per-prompt reply state.

    Args:
        name: Sitting name (empty = cwd-repo-slug / sole live sitting)

    Returns:
        Roster health and which souls are still pending on open prompts.
    """
    args = ["council", "status"]
    if name:
        args += ["--name", name]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to get council status: {data.get('error', 'Unknown error')}"
    if not data.get("running"):
        return data.get("error") or "No active council sitting."
    lines = [
        f"{_council_tag(data)}Council sitting (started {data.get('started_at')})",
        f"  orchestrator: {data.get('orchestrator')} "
        f"[{'alive' if data.get('orchestrator_alive') else 'DOWN'}]",
    ]
    for s in data.get("souls") or []:
        lines.append(f"  {s['soul']}: {s['session']} [{'alive' if s['alive'] else 'DOWN'}]")
    for p in data.get("prompts") or []:
        status = "complete" if p["complete"] else f"pending: {', '.join(p['pending'])}"
        lines.append(f"  prompt #{p['id']}: {status}")
    return "\n".join(lines)


@mcp.tool()
def council_list() -> str:
    """List every known council sitting, oldest-first.

    Returns:
        name · cwd · age · live/total sessions · prompts for each sitting —
        the age column surfaces forgotten token-burning sittings.
    """
    data = run_agentwire_cmd(["council", "list"])
    if not data.get("success"):
        return f"Failed to list councils: {data.get('error', 'Unknown error')}"
    councils = data.get("councils") or []
    if not councils:
        return "No council sittings."
    lines = [f"{'NAME':<24} {'LIVE':>7} {'PROMPTS':>8}  CWD"]
    for c in councils:
        live = f"{c['live_sessions']}/{c['total_sessions']}"
        lines.append(f"{c['name']:<24} {live:>7} {c['prompts']:>8}  {c.get('cwd', '')}")
    return "\n".join(lines)


@mcp.tool()
def council_ask(prompt: str, name: str = "") -> str:
    """Fan a prompt out to every soul in a council sitting.

    Creates the prompt's reply inbox, then sends the prompt to every live
    lens session. Each soul will file exactly one of: a substantive take, an
    ack (researching, follow-up coming), or a pass. Follow with
    ``council_collect`` to gather the replies.

    Args:
        prompt: The question or decision to put before the council
        name: Sitting name (empty = cwd-repo-slug / sole live sitting)

    Returns:
        The prompt id (needed for council_collect) and fan-out result.
    """
    args = ["council", "ask", prompt]
    if name:
        args += ["--name", name]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to ask council: {data.get('error') or data}"
    pid = data.get("prompt_id")
    sent = data.get("sent_to") or []
    out = (
        f"{_council_tag(data)}PROMPT ID: {pid} — fanned out to "
        f"{len(sent)} souls ({', '.join(sent)})"
    )
    for f in data.get("failed") or []:
        out += f"\n  ! {f['soul']}: {f['error']}"
    return out


@mcp.tool()
def council_collect(prompt_id: int = 0, timeout: int = 120, name: str = "") -> str:
    """Collect a council's replies for a prompt (blocks until done/timeout).

    Returns as soon as every roster soul has filed a take, ack, or pass — or
    when the soft timeout lapses. Re-collecting a complete prompt returns
    instantly and includes any follow-up takes filed since (the
    ack-and-research path).

    Args:
        prompt_id: Prompt id from council_ask (0 = latest)
        timeout: Soft timeout in seconds (default 120)
        name: Sitting name (empty = cwd-repo-slug / sole live sitting)

    Returns:
        Every reply attributed by soul, plus any souls still pending.
    """
    args = ["council", "collect", "--timeout", str(timeout)]
    if prompt_id:
        args += ["--prompt", str(prompt_id)]
    if name:
        args += ["--name", name]
    # Pad the subprocess timeout past the blocking collect window.
    data = run_agentwire_cmd(args, timeout=timeout + 15)
    if not data.get("success"):
        return f"Failed to collect: {data.get('error') or data}"
    lines = [
        f"{_council_tag(data)}Prompt #{data.get('prompt_id')}: "
        + ("complete" if data.get("complete") else f"pending: {', '.join(data.get('pending') or [])}")
    ]
    for r in data.get("replies") or []:
        lines.append(f"\n--- {r['soul']} ({r['kind']}) ---\n{r['text']}")
    return "\n".join(lines)


@mcp.tool()
def council_minutes(name: str = "", prompt: str = "", synthesis: str = "") -> str:
    """Render a sitting's minutes: question, synthesis, verbatim takes → HTML.

    Deterministically renders the sitting's persisted prompt history
    (question + attributed take/ack/pass replies) plus your optional
    synthesis into a self-contained HTML artifact at
    ``~/.agentwire/artifacts/council-<name>-minutes/index.html``, and opens
    it as a portal artifact window when the portal is up. Works for live and
    dismissed sittings alike — prompt history survives ``council_stop``.

    Args:
        prompt: Prompt id to render, or 'all' (empty = all)
        synthesis: Your synthesis of the takes — text, or a path to a file
        name: Sitting name (empty = cwd-repo-slug / sole live sitting)

    Returns:
        The rendered artifact's path, and whether a portal window opened.
    """
    args = ["council", "minutes"]
    if name:
        args += ["--name", name]
    if prompt:
        args += ["--prompt", prompt]
    if synthesis:
        args += ["--synthesis", synthesis]
    data = run_agentwire_cmd(args, timeout=60)
    if not data.get("success"):
        return f"Failed to render minutes: {data.get('error') or data}"
    out = f"{_council_tag(data)}Minutes: {data.get('path')}"
    if data.get("opened"):
        out += "\nOpened as a portal artifact window."
    return out
