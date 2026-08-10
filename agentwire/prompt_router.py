"""Interactive-prompt routing — notify a parent session when a child blocks.

When a session hits an interactive gate (permission confirmation, plan-mode
approval, AskUserQuestion), the human paths (audio + portal dialog) already
fire. This module adds the agent path: detect the prompt, resolve the
session's parent/orchestrator, and deliver a short text notification the
parent can act on (inspect the pane, answer with a guarded keystroke).

Two detection paths feed the same routing core:
  - hook path (seconds): the permission hook POSTs to the portal, which calls
    ``notify_permission_request()`` directly — no pane parsing needed.
  - sweep path (<=60s): ``tick()`` rides the usage-limit watchdog and scans
    every pane for plan-approval / AskUserQuestion / permission dialogs that
    fire no hooks.

Routing never auto-answers and never blocks the prompt itself: no parent
(or an unsafe delivery target) degrades to the existing human-only behavior.

State:
  ~/.agentwire/prompt-router/{session}.{pane}.json   active-prompt markers
  ~/.agentwire/prompt-router-events.jsonl            audit log
"""

import fcntl
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .core import load_session_metadata
from .usage_limit import (
    PARK_OPTION,
    _atomic_write,
    _capture,
    _normalize,
    _now,
    _session_exists,
    _tmux,
)
from .usage_limit import detect_dialog as _usage_limit_dialog
from .usage_limit import is_parked as _is_parked
from .utils.event_log import append_event

STATE_DIR = Path.home() / ".agentwire" / "prompt-router"
EVENTS_FILE = Path.home() / ".agentwire" / "prompt-router-events.jsonl"

# A prompt the parent never answered re-notifies after this long.
RENOTIFY_TTL = timedelta(minutes=10)
# Hook-source permission markers suppress the sweep's permission detector
# for slightly longer than the portal's 300s wait.
HOOK_MARKER_TTL = timedelta(minutes=6)
# Markers whose pane vanished are garbage-collected after this long.
MARKER_GC_TTL = timedelta(minutes=30)

# A root session's prompt has nowhere to route, so the owner is the only
# recipient left. Longer than RENOTIFY_TTL on purpose: this is an out-of-band
# email, not a paste into a session that is already watching.
NO_PARENT_ESCALATE_TTL = timedelta(hours=1)

# How long a pane may sit on a detected prompt before `doctor` calls it stuck.
# Comfortably longer than RENOTIFY_TTL: one re-notification going unanswered
# is a busy parent, a second is a parent that isn't coming.
STUCK_PROMPT_AFTER = timedelta(minutes=25)

# Every routed message starts with this; the detector treats its presence on
# a screen as poison so a delivered notification can never be re-detected.
MESSAGE_PREFIX = "[PROMPT from "

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m|\x1b\].*?\x07")

# AskUserQuestion UI blocks (moved from server.py — single source of truth).
# Format: ☐ Header\n\nQuestion?\n\n❯ 1. Label\n     Description\n  2. Label...
# Multi-tab format: ←  ☐ Tab1  ☐ Tab2  ✔ Submit  →\n\nQuestion?...
ASK_PATTERN = re.compile(
    r"☐\s+(\S+)"  # ☐ followed by first word only (active tab name)
    r".*?\n\s*\n"  # Rest of header line + blank line
    r"((?:.+\n)+?)"  # Question text (one or more lines, non-greedy)
    r"\s*\n"  # Blank line before options
    r"((?:[❯\s]+\d+\.\s+.+\n(?:\s{3,}.+\n)?)+)",  # Options block
    re.MULTILINE | re.DOTALL,
)

# Simple format without ☐ header (e.g. "Ready to submit?\n\n❯ 1. Submit")
ASK_PATTERN_SIMPLE = re.compile(
    r"\n([^\n☐❯]+\?)\s*\n"  # Question ending with ? (no ☐ or ❯)
    r"\s*\n"  # Blank line
    r"((?:[❯\s]+\d+\.\s+.+\n(?:\s{3,}.+\n)?)+)",  # Options block
    re.MULTILINE,
)

# Anchors and liveness footers observed in real captures (the test fixtures
# embed those captures verbatim). A LIVE dialog ends the visible screen at
# its hint footer; a *quoted* dialog (a pane displaying a capture) has its
# own input box / status bar below, so the ends-with check fails.
_PLAN_ANCHOR = "ready to execute. Would you like to proceed?"
_PLAN_QUESTION_RE = re.compile(r"Would you like to\s+proceed\?")
_PERMISSION_QUESTION_RE = re.compile(r"Do you want\b[\s\S]{0,160}?\?")

# The resume dialog (#905). Claude Code shows this on `--resume` of a
# conversation past its age + token thresholds, which is every recovery from a
# stranded session and — once `agentwire restart` (#871 item 4) resumes in
# place — a routine path rather than an incident-only one.
#
# The anchor is the dialog's body sentence, verbatim from the shipped binary
# (`strings claude | grep "Resuming the full session"`), not from a screenshot:
# the title line above it interpolates age and token count, and the option
# labels could be reworded, but this sentence is one string literal.
#
# \s+ between every word, like every other pattern in this module. That
# sentence renders on a 122-column line, so on the 80- and 64-column panes
# this fleet actually runs it MUST wrap, and a line break lands inside the
# anchor. Locating it with a plain `str.index` on the un-normalized capture
# raised ValueError at 54 of 101 widths between 40 and 140 — and the sweep's
# pane loop had no per-pane guard, so that single raise abandoned every
# remaining pane and the marker GC, which limits_cli's stage isolation then
# swallowed. One unluckily-sized pane silently disabled ALL prompt routing
# fleet-wide, every tick, for as long as the dialog stayed up.
_RESUME_ANCHOR_RE = re.compile(r"We\s+recommend\s+resuming\s+from\s+a\s+summary\.")
# Used to end the options region, so the hint footer isn't swallowed as the
# last option's description. Same wrap tolerance, for the same reason.
_RESUME_FOOTER_RE = re.compile(r"Enter\s+to\s+confirm\s+·\s+Esc\s+to\s+cancel")
# Title: "This session is 2h 47m old and 233.6k tokens." — both values are
# formatted at render time, which is exactly why they stay OUT of the hash.
_RESUME_TITLE_RE = re.compile(
    r"This session is\s+(?P<age>.+?)\s+old and\s+(?P<tokens>\S+)\s+tokens\."
)
# Stable across every rendering of this dialog, so the content hash is too.
_RESUME_QUESTION = "Resume from summary, or resume the full session?"

