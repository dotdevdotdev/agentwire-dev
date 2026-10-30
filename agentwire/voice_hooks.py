"""Deterministic speech for the ``agentwire-voice`` session (#1198, slice 2).

A role prompt cannot guarantee the assistant speaks: in the first live test it
answered correctly and never called ``say``. These Claude Code hooks make the
two utterances a property of the session instead of the model:

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

#: Prompts that are not a request from the owner: slash commands (the context
#: policy's /compact) and msg-inbox deliveries (a report from a session the
#: assistant started). Those get no "On it." — the Stop hook still speaks
#: whatever the turn produced.
_NOT_A_REQUEST = re.compile(r"^\s*(?:/|\[MSG from )")

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

#: How much of a long report rides along as the on-screen card.
DISPLAY_LIMIT = 1500


def claude_settings() -> dict:
    """The ``--settings`` payload: this module, once per event."""
    def hook(event: str) -> list[dict]:
        cmd = f"{shlex.quote(sys.executable)} -m agentwire.voice_hooks {event}"
        return [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]

    return {"hooks": {
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


def on_notification(payload: dict) -> None:
    if payload.get("notification_type") not in _APPROVAL_TYPES:
        return
    signal("approval")
    detail = spoken_summary(payload.get("message") or "", limit=140).rstrip(".")
    speak(f"Approval needed: {detail}. Say yes or no." if detail
          else "Approval needed. Say yes or no.", kind="approval")


HANDLERS = {
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
