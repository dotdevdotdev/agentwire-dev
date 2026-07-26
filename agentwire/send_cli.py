"""CLI for sending input to sessions/panes — ``agentwire send`` and ``send-keys``.

``send`` pastes a prompt and presses Enter (with optional readiness wait and
delivery verification); ``send-keys`` sends raw key groups with no automatic
Enter. Both support the ``session@machine`` remote form.
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
import sys
import time

from . import pane_manager
from .core import (
    _get_machine_config,
    _output_json,
    _output_result,
    _parse_session_target,
    _run_remote,
)


def _recover_unverified_send(session: str, prompt: str, sender: str) -> "str | None":
    """Fallback when `send_verified` can't confirm delivery (#834, #835 review).

    First checks whether the bare message is already on scrollback outside
    the input box. A `send_verified` failure can mean the paste genuinely
    never landed/submitted, OR that it fully submitted and only the
    *confirm* read was ambiguous (an unparseable box frame, or a laggy host
    blowing the submit budget). The msg-inbox drain's own dedup can't catch
    the latter case on its own: it matches the WRAPPED ``[MSG from ... ]
    ... <id>`` render (see ``inbox.Message.render``), never the bare text
    this path pasted directly — enqueuing unconditionally would risk a real
    duplicate delivery rather than the safe no-op the drain's dedup gives
    its own messages. Checking here first avoids that duplicate in the
    common case.

    A message that lands only *after* this check runs (queued invisibly
    mid-generation, per ``submit_confirmed``'s own docstring) can still
    duplicate — accepted per this codebase's existing bias that a
    recoverable duplicate beats a silently dropped message.

    Known residual risk (#835 second-pass review): this is the same bare
    whitespace-normalized substring match ``_deliver_once`` already uses for
    its own internal idempotent-paste guard — not a new invention — but
    reused here it trades in the *opposite* direction from most of this
    file's checks. A false match reports "already delivered" and SKIPS the
    inbox enqueue entirely: for a short or generic message (a bare "yes",
    "continue", "approved") that happens to already sit in the last
    ``VERIFY_SCROLLBACK_LINES`` for an unrelated reason, a send that
    genuinely never landed could be silently dropped instead of queued —
    the one outcome this whole fallback exists to prevent. Tracked as a
    follow-up (a lightweight per-attempt marker, mirroring
    ``send_verified``'s own ``marker`` param, would close this precisely);
    not fixed here because it's strictly narrower than the bug this
    function was written to close (needs a coincidental/repeated-text
    match, not just any unverified send).

    Returns ``"already_delivered"``, ``"inbox"``, or ``None`` (the inbox
    fallback itself failed).
    """
    from agentwire.session_ready import message_on_scrollback, recover_failed_seed, scrollback

    if message_on_scrollback(scrollback(session), prompt):
        return "already_delivered"
    return recover_failed_seed(session, prompt, sender=sender)


def _fallback_suffix(fallback: "str | None") -> str:
    if fallback == "already_delivered":
        return " — already delivered directly (the confirmation read was just ambiguous); no action needed"
    if fallback == "inbox":
        return " — queued to its msg inbox for guaranteed delivery"
    return " and could not be queued — resend manually"


def cmd_send(args) -> int:
    """Send a prompt to a tmux session or pane (adds Enter automatically).

    Supports remote sessions with session@machine format.
    Use --pane N to send to a specific pane in the current session.
    """
    session_full = getattr(args, 'session', None)
    pane_index = getattr(args, 'pane', None)
    prompt = " ".join(args.prompt) if args.prompt else ""
    json_mode = getattr(args, 'json', False)
    wait_ready = getattr(args, 'wait_ready', False)
    verify = getattr(args, 'verify', False)
    # Candidate sender for a msg-inbox fallback's attribution (#835 review):
    # prefer the real calling session over the generic "agentwire" so
    # dead-letter emails and the rendered [MSG from ...] header stay
    # meaningful. MCP forwards --caller-session explicitly (can't reliably
    # auto-detect the caller across that boundary); a bare CLI invocation
    # falls back to auto-detecting its own tmux session.
    fallback_sender = getattr(args, 'caller_session', None) or pane_manager.get_current_session() or "agentwire"

    if wait_ready and pane_index is not None:
        return _output_result(False, json_mode, "--wait-ready targets a session's pane 0; it can't be combined with --pane")

    # Handle pane mode (auto-detect session from environment)
    if pane_index is not None:
        if not prompt:
            return _output_result(False, json_mode, "Usage: agentwire send --pane N <prompt>")

        try:
            target_session = session_full or pane_manager.get_current_session()
            if verify:
                # Confirm the paste actually landed in the pane (a paste into a
                # busy/booting pane can vanish silently). Report verified vs not.
                # retries=0: send ONCE and report — never re-paste. A streaming
                # pane can scroll the echo out of the capture window and produce
                # a false miss; re-pasting there would deliver the instruction
                # twice, which is worse than a false "not verified" warning.
                from agentwire.session_ready import send_verified

                ok = send_verified(target_session, prompt, pane_index=pane_index, retries=0)
                if json_mode:
                    _output_json({
                        "success": True, "pane": pane_index, "session": target_session,
                        "verified": ok,
                        "message": "Prompt sent (verified)" if ok else "Prompt sent but delivery could not be verified",
                    })
                else:
                    print(f"Sent to pane {pane_index}" + ("" if ok else " (delivery NOT verified)"))
                return 0
            pane_manager.send_to_pane(session_full, pane_index, prompt)
            if json_mode:
                _output_json({
                    "success": True,
                    "pane": pane_index,
                    "session": target_session,
                    "message": "Prompt sent"
                })
            else:
                print(f"Sent to pane {pane_index}")
            return 0
        except RuntimeError as e:
            return _output_result(False, json_mode, str(e))

    # Session mode (original behavior)
    if not session_full:
        if json_mode:
            print(json.dumps({"success": False, "error": "Session name required (-s) or pane number (--pane)"}))
        else:
            print("Usage: agentwire send -s <session> <prompt>", file=sys.stderr)
            print("   or: agentwire send --pane N <prompt>", file=sys.stderr)
        return 1

    if not prompt:
        if json_mode:
            print(json.dumps({"success": False, "error": "Prompt required"}))
        else:
            print("Usage: agentwire send -s <session> <prompt>", file=sys.stderr)
        return 1

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)
    if wait_ready and machine_id:
        return _output_result(False, json_mode, "--wait-ready is local-only (readiness capture doesn't span SSH)")
    if machine_id:
        # Remote: SSH and run tmux commands
        machine = _get_machine_config(machine_id)
        if machine is None:
            if json_mode:
                print(json.dumps({"success": False, "error": f"Machine '{machine_id}' not found"}))
            else:
                print(f"Machine '{machine_id}' not found in machines.json", file=sys.stderr)
            return 1

        # Build remote command — use load-buffer for anything non-trivial
        quoted_session = shlex.quote(session)
        use_buffer = len(prompt) > 10 or "\n" in prompt

        if use_buffer:
            encoded = base64.b64encode(prompt.encode()).decode()
            # Pipe straight into the tmux buffer — no on-disk temp that could
            # expose message content or be pre-planted on the remote host
            cmd = (
                f"echo {shlex.quote(encoded)} | base64 -d | tmux load-buffer - && "
                f"tmux paste-buffer -t {quoted_session} && "
                f"sleep 0.5 && "
                f"tmux send-keys -t {quoted_session} Enter"
            )
        else:
            quoted_prompt = shlex.quote(prompt)
            cmd = f"tmux send-keys -t {quoted_session} -l {quoted_prompt} && sleep 0.5 && tmux send-keys -t {quoted_session} Enter"

        # For multi-line text, Claude Code shows "[Pasted text...]" and waits for Enter
        if "\n" in prompt or len(prompt) > 200:
            cmd += f" && sleep 0.5 && tmux send-keys -t {quoted_session} Enter"

        result = _run_remote(machine_id, cmd)
        if result.returncode != 0:
            if json_mode:
                print(json.dumps({"success": False, "error": f"Failed to send to {session_full}"}))
            else:
                print(f"Failed to send to {session_full}: {result.stderr}", file=sys.stderr)
            return 1

        if json_mode:
            # Delivery can't be verified across SSH (no pane capture) — say so
            # honestly rather than implying confirmation.
            out = {"success": True, "session": session_full, "machine": machine_id, "message": "Prompt sent"}
            if verify:
                out["verified"] = None  # unverifiable: remote session
            print(json.dumps(out))
        else:
            print(f"Sent to {session_full}")
        return 0

    # Local: existing logic
    # Check if session exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if result.returncode != 0:
        if json_mode:
            print(json.dumps({"success": False, "error": f"Session '{session}' not found"}))
        else:
            print(f"Session '{session}' not found", file=sys.stderr)
        return 1

    if wait_ready:
        # Wait for the agent to be ready, then send with delivery
        # verification (a paste into a booting Claude vanishes silently).
        from agentwire.session_ready import send_verified, wait_for_session_ready

        timeout = getattr(args, 'timeout', None) or 30.0
        if not wait_for_session_ready(session, timeout=timeout):
            return _output_result(False, json_mode, f"Agent in '{session}' not ready after {timeout:.0f}s")
        if not send_verified(session, prompt):
            # A caller acting on "not verified" text is optional, not
            # guaranteed — under load this is exactly the class of message
            # that must never depend on an LLM noticing and manually
            # resending. Fall back to the durable msg inbox (retried across
            # ticks, dead-lettered + emailed on true exhaustion) instead of
            # returning inert advisory text (#834).
            fallback = _recover_unverified_send(session, prompt, fallback_sender)
            if json_mode:
                print(json.dumps({"success": False, "session": session_full, "verified": False,
                                  "fallback": fallback, "error": "Delivery not verified"}))
            else:
                print(f"Sent to {session} but delivery could not be verified{_fallback_suffix(fallback)}", file=sys.stderr)
            return 1
        if json_mode:
            print(json.dumps({"success": True, "session": session_full, "machine": None,
                              "verified": True, "fallback": None, "message": "Prompt sent"}))
        else:
            print(f"Sent to {session} (verified)")
        return 0

    if verify:
        # Same delivery verification as --wait-ready, but without waiting for a
        # fresh-boot banner — for an already-running session, confirm the paste
        # landed AND submitted. retries=1: a whole-send retry only fires when the
        # paste never landed in the box (phase 1 of _deliver_once) — i.e. it
        # genuinely vanished, so re-pasting can't double-deliver. Once text has
        # landed, _deliver_once re-presses Enter in place (never re-pastes), so
        # the old double-delivery worry the retries=0 choice guarded against
        # can't happen anymore. Staying patient here is what keeps a laggy host
        # from surfacing a false failure the parent has to clean up by hand.
        from agentwire.session_ready import send_verified

        ok = send_verified(session, prompt, retries=1)
        fallback = None
        if not ok:
            # Same durable fallback as --wait-ready (#834).
            fallback = _recover_unverified_send(session, prompt, fallback_sender)
        if json_mode:
            print(json.dumps({"success": True, "session": session_full, "machine": None,
                              "verified": ok, "fallback": fallback,
                              "message": "Prompt sent" if ok else "Prompt sent but delivery could not be verified"}))
        else:
            if ok:
                print(f"Sent to {session} (verified)")
            else:
                print(f"Sent to {session} (delivery NOT verified){_fallback_suffix(fallback)}")
        return 0

    # Delegate paste + Enter handling to the shared pane_manager helper so
    # send-to-session and send-to-pane stay in lockstep. The helper handles
    # buffer-vs-send-keys, settle delay, and the bracketed-paste double-Enter
    # in one place.
    try:
        pane_manager.send_to_target(session, prompt, enter=True)
    except RuntimeError as e:
        return _output_result(False, json_mode, str(e))

    if json_mode:
        print(json.dumps({"success": True, "session": session_full, "machine": None, "message": "Prompt sent"}))
    else:
        print(f"Sent to {session}")
    return 0


def cmd_send_keys(args) -> int:
    """Send raw keys to a tmux session (no automatic Enter).

    Each argument is sent as a separate key group with a brief pause between.
    Useful for sending special keys like Enter, Escape, C-c, etc.

    Supports remote sessions with session@machine format.
    """
    session_full = args.session
    keys = args.keys if args.keys else []

    if not session_full:
        print("Usage: agentwire send-keys -s <session> <keys>...", file=sys.stderr)
        return 1

    if not keys:
        print("Usage: agentwire send-keys -s <session> <keys>...", file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print("  agentwire send-keys -s mysession Enter", file=sys.stderr)
        print("  agentwire send-keys -s mysession C-c", file=sys.stderr)
        print("  agentwire send-keys -s mysession Escape", file=sys.stderr)
        print("  agentwire send-keys -s mysession 'hello world' Enter", file=sys.stderr)
        return 1

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)

    # Optional pane targeting (worker panes); None targets the session.
    pane = getattr(args, 'pane', None)
    target = f"{session}.{pane}" if pane is not None else session

    if machine_id:
        # Remote: SSH and run tmux commands
        machine = _get_machine_config(machine_id)
        if machine is None:
            print(f"Machine '{machine_id}' not found in machines.json", file=sys.stderr)
            return 1

        # Build remote command with pauses between keys
        quoted_target = shlex.quote(target)
        cmd_parts = []
        for i, key in enumerate(keys):
            cmd_parts.append(f"tmux send-keys -t {quoted_target} {shlex.quote(key)}")
            if i < len(keys) - 1:
                cmd_parts.append("sleep 0.1")

        cmd = " && ".join(cmd_parts)

        result = _run_remote(machine_id, cmd)
        if result.returncode != 0:
            print(f"Failed to send keys to {session_full}: {result.stderr}", file=sys.stderr)
            return 1

        print(f"Sent keys to {session_full}")
        return 0

    # Local: existing logic
    # Check if session exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if result.returncode != 0:
        print(f"Session '{session}' not found", file=sys.stderr)
        return 1

    # Send each key group with a pause between
    for i, key in enumerate(keys):
        subprocess.run(
            ["tmux", "send-keys", "-t", target, key],
            check=True
        )
        # Brief pause between key groups (not after last one)
        if i < len(keys) - 1:
            time.sleep(0.1)

    print(f"Sent keys to {target}")
    return 0


def register_send_parser(subparsers) -> None:
    send_parser = subparsers.add_parser("send", help="Send prompt to a session or pane (adds Enter)")
    send_parser.add_argument("-s", "--session", help="Target session (supports session@machine)")
    send_parser.add_argument("--pane", type=int, help="Target pane index (auto-detects session)")
    send_parser.add_argument("prompt", nargs="*", help="Prompt to send")
    send_parser.add_argument("--wait-ready", dest="wait_ready", action="store_true",
                             help="Wait for the agent to be ready, then verify delivery (local sessions only)")
    send_parser.add_argument("--verify", action="store_true",
                             help="Confirm the message actually landed in the pane (local only); "
                                  "report verified vs unconfirmed instead of blind success")
    send_parser.add_argument("--timeout", type=float, default=30.0,
                             help="Readiness wait timeout in seconds (with --wait-ready, default: 30)")
    send_parser.add_argument("--caller-session", dest="caller_session",
                             help="Internal: attribute a msg-inbox delivery fallback to this sender "
                                  "instead of auto-detecting the calling tmux session. MCP forwards "
                                  "this explicitly, since the CLI subprocess can't reliably "
                                  "auto-detect the caller across that boundary.")
    send_parser.add_argument("--json", action="store_true", help="Output as JSON")
    send_parser.set_defaults(func=cmd_send)

    # === send-keys command ===
    send_keys_parser = subparsers.add_parser(
        "send-keys", help="Send raw keys to a session (with pause between groups)"
    )
    send_keys_parser.add_argument("-s", "--session", required=True, help="Target session (supports session@machine)")
    send_keys_parser.add_argument("--pane", type=int, default=None,
                                  help="Target a specific pane index (default: the session's active pane)")
    send_keys_parser.add_argument("keys", nargs="*", help="Key groups to send (e.g., 'hello world' Enter)")
    send_keys_parser.set_defaults(func=cmd_send_keys)