_LIVE_TAIL = {
    "permission": re.compile(r"(Tab to\s+amend|ctrl\+e to\s+explain)\s*$"),
    "plan": re.compile(r"ctrl\+g to edit in\s+VS Code\s+·\s+\S+\.md\s*$"),
    "question": re.compile(r"Esc to cancel\s*$"),
    "resume": re.compile(r"Enter to confirm\s+·\s+Esc to cancel\s*$"),
}


@dataclass
class PromptInfo:
    kind: str  # "permission" | "plan" | "question"
    question: str
    options: list[dict] = field(default_factory=list)  # {number,label,description}
    summary: str = ""  # one-line context (tool/command for permissions)

    def content_hash(self) -> str:
        labels = " ".join(o.get("label", "") for o in self.options)
        raw = _normalize(f"{self.kind} {self.question} {labels}")
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_ask_options(options_block: str) -> list[dict]:
    """Parse numbered options from a dialog's options block."""
    options = []
    current_option = None

    for line in options_block.split("\n"):
        line = ANSI_PATTERN.sub("", line)
        option_match = re.match(r"[❯\s]*(\d+)\.\s+(.+)", line)
        if option_match:
            if current_option:
                options.append(current_option)
            current_option = {
                "number": int(option_match.group(1)),
                "label": option_match.group(2).strip(),
                "description": "",
            }
        elif current_option and line.strip():
            current_option["description"] = line.strip()

    if current_option:
        options.append(current_option)

    return options


def _is_live(norm: str, kind: str) -> bool:
    return bool(_LIVE_TAIL[kind].search(norm))


def _detect_plan(clean: str, norm: str) -> "PromptInfo | None":
    if _PLAN_ANCHOR not in norm or not _is_live(norm, "plan"):
        return None
    q = _PLAN_QUESTION_RE.search(clean)
    options = parse_ask_options(clean[q.end():]) if q else []
    if not options:
        return None
    return PromptInfo(
        kind="plan", question="Would you like to proceed?", options=options
    )


def _detect_permission(clean: str, norm: str) -> "PromptInfo | None":
    if not _is_live(norm, "permission"):
        return None
    q = _PERMISSION_QUESTION_RE.search(clean)
    if not q:
        return None
    options = parse_ask_options(clean[q.end():])
    if not options:
        return None
    return PromptInfo(
        kind="permission",
        question=_normalize(q.group(0)),
        options=options,
        summary=_normalize(clean[max(0, q.start() - 250):q.start()])[-200:],
    )


def _detect_resume(clean: str, norm: str) -> "PromptInfo | None":
    """Claude Code's "resume from summary?" dialog (#905).

    The state this exists for is the nastiest one the fleet has hit: the agent
    process is running, so ``pane_current_command`` reports the agent and every
    liveness check passes, while the session does nothing and every message
    queues behind ``safe_deliver``'s live-menu refusal. Four sessions sat here
    for hours — one about four — and no surface reported it.

    The age and token count live in ``summary``, never in ``question``, so the
    content hash is stable: a dialog that redrew with a ticking age would
    otherwise look like a NEW prompt on every sweep and re-notify the parent
    every 60 seconds.
    """
    if not _is_live(norm, "resume"):
        return None
    # Located on `clean`, not tested on `norm` and then sliced from `clean`:
    # a wrap-insensitive membership test followed by a wrap-sensitive slice is
    # how this crashed at half of all plausible pane widths. The SLICE must
    # stay un-normalized — parse_ask_options reads line structure — so the
    # wrap tolerance belongs in the pattern.
    anchor = _RESUME_ANCHOR_RE.search(clean)
    if not anchor:
        return None
    # Stop at the hint footer, or it is parsed as the last option's
    # description ("3. Don't ask me again" / "Enter to confirm · Esc to...").
    footer = _RESUME_FOOTER_RE.search(clean, anchor.end())
    options = parse_ask_options(clean[anchor.end():footer.start() if footer else None])
    if not options:
        return None
    title = _RESUME_TITLE_RE.search(norm)
    summary = (
        f"session is {title.group('age')} old, {title.group('tokens')} tokens"
        if title else ""
    )
    return PromptInfo(
        kind="resume", question=_RESUME_QUESTION, options=options, summary=summary
    )


def _detect_question(clean: str, norm: str) -> "PromptInfo | None":
    if not _is_live(norm, "question"):
        return None
    match = ASK_PATTERN.search(clean) or ASK_PATTERN_SIMPLE.search(clean)
    if not match:
        return None
    if len(match.groups()) == 3:
        question = _normalize(match.group(2))
        options_start = match.start(3)
    else:
        question = _normalize(match.group(1))
        options_start = match.start(2)
    # Parse from the options block to the end of the screen so options that
    # render after a separator rule (e.g. "4. Chat about this") are kept.
    options = parse_ask_options(clean[options_start:])
    if not options:
        return None
    return PromptInfo(kind="question", question=question, options=options)


# =============================================================================
# Parent resolution
# =============================================================================

# pane_current_command values that indicate an agent runs in a pane.
# Claude Code panes report the node binary or a bare version string
# (e.g. "2.1.170"); pi panes report node.
_AGENT_COMMAND_RE = re.compile(r"^(node|claude|\d+\.\d+\.\d+\S*)$")

def _read_creator(session: str) -> "str | None":
    """The session recorded as creator at `agentwire new` time, if any."""
    creator = load_session_metadata(session).get("created_by")
    return creator if isinstance(creator, str) and creator else None


def pane_command(session: str, pane_index: int) -> str:
    """The pane's current command ('' on any error)."""
    try:
        result = _tmux([
            "display", "-t", f"{session}.{pane_index}",
            "-p", "#{pane_current_command}",
        ])
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def is_agent_pane(session: str, pane_index: int) -> bool:
    return bool(_AGENT_COMMAND_RE.match(pane_command(session, pane_index)))


