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


# =============================================================================
# Parent resolution
# =============================================================================

# pane_current_command values that indicate an agent runs in a pane.
# Claude Code panes report the node binary or a bare version string
# (e.g. "2.1.170"); pi panes report node.
_AGENT_COMMAND_RE = re.compile(r"^(node|claude|\d+\.\d+\.\d+\S*)$")

_SESSIONS_META_DIR = Path.home() / ".agentwire" / "sessions"


def _read_creator(session: str) -> "str | None":
    """The session recorded as creator at `agentwire new` time, if any."""
    metadata_file = _SESSIONS_META_DIR / session.split("@")[0] / "metadata.json"
    try:
        metadata = json.loads(metadata_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    creator = metadata.get("created_by")
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


def safe_deliver(target_session: str, target_pane: int, text: str) -> "tuple[bool, str]":
    """Deliver *text* to the target pane, refusing unsafe targets.

    Refusals (returned as (False, reason), retried by the next sweep tick):
      target_gone       session no longer exists
      target_parked     usage-limit parked — paste would corrupt the resume
      target_not_agent  pane runs a shell/editor — pasted text could EXECUTE
      target_dialog     pane shows its own live menu — Enter would answer it

    Delivery itself is session_ready.send_verified (marker = the message's
    own [PROMPT ...] prefix line), so a silent tmux paste failure reports
    as not-delivered instead of being assumed sent.
    """
    if not _session_exists(target_session):
        return False, "target_gone"
    if _is_parked(target_session):
        return False, "target_parked"
    if not is_agent_pane(target_session, target_pane):
        return False, "target_not_agent"
    if screen_shows_live_menu(_capture(f"{target_session}.{target_pane}")):
        return False, "target_dialog"

    from .session_ready import derive_check_fragment, send_verified

    marker = derive_check_fragment(text)
    ok = send_verified(target_session, text, marker=marker or None)
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
    for detector in (_detect_plan, _detect_permission, _detect_question):
        info = detector(clean, norm)
        if info:
            return info
    return None


# =============================================================================
# Markers (presence-based dedupe) + events
# =============================================================================


def _log_event(event: str, **fields) -> None:
    record = {"ts": _now().isoformat(), "event": event, **fields}
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


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
            write_marker(
                session, pane_index,
                kind=info.kind, hash=content_hash, source=source,
                parent=None, status="no_parent",
                detected_at=_now().isoformat(), notified_at=None,
            )
            _log_event("no_parent", session=session, pane=pane_index, kind=info.kind)
            return None

        target_session, target_pane = parent
        delivered, reason = safe_deliver(
            target_session, target_pane, build_message(session, pane_index, info)
        )
        write_marker(
            session, pane_index,
            kind=info.kind, hash=content_hash, source=source,
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
        return {"routed": [], "deferred": [], "active": []}
    try:
        result = _tmux(
            ["list-panes", "-a", "-F",
             "#{session_name}\t#{pane_index}\t#{pane_current_command}\t#{pane_current_path}"]
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"routed": [], "deferred": [], "active": []}
    if result.returncode != 0:
        return {"routed": [], "deferred": [], "active": []}

    routed, deferred, active = [], [], []
    seen_panes = set()
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        session, pane_s, command, pane_path = parts
        try:
            pane_index = int(pane_s)
        except ValueError:
            continue
        seen_panes.add((session, pane_index))

        # Only Claude Code panes produce these dialogs; a vim/less pane
        # *displaying* dialog text must never match.
        if not _AGENT_COMMAND_RE.match(command.strip()):
            continue
        if session in excluded or _is_parked(session):
            continue

        info = detect_prompt(_capture(f"{session}.{pane_index}"))
        marker = read_marker(session, pane_index)

        if info is None:
            if marker:
                clear_marker(session, pane_index)
            continue

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
            active.append({"session": session, "pane": pane_index, "kind": info.kind})
            continue

        if marker and marker.get("hash") == info.content_hash():
            if marker.get("notified_at"):
                age = _marker_age(marker, "notified_at")
                if age is not None and age < RENOTIFY_TTL:
                    active.append({"session": session, "pane": pane_index, "kind": info.kind})
                    continue
            # deferred (or TTL-expired) -> try again

        parent = route_prompt(session, pane_index, info, project_path=pane_path)
        entry = {"session": session, "pane": pane_index, "kind": info.kind, "parent": parent}
        (routed if parent else deferred).append(entry)

    # GC markers whose pane no longer exists.
    for marker in list_markers():
        key = (marker.get("session"), marker.get("pane"))
        if key in seen_panes:
            continue
        age = _marker_age(marker)
        if age is None or age > MARKER_GC_TTL:
            clear_marker(*key)

    return {"routed": routed, "deferred": deferred, "active": active}


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
