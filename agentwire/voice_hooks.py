"""Deterministic speech for the ``agentwire-voice`` session (#1198, slice 2).

A role prompt cannot guarantee the assistant speaks: in the first live test it
answered correctly and never called ``say``. These Claude Code hooks make the
two utterances a property of the session instead of the model:

- **SessionStart** injects the system digest (``agentwire voice digest``):
  sessions with live state, projects, scheduler, config.
- **UserPromptSubmit** speaks a fixed acknowledgement the moment a request
  arrives (no model involved), and opens the turn's state.
- **PostToolUse** on ``mcp__agentwire__say`` marks that the model spoke a
  result this turn.
- **Stop** speaks the result when the model didn't: a headline of at most
  ``MAX_SPOKEN_WORDS`` from ``last_assistant_message`` (the field the Stop
  payload carries — no transcript parsing), with the full text on screen
  when it had to be cut.
- **StopFailure** speaks why the turn died (rate limit, expired login, ...).
- **Notification** speaks approval requests, which would otherwise sit
  silently in a root session with no screen in front of it. The owner answers
  them by voice: ``agentwire voice ask "yes"`` answers the live dialog.

Each event also signals the portal (``agentwire voice state``) so the status
chip shows working / done / failed / approval from the session's real state.

Scope: these hooks reach ONE process. ``claude_settings()`` is passed as
``claude --settings '<json>'`` on the voice-assistant's launch line (see
``roles.role_settings``), so no other session and no user settings see them;
user-level hooks (damage-control) still merge in alongside.

Never blocks and never fails loudly: speech is spawned detached and every
error is swallowed, so a TTS outage costs silence, never a stuck turn. Kept
stdlib-only — it runs on every prompt and every turn end.
"""

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SESSION = "agentwire-voice"
SAY_TOOL = "mcp__agentwire__say"
ACK = "On it."

#: One small file: the current turn's prompt id and whether a result was spoken.
STATE_FILE = Path.home() / ".agentwire" / "voice" / "turn.json"

#: The pending approval's plain-language intent (and exact command), written
#: when it is announced so ``voice ask`` can judge a spoken reply against it.
APPROVAL_FILE = Path.home() / ".agentwire" / "voice" / "approval.json"

#: Prompts that are not a request from the owner: slash commands (the context
#: policy's /compact) and msg-inbox deliveries from other sessions (a report
#: from a session the assistant started). Those get no "On it." — the Stop hook
#: still speaks whatever the turn produced. ``[MSG from owner …]`` IS the
#: owner: a request queued behind an approval dialog (``voice ask``).
_NOT_A_REQUEST = re.compile(r"^\s*(?:/|\[MSG from (?!owner ))")

#: StopFailure ``error`` values → what to say. Unknown values are spoken as-is.
_FAILURE_REASONS = {
    "rate_limit": "I hit a usage limit",
    "authentication_failed": "the Claude login has expired",
    "billing_error": "there is a billing problem with the account",
    "server_error": "the Claude API had a server error",
    "invalid_request": "the request was rejected",
    "max_output_tokens": "the reply ran too long",
}

_APPROVAL_TYPES = {"permission_prompt", "elicitation_dialog"}

SUMMARY_LIMIT = 220

#: Spoken utterances from the assistant are capped at about one short
#: sentence. Longer text is trimmed to a headline; the full text goes on
#: screen (``say --display``).
MAX_SPOKEN_WORDS = 15

#: Bound on building the system digest at session start.
DIGEST_TIMEOUT_S = 20

#: How much of a long report rides along as the on-screen card.
DISPLAY_LIMIT = 1500


def claude_settings() -> dict:
    """The ``--settings`` payload: this module, once per event."""
    def hook(event: str) -> list[dict]:
        cmd = f"{shlex.quote(sys.executable)} -m agentwire.voice_hooks {event}"
        return [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]

    session_start = hook("session-start")
    session_start[0]["hooks"][0]["timeout"] = DIGEST_TIMEOUT_S + 5
    return {"hooks": {
        "SessionStart": session_start,
        "UserPromptSubmit": hook("prompt-submit"),
        "PostToolUse": [{**hook("said")[0], "matcher": SAY_TOOL}],
        "Stop": hook("stop"),
        "StopFailure": hook("stop-failure"),
        "Notification": hook("notification"),
    }}


# ─────────────────────────────────────────────────────────────
# Speech + turn state
# ─────────────────────────────────────────────────────────────


def _agentwire_binary() -> str | None:
    sibling = Path(sys.executable).with_name("agentwire")
    return str(sibling) if sibling.exists() else shutil.which("agentwire")