def resolve_parent(
    session: str, pane_index: int, project_path: "str | None" = None
) -> "tuple[str, int] | None":
    """The (session, pane) that should be told about this pane's prompt.

    Precedence:
      1. Worker pane (index > 0) -> pane 0 of the same session.
      2. Creator recorded at `agentwire new` time (session metadata).
      3. `.agentwire.yml` `parent:` field (project_path's config).
      4. None -> human-only behavior, unchanged.

    Depth-1 and local-machine only. Never returns the source pane itself or
    a dead session. (Whether the target pane is safe to paste into is the
    delivery layer's job — see safe_deliver.)
    """
    if pane_index > 0:
        return (session, 0)

    bare = session.split("@")[0]
    creator = _read_creator(bare)
    if creator and creator != bare and _session_exists(creator):
        return (creator, 0)

    parent = _parent_from_config(project_path)
    if parent and parent != bare and _session_exists(parent):
        return (parent, 0)

    return None


def _parent_from_config(project_path: "str | None") -> "str | None":
    try:
        from .project_config import get_parent_from_config

        return get_parent_from_config(Path(project_path) if project_path else None)
    except Exception:
        return None


# =============================================================================
# Delivery
# =============================================================================


def screen_shows_live_menu(visible: str) -> bool:
    """True if a live select-menu/dialog appears to be on screen.

    Used as a pre-paste safety check: pasting text + Enter into a pane whose
    screen ends in a menu would CONFIRM the highlighted option. Deliberately
    broader than detect_prompt — quoted-dialog subtleties don't matter here,
    and a screen that merely *looks* like a menu defers delivery one cycle.
    """
    clean = ANSI_PATTERN.sub("", visible)
    norm = _normalize(clean)
    if not norm:
        return False
    if _usage_limit_dialog(visible):
        return True
    if norm.rstrip().endswith("Enter to confirm · Esc to cancel"):
        return True
    return any(rx.search(norm) for rx in _LIVE_TAIL.values())


# A Claude Code input box is bordered by horizontal-rule lines (runs of the
# U+2500 box-drawing char); the typed text sits between the last two rules.
_RULE_CHAR = "─"
_PROMPT_GLYPHS = "❯>"


def _is_rule_line(line: str) -> bool:
    s = line.strip()
    return len(s) >= 10 and set(s) == {_RULE_CHAR}


def input_box_content(visible: str) -> "str | None":
    """The text in the Claude Code input box, or None if it can't be located.

    The box is the region between the last two horizontal-rule lines; an empty
    box holds just the prompt glyph (``❯``). Returns the content with the glyph
    and surrounding whitespace stripped (``""`` for an empty box), or None when
    the screen has no parseable box (busy render, a live dialog replacing the
    box, a non-agent pane) — the conservative "defer" signal.
    """
    clean = ANSI_PATTERN.sub("", visible)
    lines = clean.split("\n")
    rules = [i for i, ln in enumerate(lines) if _is_rule_line(ln)]
    if len(rules) < 2:
        return None
    box = lines[rules[-2] + 1:rules[-1]]
    if not box:
        return None
    text = "\n".join(box).lstrip()
    if text[:1] not in _PROMPT_GLYPHS:
        return None  # no prompt glyph between the rules — not the input box
    return text[1:].strip()


# SGR (color/attribute) escape sequences, captured with tmux capture-pane -e.
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
# Non-SGR escapes that may survive an -e capture (OSC titles, cursor moves).
_NON_SGR_ESCAPES = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-ln-z]")


def _non_dim_lines(visible: str) -> list[str]:
    """Per-line text with dim-rendered (``ESC[2m``) characters removed.

    Walks an SGR-preserved capture tracking the dim/faint attribute (set by
    param ``2``; cleared by ``0``, empty, or ``22``) — the attribute carries
    across lines, matching terminal semantics. Ghost/autosuggest text is the
    dim content; whatever survives is what a human actually typed.
    """
    visible = _NON_SGR_ESCAPES.sub("", visible)
    lines: list[str] = []
    cur: list[str] = []
    dim = False
    pos = 0

    def emit(segment: str) -> None:
        for ch in segment:
            if ch == "\n":
                lines.append("".join(cur))
                cur.clear()
            elif not dim:
                cur.append(ch)

    for m in _SGR_RE.finditer(visible):
        emit(visible[pos:m.start()])
        for param in (m.group(1) or "0").split(";"):
            if param in ("", "0", "22"):
                dim = False
            elif param == "2":
                dim = True
        pos = m.end()
    emit(visible[pos:])
    lines.append("".join(cur))
    return lines


def input_box_content_sgr(visible: str) -> "str | None":
    """SGR-aware ``input_box_content``: dim-only ghost text reads as empty.

    *visible* must be an SGR-preserving capture (``capture-pane -e``). Claude
    Code renders ghost/autosuggest text inside the input box dim (``ESC[2m``)
    and human-typed drafts never dim, so content that is entirely dim is not a
    draft — the box is deliverable. Any non-dim content is returned as usual
    (defer). On any ambiguity (line-count mismatch, glyph itself dim, no
    parseable box) this degrades to the plain parse — never wider than it.
    """
    plain = input_box_content(visible)  # ANSI_PATTERN strips SGR: same parse
    if not plain:
        return plain  # None (defer) or "" (empty) — nothing to reclassify
    non_dim = _non_dim_lines(visible)
    plain_lines = ANSI_PATTERN.sub("", _NON_SGR_ESCAPES.sub("", visible)).split("\n")
    if len(non_dim) != len(plain_lines):
        return plain
    rules = [i for i, ln in enumerate(plain_lines) if _is_rule_line(ln)]
    if len(rules) < 2:
        return plain
    text = "\n".join(non_dim[rules[-2] + 1:rules[-1]]).lstrip()
    if not text or text[0] not in _PROMPT_GLYPHS:
        return plain  # prompt glyph rendered dim/odd — stay conservative
    return text[1:].strip()


def capture(target_session: str, target_pane: int = 0, escapes: bool = False) -> str:
    """Capture the live screen text of a pane (``escapes=True`` keeps SGR)."""
    return _capture(f"{target_session}.{target_pane}", escapes=escapes)


