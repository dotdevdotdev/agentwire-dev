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

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .usage_limit import (
    PARK_OPTION,
    _normalize,
)
from .usage_limit import detect_dialog as _usage_limit_dialog

STATE_DIR = Path.home() / ".agentwire" / "prompt-router"
EVENTS_FILE = Path.home() / ".agentwire" / "prompt-router-events.jsonl"

# A prompt the parent never answered re-notifies after this long.
RENOTIFY_TTL = timedelta(minutes=10)
# Hook-source permission markers suppress the sweep's permission detector
# for slightly longer than the portal's 300s wait.
HOOK_MARKER_TTL = timedelta(minutes=6)
# Markers whose pane vanished are garbage-collected after this long.
MARKER_GC_TTL = timedelta(minutes=30)

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
_LIVE_TAIL = {
    "permission": re.compile(r"(Tab to\s+amend|ctrl\+e to\s+explain)\s*$"),
    "plan": re.compile(r"ctrl\+g to edit in\s+VS Code\s+·\s+\S+\.md\s*$"),
    "question": re.compile(r"Esc to cancel\s*$"),
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
    for detector in (_detect_plan, _detect_permission, _detect_question):
        info = detector(clean, norm)
        if info:
            return info
    return None