def _spawn(args: list[str]) -> None:
    """Run ``agentwire <args>`` detached: the hook returns in milliseconds and
    a dead TTS server or portal can only cost silence."""
    binary = _agentwire_binary()
    if not binary:
        return
    try:
        subprocess.Popen(
            [binary, *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


def speak(text: str, kind: str = "say", display: str | None = None) -> None:
    """Fire-and-forget ``agentwire say`` to the assistant session.

    ``say`` owns routing (portal dashboard vs local speakers), the machine-wide
    playback lane (``--kind`` drives its drop policy) and the TTS-off case.
    """
    if not text.strip():
        return
    args = ["say", "-s", SESSION, "--kind", kind]
    if display:
        args += ["--display", display]
    _spawn([*args, text])


def signal(state: str) -> None:
    """Tell the portal what the assistant is doing (its status chip)."""
    _spawn(["voice", "state", state])


def _load_turn() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_turn(turn: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(turn))
    except OSError:
        pass


def _already_spoken(prompt_id: str | None) -> bool:
    turn = _load_turn()
    return bool(turn.get("spoken")) and turn.get("prompt_id") == prompt_id


def _mark_spoken(prompt_id: str | None) -> None:
    _save_turn({"prompt_id": prompt_id, "spoken": True})


def spoken_summary(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """The first sentences of *text*, made sayable: no code, markup or URLs."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*(?:#+|[-*+]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if out and len(out) + 1 + len(sentence) > limit:
            break
        out = f"{out} {sentence}".strip()
    return out if len(out) <= limit else out[: limit - 1].rsplit(" ", 1)[0] + "…"


def speech_headline(text: str, max_words: int = MAX_SPOKEN_WORDS) -> tuple[str, bool]:
    """(what to say, whether anything was cut).

    Whole sentences while they fit in *max_words*; a first sentence that is
    itself too long is cut at a word boundary.
    """
    clean = spoken_summary(text, limit=10_000)
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", clean):
        words = sentence.split()
        if not words:
            continue
        if len(out) + len(words) > max_words:
            if not out:
                out = words[:max_words]
                out[-1] = out[-1].rstrip(",;:") + "…"
            return " ".join(out), True
        out += words
    return " ".join(out), False


# ─────────────────────────────────────────────────────────────
# Event handlers (payload in → what is spoken)
# ─────────────────────────────────────────────────────────────


def on_session_start(payload: dict) -> None:
    """Print the system digest; Claude Code adds SessionStart stdout to the
    context. Fires on startup, resume and after compaction, so the assistant
    never starts a stretch of conversation blind. Silent on failure."""
    binary = _agentwire_binary()
    if not binary:
        return
    try:
        result = subprocess.run([binary, "voice", "digest"], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=DIGEST_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode == 0 and result.stdout.strip():
        print(result.stdout.strip())


def on_prompt_submit(payload: dict) -> None:
    _save_turn({"prompt_id": payload.get("prompt_id"), "spoken": False})
    signal("working")
    if not _NOT_A_REQUEST.match(payload.get("prompt") or ""):
        speak(ACK, kind="ack")


def on_said(payload: dict) -> None:
    _mark_spoken(payload.get("prompt_id"))


def on_stop(payload: dict) -> None:
    prompt_id = payload.get("prompt_id")
    turn = _load_turn()
    this_turn = turn.get("prompt_id") == prompt_id
    if this_turn and turn.get("state") == "failed":
        return  # StopFailure already spoke and signalled
    signal("done")
    if this_turn and turn.get("spoken"):
        return
    _mark_spoken(prompt_id)
    message = (payload.get("last_assistant_message") or "").strip()
    headline, trimmed = speech_headline(message)
    speak(headline or "Done.", kind="report",
          display=message[:DISPLAY_LIMIT] if trimmed else None)


def on_stop_failure(payload: dict) -> None:
    prompt_id = payload.get("prompt_id")
    signal("failed")
    if _already_spoken(prompt_id):
        return
    _save_turn({"prompt_id": prompt_id, "spoken": True, "state": "failed"})
    error = str(payload.get("error") or "unknown error")
    speak(f"That failed: {_FAILURE_REASONS.get(error, error.replace('_', ' '))}.", kind="failure")


def pending_tool_call(transcript_path: str | None) -> tuple[str, dict, str]:
    """(tool name, tool input, the assistant's text alongside it) for the
    newest tool call in the transcript — the one the dialog is asking about.
    Empty values when the transcript can't be read."""
    try:
        lines = Path(transcript_path).read_text().splitlines() if transcript_path else []
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") != "assistant":
            continue
        content = (row.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        calls = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
        if calls:
            text = " ".join(c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text")
            return calls[-1].get("name", ""), calls[-1].get("input") or {}, text
    return "", {}, ""


def approval_request(payload: dict) -> tuple[str, str]:
    """(what to say, what to show) for a pending approval.

    Spoken: the INTENT in plain words — the tool call's own ``description``
    (Bash carries one), else the assistant's stated plan — then "Yes or no?".
    Never the raw command: it is unreadable aloud. The exact command goes in
    the on-screen card instead.
    """
    intent, exact, tool = approval_intent(payload)
    headline, _ = speech_headline(intent, max_words=MAX_SPOKEN_WORDS - 3)
    display = f"**Approval needed** ({tool or 'tool'})\n{exact}" if exact else None
    return f"{headline.rstrip('.!?…')}. Yes or no?", display


def approval_intent(payload: dict) -> tuple[str, str, str]:
    """(intent in plain words, exact command/target, tool name)."""
    tool, tool_input, plan = pending_tool_call(payload.get("transcript_path"))
    intent = (tool_input.get("description") or plan
              or payload.get("message") or f"use {tool or 'a tool'}")
    exact = tool_input.get("command") or tool_input.get("file_path") or (
        json.dumps(tool_input)[:DISPLAY_LIMIT] if tool_input else "")
    return intent, exact, tool


def on_notification(payload: dict) -> None:
    if payload.get("notification_type") not in _APPROVAL_TYPES:
        return
    signal("approval")
    intent, exact, _tool = approval_intent(payload)
    try:
        APPROVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        APPROVAL_FILE.write_text(json.dumps({"intent": intent, "command": exact}))
    except OSError:
        pass
    text, display = approval_request(payload)
    speak(text, kind="approval", display=display)


HANDLERS = {
    "session-start": on_session_start,
    "prompt-submit": on_prompt_submit,
    "said": on_said,
    "stop": on_stop,
    "stop-failure": on_stop_failure,
    "notification": on_notification,
}


def main(argv: list[str]) -> int:
    """Hook entry point. Always exits 0: a speech hook must never block a turn."""
    handler = HANDLERS.get(argv[0]) if argv else None
    if handler is None:
        return 0
    try:
        handler(json.load(sys.stdin))
    except Exception:  # noqa: BLE001  # silence beats a wedged session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