def prompt_is_empty(target_session: str, target_pane: int = 0) -> bool:
    """True iff the target's input box holds no uncommitted text.

    The one new building block for polite messaging: detects whether a human
    is mid-typing. Conservative by design — any non-empty content (a draft OR
    a busy-state placeholder like "Press up to edit queued messages") and any
    screen we can't parse as a clean empty box returns False (defer). A delayed
    message is fine; a clobbered human draft is not. Captures with SGR escapes
    so dim-rendered ghost/autosuggest text doesn't read as a draft (#669).
    """
    content = input_box_content_sgr(
        _capture(f"{target_session}.{target_pane}", escapes=True)
    )
    return content == ""


# Claude Code renders this in the input box while the agent is generating and the
# human has queued one or more messages (e.g. "Press up to edit queued messages").
# It is a BUSY-state placeholder, not a human draft. Matched loosely on purpose:
# if a future Claude Code reworded it, a non-match degrades to the SAFE default
# (treated as a real draft → defer with penalty), never to anything that pastes.
_QUEUED_PLACEHOLDER = re.compile(r"queued\s+messages?", re.IGNORECASE)


def is_queued_placeholder(content: str) -> bool:
    """True if *content* is Claude Code's queued-message busy placeholder.

    The inbox drain uses this to defer WITHOUT penalty (like ``target_busy``)
    when a session is generating with human-queued input, instead of burning the
    message toward dead-letter. It NEVER widens delivery: a placeholder is still
    non-empty, so ``prompt_is_empty`` stays False and we never paste into the box
    — only the dead-letter penalty decision changes.
    """
    return bool(content) and bool(_QUEUED_PLACEHOLDER.search(content))


def safe_deliver(target_session: str, target_pane: int, text: str) -> "tuple[bool, str]":
    """Deliver *text* to the target pane, refusing unsafe targets.

    Refusals (returned as (False, reason), retried by the next sweep tick):
      target_gone       session no longer exists
      target_parked     usage-limit parked — paste would corrupt the resume
      target_not_agent  pane runs a shell/editor — pasted text could EXECUTE
      target_dialog     pane shows its own live menu — Enter would answer it

    Delivery itself is session_ready.send_verified, keyed on the FULL
    whitespace-normalized message (#667) — never a fixed-length prefix, which
    collided across same-prefix ``[NOTIFY from …]`` pile-ups — so a silent
    tmux paste failure reports as not-delivered instead of being assumed sent.
    """
    if not _session_exists(target_session):
        return False, "target_gone"
    if _is_parked(target_session):
        return False, "target_parked"
    if not is_agent_pane(target_session, target_pane):
        return False, "target_not_agent"
    if screen_shows_live_menu(_capture(f"{target_session}.{target_pane}")):
        return False, "target_dialog"

    from .session_ready import send_verified

    ok = send_verified(target_session, text, pane_index=target_pane)
    return (ok, "delivered" if ok else "delivery_unverified")


def detect_prompt(visible: str) -> "PromptInfo | None":
    """Classify a live interactive prompt on a pane screen, if any.

    Returns None for: empty screens, the usage-limit dialog (owned by
    usage_limit.py), screens containing a routed notification (poison
    marker — prevents self-triggering loops), and quoted dialogs (liveness
    footer check fails — see _LIVE_TAIL).
    """
    clean = ANSI_PATTERN.sub("", visible)
    norm = _normalize(clean)
    if not norm:
        return None
    if MESSAGE_PREFIX in norm:
        return None
    if PARK_OPTION in norm or _usage_limit_dialog(visible):
        return None
    # _detect_resume runs FIRST: it is the most specific (an exact product
    # string), and this screen also satisfies _detect_question's liveness
    # footer, so leaving it last would let a coincidental "…?" line in the
    # scrollback above claim the dialog as a generic question.
    for detector in (_detect_resume, _detect_plan, _detect_permission, _detect_question):
        info = detector(clean, norm)
        if info:
            return info
    return None


# =============================================================================
# Markers (presence-based dedupe) + events
# =============================================================================


def _log_event(event: str, **fields) -> None:
    record = {"ts": _now().isoformat(), "event": event, **fields}
    append_event(EVENTS_FILE, record)


def marker_path(session: str, pane_index: int) -> Path:
    # Worktree session names contain "/" and nest a directory level, same as
    # usage_limit.state_path. The bash idle-handler guard tests the literal
    # "$HOME/.agentwire/prompt-router/${session}.${pane}.json" string — keep
    # these in lockstep.
    return STATE_DIR / f"{session}.{pane_index}.json"


def read_marker(session: str, pane_index: int) -> "dict | None":
    try:
        return json.loads(marker_path(session, pane_index).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_marker(session: str, pane_index: int, **fields) -> dict:
    marker = {"session": session, "pane": pane_index, **fields}
    _atomic_write(marker_path(session, pane_index), marker)
    return marker


def clear_marker(session: str, pane_index: int) -> None:
    try:
        marker_path(session, pane_index).unlink(missing_ok=True)
    except OSError:
        pass


def _marker_age(marker: dict, field_name: str = "detected_at") -> "timedelta | None":
    try:
        return _now() - datetime.fromisoformat(marker[field_name])
    except (KeyError, TypeError, ValueError):
        return None


def list_markers() -> list[dict]:
    if not STATE_DIR.exists():
        return []
    markers = []
    for path in sorted(STATE_DIR.rglob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("session") is not None:
            markers.append(data)
    return markers


# =============================================================================
# Routing
# =============================================================================


def build_message(session: str, pane_index: int, info: PromptInfo) -> str:
    """The notification a parent receives. Deliberately paraphrased: no `❯`,
    no menu-style option block, no dialog footer text — a delivered message
    must never look like a live dialog to the sweep (see MESSAGE_PREFIX
    poison + screen_shows_live_menu)."""
    labels = ", ".join(
        f"{o['number']}={o['label']}" for o in info.options if o.get("label")
    )
    deadline = (
        "~5 minutes (portal permission timeout)"
        if info.kind == "permission"
        else "none — blocks until answered"
    )
    summary = f" Context: {info.summary}" if info.summary else ""
    return (
        f"{MESSAGE_PREFIX}{session} pane {pane_index}] kind={info.kind} — "
        f"a session you are responsible for is blocked on an interactive prompt. "
        f"Question: {info.question}{summary} "
        f"Option keys: {labels}. Deadline: {deadline}. "
        f"Inspect FIRST: agentwire output -s '{session}' (MCP: pane_output/session_output). "
        f"Answer ONLY via: agentwire prompts answer -s '{session}' --pane {pane_index} "
        f"--expect {info.content_hash()} <key> — it verifies the same prompt is still "
        f"live before sending the key. Do not blanket-approve; if unsure, do nothing "
        f"(the human was also notified)."
    )


def _alert_no_parent(
    session: str, pane_index: int, info: PromptInfo, prior: "dict | None"
) -> "str | None":
    """Mirror a no-parent prompt to subscribed sessions (#982). Escalation kind.

    Earns the interrupt on both halves of the test: nothing but a human can
    answer a prompt with no parent to route to, and the session is stalled —
    burning wall-clock, and in the plan/permission cases holding a tool call
    open — for as long as it waits.

    **Its own stamp, and this is the whole bug.** The first version rode
    ``escalated_at``, described as "inheriting" the email's throttle. It does
    not inherit it, because that gate never closes on a machine without
    ``RESEND_API_KEY``: ``send_email`` RAISES ``EmailConfigError`` rather than
    returning a failed result, and :func:`_escalate_no_parent`'s handler returns
    the previous (absent) stamp. So the marker was rewritten every 60s sweep
    with ``escalated_at=None`` and the alert re-fired every tick — measured at
    5 escalations for 5 sweeps of ONE prompt, which over a 12h lease is ~720.
    That is precisely the failure mode the interrupt tier cannot survive: the
    over-production does not merely annoy, it retires the tier.

    ``alerted_at`` therefore stamps on successful ENQUEUE, which is a local
    write and cannot fail for the reason the email does. Same marker, same
    ``NO_PARENT_ESCALATE_TTL`` window, keyed on the same prompt hash (the
    caller only passes a *prior* whose hash matches), so a redraw is suppressed
    while a genuinely different question still alerts.

    Sent before the email for the same reason it needed its own stamp: on the
    common keyless machine the email is not a channel at all.
    """
    previous = (prior or {}).get("alerted_at")
    if previous:
        try:
            if _now() - datetime.fromisoformat(previous) < NO_PARENT_ESCALATE_TTL:
                return previous
        except (TypeError, ValueError):
            pass
    try:
        from . import fleet_alerts

        waiting = _marker_age(prior, "detected_at") if prior else None
        waited = (
            f" It has been waiting {int(waiting.total_seconds() // 60)} minutes."
            if waiting else ""
        )
        reached = fleet_alerts.emit_for(
            "blocked_pane_no_parent",
            f"{session} (pane {pane_index}) is blocked on a {info.kind} prompt "
            f"and is a root session — there is no parent to route it to, so "
            f"nothing will answer it automatically.{waited} Question: "
            f"{info.question} Answer with: agentwire prompts answer -s "
            f"'{session}' --pane {pane_index} --expect {info.content_hash()} <key>",
        )
    except Exception as exc:  # best-effort; never break the sweep
        _log_event(
            "no_parent_alert_failed", session=session, pane=pane_index, error=str(exc)
        )
        return previous
    if not reached:
        return previous
    _log_event("no_parent_alerted", session=session, pane=pane_index, to=reached)
    return _now().isoformat()


def _escalate_no_parent(
    session: str, pane_index: int, info: PromptInfo, prior: "dict | None"
) -> "str | None":
    """Email the owner about a ROOT session blocked with nowhere to route (#905).

    A root session has no parent by design, so ``status=no_parent`` was a
    terminal state: the marker sat there forever and no surface said anything.
    That is fine for a prompt a human is sitting in front of and wrong for an
    unattended one — a root orchestrator blocked on a product question is
    stalled until somebody happens to look at the pane.

    Follows the dead-letter escalation precedent rather than inventing a
    channel: shared Resend wiring, best-effort, never raises. Rate-limited by
    ``escalated_at`` on the marker — the first sighting emails, then at most
    once per :data:`NO_PARENT_ESCALATE_TTL` while the SAME prompt stays up
    (the sweep re-routes a no-parent prompt every 60s, so an unthrottled
    escalation would be 60 emails an hour). Returns the timestamp to record.
    """
    previous = (prior or {}).get("escalated_at")
    if previous:
        try:
            if _now() - datetime.fromisoformat(previous) < NO_PARENT_ESCALATE_TTL:
                return previous
        except (TypeError, ValueError):
            pass

    waiting = _marker_age(prior, "detected_at") if prior else None
    try:
        import socket

        from .channels.email import send_email

        options = ", ".join(
            f"{o['number']}={o['label']}" for o in info.options if o.get("label")
        )
        summary = f"\n**Context:** {info.summary}" if info.summary else ""
        waited = (
            f" It has been waiting {int(waiting.total_seconds() // 60)} minutes."
            if waiting else ""
        )
        body = (
            f"`{session}` (pane {pane_index}) on `{socket.gethostname()}` is blocked "
            f"on a **{info.kind}** prompt and has no parent session to route it to, "
            f"so nothing can answer it automatically.{waited}\n"
            f"\n**Question:** {info.question}{summary}\n"
            f"\n**Options:** {options}\n"
            f"\nInspect:\n```\nagentwire output -s '{session}'\n```\n"
            f"\nAnswer (never raw send-keys — it verifies the same prompt is still "
            f"live first):\n```\nagentwire prompts answer -s '{session}' "
            f"--pane {pane_index} --expect {info.content_hash()} <key>\n```\n"
        )
        result = send_email(
            subject=f"[agentwire] {session} is blocked on a {info.kind} prompt "
                    f"with no parent",
            body=body,
        )
        ok = bool(getattr(result, "success", False))
    except Exception as exc:  # escalation is best-effort; never break the sweep
        _log_event(
            "no_parent_escalate_failed",
            session=session, pane=pane_index, error=str(exc),
        )
        return previous
    _log_event(
        "no_parent_escalated",
        session=session, pane=pane_index, kind=info.kind, ok=ok,
    )
    return _now().isoformat()


def route_prompt(
    session: str,
    pane_index: int,
    info: PromptInfo,
    source: str = "sweep",
    project_path: "str | None" = None,
) -> "str | None":
    """Resolve the parent and deliver the notification. Never raises.

    Writes a marker either way: delivered markers dedupe future sweeps,
    deferred/no-parent markers make retries cheap and keep the idle-handler
    reap guard active while the pane is blocked. Returns the parent session
    name when delivery succeeded, else None.
    """
    try:
        content_hash = info.content_hash()
        parent = resolve_parent(session, pane_index, project_path)
        if parent is None:
            # Same prompt as last sweep -> keep the ORIGINAL detected_at. A
            # no-parent marker is rewritten every tick (nothing sets
            # notified_at, so the sweep never short-circuits), and refreshing
            # the timestamp each pass would make a pane blocked for four hours
            # read as four seconds old to anything measuring the wait.
            prior = read_marker(session, pane_index)
            if not prior or prior.get("hash") != content_hash:
                prior = None
            escalated_at = _escalate_no_parent(session, pane_index, info, prior)
            # Two channels, two stamps, deliberately (#982). They cannot share
            # one: the email's gate only closes on a SUCCESSFUL send, and on a
            # machine with no RESEND_API_KEY there is never one — which left the
            # fleet alert re-firing every 60s sweep when it rode that stamp.
            alerted_at = _alert_no_parent(session, pane_index, info, prior)
            write_marker(
                session, pane_index,
                kind=info.kind, question=info.question,
                hash=content_hash, source=source,
                parent=None, status="no_parent",
                detected_at=(prior or {}).get("detected_at") or _now().isoformat(),
                notified_at=None,
                escalated_at=escalated_at,
                alerted_at=alerted_at,
            )
            _log_event("no_parent", session=session, pane=pane_index, kind=info.kind)
            return None

        target_session, target_pane = parent
        delivered, reason = safe_deliver(
            target_session, target_pane, build_message(session, pane_index, info)
        )
        write_marker(
            session, pane_index,
            kind=info.kind, question=info.question,
            hash=content_hash, source=source,
            parent=target_session, status=reason,
            detected_at=_now().isoformat(),
            notified_at=_now().isoformat() if delivered else None,
        )
        _log_event(
            "prompt_routed" if delivered else "route_deferred",
            session=session, pane=pane_index, kind=info.kind,
            parent=target_session, status=reason,
        )
        return target_session if delivered else None
    except Exception as exc:  # routing must never break a caller
        _log_event("route_failed", session=session, pane=pane_index, error=str(exc))
        return None


def notify_permission_request(session: str, pane_index: int, data: dict) -> "str | None":
    """Hook-path entry: the portal received a PermissionRequest POST.

    Builds the PromptInfo from the hook payload (no pane parsing needed) and
    routes it. Sync + best-effort; the server calls this in an executor.
    """
    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input") or {}

    # ExitPlanMode fires the PermissionRequest hook too (live-verified
    # 2026-06-11) but renders the plan-approval dialog, whose options differ
    # from a permission dialog's — mirror what's actually on screen.
    if tool_name == "ExitPlanMode":
        plan = str(tool_input.get("plan", ""))[:300]
        info = PromptInfo(
            kind="plan",
            question="Plan approval: Would you like to proceed?",
            options=[
                {"number": 1, "label": "Yes, and use auto mode", "description": ""},
                {"number": 2, "label": "Yes, manually approve edits", "description": ""},
                {"number": 3, "label": "No", "description": ""},
                {"number": 4, "label": "Tell Claude what to change", "description": ""},
            ],
            summary=plan,
        )
        return route_prompt(session, pane_index, info, source="hook")

    # AskUserQuestion fires the hook in prompted sessions too (drill-verified
    # 2026-06-11) and the payload carries the question + options — mirror the
    # on-screen dialog. (The screen adds trailing "Type something." / "Chat
    # about this" options; the marker bridges the hash, kinds match.)
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions") or []
        first = questions[0] if isinstance(questions, list) and questions else {}
        options = [
            {
                "number": i + 1,
                "label": str(o.get("label", "")),
                "description": str(o.get("description", "")),
            }
            for i, o in enumerate(first.get("options") or [])
            if isinstance(o, dict)
        ]
        info = PromptInfo(
            kind="question",
            question=str(first.get("question") or "AskUserQuestion dialog"),
            options=options,
        )
        return route_prompt(session, pane_index, info, source="hook")

    if tool_name == "Bash":
        detail = str(tool_input.get("command", ""))[:300]
        summary = f"run: {detail}" if detail else "run a command"
    elif tool_name in ("Edit", "Write"):
        summary = f"{tool_name.lower()} {tool_input.get('file_path', 'a file')}"
    else:
        detail = json.dumps(tool_input, sort_keys=True)[:300]
        summary = f"use {tool_name} {detail}".strip()
    info = PromptInfo(
        kind="permission",
        question=f"Claude wants to {summary}",
        options=[
            {"number": 1, "label": "allow", "description": ""},
            {"number": 2, "label": "allow always", "description": ""},
            {"number": 3, "label": "deny (Escape also denies)", "description": ""},
        ],
    )
    return route_prompt(session, pane_index, info, source="hook")


# =============================================================================
# Guarded answer (compare-and-send)
# =============================================================================


def answer(
    session: str, pane_index: int, expect_hash: str, keys: list[str]
) -> "tuple[bool, str]":
    """Send *keys* to the pane only if the expected prompt is still live.

    This is the race guard: a human may have answered via the portal (or the
    prompt may have expired) between notification and the parent's answer —
    a stray keystroke would land in the child's input box, and a stray
    Escape would abort its in-flight turn. First answer wins; the loser
    no-ops here.
    """
    visible = _capture(f"{session}.{pane_index}")
    info = detect_prompt(visible)
    if info is None:
        clear_marker(session, pane_index)
        return False, "no live prompt on the pane (already answered or gone)"
    if info.content_hash() != expect_hash:
        # Hook-routed permission notifications carry a payload-derived hash
        # that can't be recomputed from the screen; the marker bridges the
        # two — same expected hash, same kind, a live prompt of that kind.
        marker = read_marker(session, pane_index)
        if not (
            marker
            and marker.get("hash") == expect_hash
            and marker.get("kind") == info.kind
        ):
            return False, (
                f"a DIFFERENT prompt is live (kind={info.kind}, "
                f"hash={info.content_hash()}) — inspect before answering"
            )
    for key in keys:
        _tmux(["send-keys", "-t", f"{session}.{pane_index}", key])
    clear_marker(session, pane_index)
    _log_event(
        "prompt_answered", session=session, pane=pane_index,
        kind=info.kind, keys=keys,
    )
    return True, f"sent {' '.join(keys)} to {session}.{pane_index}"


# =============================================================================
# Sweep + tick
# =============================================================================


def _router_config() -> "tuple[bool, set[str]]":
    """(enabled, excluded session names) from config.yaml."""
    try:
        from .config import get_config

        cfg = get_config().prompt_router
        return bool(cfg.enabled), set(cfg.exclude_sessions)
    except Exception:
        return True, set()


# Stuck-box sweeper backstop (#689). Machine-injected message headers whose
# pasted-but-unsubmitted drafts the sweep may flush with a bare Enter. Human
# drafts never start with these, so the backstop cannot submit a walked-away
# human's half-typed message. ``[Pasted text`` is Claude Code's placeholder for
# a large multi-line paste — the msg drain's coalesced blob renders exactly
# that way when its Enter is swallowed.
_MACHINE_HEADER_RE = re.compile(r"^\[(MSG from|NOTIFY from|PROMPT|IDLE|Pasted text)\b")
# Consecutive sweeps (~60s apart) the identical content must sit in the box
# before the flush fires — one sweep of settling covers a delivery in flight.
STUCK_BOX_SWEEPS = 2


def _stuck_box_path(session: str, pane_index: int) -> Path:
    return STATE_DIR / f"{session}.{pane_index}.stuckbox.json"


def _flush_stuck_box(session: str, pane_index: int, visible: str) -> bool:
    """Press Enter on a box stuck holding machine-injected text (#689).

    The last-resort healer behind the verified-submit and drain fixes: any
    paste-then-submit path that died between paste and Enter (crash, kill,
    swallowed keystroke that exhausted its budget) leaves a message rendered in
    the recipient's input box forever. Fires only when ALL of:

      - the box parses and holds non-empty, non-placeholder content,
      - the content starts with a machine-injected header (never a human draft),
      - no live select-menu is on screen (Enter would answer the dialog),
      - the identical content has sat there for ``STUCK_BOX_SWEEPS``
        consecutive sweeps (state persisted per-pane next to prompt markers).

    A mid-generation pane is deliberately NOT skipped (#698): Enter on a
    generating Claude Code pane QUEUES the draft (Esc interrupts, Enter never
    does), while the old ``esc to interrupt`` gate both refused the flush AND
    reset the counter every sweep — so a stuck box on a busy orchestrator was
    never rescued for as long as it kept working (the 2026-07-04 12:40
    incident, where the owner watched the message sit and pressed Enter
    manually). Frames we can't judge (unparseable box, live menu) HOLD the
    counter instead of resetting it, for the same reason.

    Never pastes — Enter only, so the #621 idempotency dedup keeps holding.
    """
    path = _stuck_box_path(session, pane_index)
    box = input_box_content(visible)
    if box is None or screen_shows_live_menu(visible):
        return False  # can't judge this frame / Enter unsafe — hold the counter
    if not box or not _MACHINE_HEADER_RE.match(box) or is_queued_placeholder(box):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        data = {}
    count = int(data.get("count", 0)) + 1 if data.get("content") == box else 1
    if count < STUCK_BOX_SWEEPS:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, {"content": box, "count": count})
        except OSError:
            pass
        return False

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _tmux(["send-keys", "-t", f"{session}.{pane_index}", "Enter"])
        _log_event(
            "stuck_box_flushed", session=session, pane=pane_index,
            content=box[:120],
        )
        return True
    except (subprocess.TimeoutExpired, OSError):
        return False


@dataclass
class PaneRef:
    """One tmux pane, as the sweep and the doctor check both see it."""
    session: str
    pane: int
    command: str
    path: str

    @property
    def is_agent(self) -> bool:
        return bool(_AGENT_COMMAND_RE.match(self.command.strip()))


def list_panes() -> "list[PaneRef] | None":
    """Every pane on the tmux server, or None if tmux couldn't be asked.

    ``list-panes -a`` deliberately, NOT ``-t <session>``: ``-a`` is server-wide
    and needs no per-session target, so it sidesteps the trap that bit the
    fleet's ad-hoc health checks — a bare ``list-panes -t <session>`` scopes to
    the ACTIVE WINDOW, making the first row the wrong pane whenever the agent
    isn't in it (``-s`` is what makes a targeted call session-wide). Pane
    indices are read from tmux, never assumed: base-index ships as 0 since
    #903, but windows created before that kept 1, so both are live at once.

    None (not ``[]``) when tmux is unreachable — "couldn't look" and "looked,
    found nothing" must not collapse, or a dead tmux server would read as a
    healthy fleet.
    """
    try:
        result = _tmux(
            ["list-panes", "-a", "-F",
             "#{session_name}\t#{pane_index}\t#{pane_current_command}\t#{pane_current_path}"]
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None

    panes = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        try:
            pane_index = int(parts[1])
        except ValueError:
            continue
        panes.append(PaneRef(parts[0], pane_index, parts[2], parts[3]))
    return panes


def blocked_panes() -> "list[dict] | None":
    """Agent panes sitting on an unanswered dialog, read-only (#905).

    The state nothing reported: the agent process is alive, so
    ``pane_current_command`` is the agent and every liveness check passes,
    while the pane shows a menu and the session does nothing. Only reading the
    pane content reveals it — which is what this does.

    Never routes, never delivers, never writes a marker; it reads the markers
    the sweep already wrote to say how long each has been waiting and whether
    anyone was told. ``status``:

    - ``unrouted`` — a live dialog with NO marker at all. The sweep hasn't run
      (watchdog down) or is excluding this session, so nobody has been told.
    - ``no_parent`` — routed nowhere; a root session, owner emailed.
    - ``waiting`` — a parent was notified and hasn't answered yet.
    - ``deferred`` — the parent pane wasn't safe to paste into.

    ``stuck`` marks the ones worth acting on: waiting longer than
    :data:`STUCK_PROMPT_AFTER`, or never routed at all.
    """
    panes = list_panes()
    if panes is None:
        return None
    _, excluded = _router_config()

    blocked = []
    for pane in panes:
        if not pane.is_agent:
            continue
        if _is_parked(pane.session):
            continue
        try:
            info = detect_prompt(_capture(f"{pane.session}.{pane.pane}"))
        except Exception as exc:
            # Contained like the sweep, and REPORTED for the same reason: a
            # crashing detector cannot be allowed to look like a healthy pane.
            # That is the blind spot this whole check exists to close, so the
            # check must be able to see its own.
            blocked.append({
                "session": pane.session, "pane": pane.pane,
                "kind": "unknown", "question": "", "summary": "",
                "status": "detector_error",
                "error": f"{type(exc).__name__}: {exc}",
                "parent": None, "excluded": pane.session in excluded,
                "waiting_minutes": None, "stuck": True,
            })
            continue
        if info is None:
            continue
        marker = read_marker(pane.session, pane.pane)
        age = _marker_age(marker) if marker else None
        if marker is None:
            status = "unrouted"
        elif marker.get("status") == "no_parent":
            status = "no_parent"
        elif marker.get("notified_at"):
            status = "waiting"
        else:
            status = "deferred"
        blocked.append({
            "session": pane.session,
            "pane": pane.pane,
            "kind": info.kind,
            "error": None,
            "question": info.question,
            "summary": info.summary,
            "status": status,
            "parent": (marker or {}).get("parent"),
            "excluded": pane.session in excluded,
            "waiting_minutes": int(age.total_seconds() // 60) if age else None,
            "stuck": status == "unrouted" or (
                age is not None and age > STUCK_PROMPT_AFTER
            ),
        })
    return blocked


def _sweep_pane(pane: PaneRef) -> "tuple[str, dict] | None":
    """One pane's share of the sweep: detect, dedupe by marker, route.

    Returns ``(bucket, entry)`` for the caller's result dict, or None when the
    pane contributes nothing. Split out of :func:`sweep` so a failure here can
    be contained to this pane — see the guard at the call site.
    """
    session, pane_index = pane.session, pane.pane
    visible = _capture(f"{session}.{pane_index}")
    info = detect_prompt(visible)
    marker = read_marker(session, pane_index)

    if info is None:
        if marker:
            clear_marker(session, pane_index)
        if _flush_stuck_box(session, pane_index, visible):
            return ("routed", {"session": session, "pane": pane_index, "kind": "stuck_box"})
        return None

    # A fresh hook-routed marker keeps the sweep off this pane entirely.
    # NOT hash- or kind-gated: the hook's hash derives from the tool
    # payload (never equals the screen hash), and ExitPlanMode arrives
    # via the hook but renders as a plan dialog. Only one dialog can be
    # live on a pane — a fresh hook marker means the portal owns it.
    if (
        marker
        and marker.get("source") == "hook"
        and (_marker_age(marker) or timedelta(0)) < HOOK_MARKER_TTL
    ):
        return ("active", {"session": session, "pane": pane_index, "kind": info.kind})

    if marker and marker.get("hash") == info.content_hash():
        if marker.get("notified_at"):
            age = _marker_age(marker, "notified_at")
            if age is not None and age < RENOTIFY_TTL:
                return ("active", {"session": session, "pane": pane_index, "kind": info.kind})
        # deferred (or TTL-expired) -> try again

    parent = route_prompt(session, pane_index, info, project_path=pane.path)
    entry = {"session": session, "pane": pane_index, "kind": info.kind, "parent": parent}
    return ("routed" if parent else "deferred", entry)


def sweep() -> dict:
    """Scan all agent panes for live interactive prompts and route them.

    Marker lifecycle per pane:
      no prompt on screen      -> clear any marker (answered/gone; identical
                                  future prompts will re-notify)
      same prompt, delivered   -> silent until RENOTIFY_TTL, then re-deliver
      same prompt, deferred    -> retry delivery
      different prompt         -> route fresh
      hook-source permission   -> sweep stays out while the marker is fresh
                                  (the portal owns that prompt's lifecycle)
    """
    enabled, excluded = _router_config()
    if not enabled:
        return {"routed": [], "deferred": [], "active": [], "failed": []}
    panes = list_panes()
    if panes is None:
        return {"routed": [], "deferred": [], "active": [], "failed": []}

    buckets = {"routed": [], "deferred": [], "active": [], "failed": []}
    seen_panes = {(p.session, p.pane) for p in panes}
    for pane in panes:
        # Only Claude Code panes produce these dialogs; a vim/less pane
        # *displaying* dialog text must never match.
        if not pane.is_agent:
            continue
        if pane.session in excluded or _is_parked(pane.session):
            continue
        try:
            result = _sweep_pane(pane)
        except Exception as exc:
            # CONTAINMENT, and deliberately not silence. One pane must never
            # cost the fleet its prompt routing: before this, a raise here
            # abandoned every REMAINING pane and the marker GC below, and
            # limits_cli's stage isolation then swallowed the traceback and
            # substituted an empty result — so the watchdog looked healthy
            # while permission, plan and question routing were all dead
            # fleet-wide. #905's own detector crashed exactly that way on a
            # narrow pane.
            #
            # A bare `continue` would be the cure that is worse: it turns "the
            # detector crashed" into "this pane has no prompt", which is
            # indistinguishable from healthy and permanent — #885's failure
            # shape with different spelling. So it is logged per pane AND
            # returned under "failed", which is what `agentwire limits tick`
            # prints and what the JSON consumers read.
            _log_event(
                "detect_failed",
                session=pane.session, pane=pane.pane,
                error=f"{type(exc).__name__}: {exc}",
            )
            buckets["failed"].append({
                "session": pane.session, "pane": pane.pane,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if result:
            bucket, entry = result
            buckets[bucket].append(entry)

    routed, deferred, active = (
        buckets["routed"], buckets["deferred"], buckets["active"])

    # GC markers whose pane no longer exists.
    for marker in list_markers():
        key = (marker.get("session"), marker.get("pane"))
        if key in seen_panes:
            continue
        age = _marker_age(marker)
        if age is None or age > MARKER_GC_TTL:
            clear_marker(*key)

    return {"routed": routed, "deferred": deferred, "active": active,
            "failed": buckets["failed"]}


def tick() -> dict:
    """One watchdog pass; rides `agentwire limits tick` (after the
    usage-limit sweep, so its dialog is parked before we ever look)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = STATE_DIR / ".tick.lock"
    with open(lock_file, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"skipped": "tick already running"}
        return sweep()
