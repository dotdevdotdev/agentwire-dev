#!/usr/bin/env python3
"""Record what HAPPENED to a tool call, next to what the damage-control hook decided.

The PreToolUse audit row says "asked" or "allowed", never whether the human
approved the prompt, whether the call then ran, or whether Claude Code's own
classifier denied it. Those outcomes are the labels the rule tuning needs
(rule worked / friction / false positive), and they only reach hooks that fire
AFTER the decision: PostToolUse (it ran), PostToolUseFailure (it ran and
failed), PermissionDenied (auto mode refused it).

One row per event is appended to the same daily JSONL as the audit rows, keyed
by ``tool_use_id`` so the two halves join. Rows carry NO tool input or output
content, only identifiers, the result and timing: the command text is already
in the PreToolUse row, and tool output can hold anything.

Never blocks, never prints, always exits 0: a logging hook that can break a
tool call is worse than no log.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

RESULTS = {
    "PostToolUse": "executed",
    "PostToolUseFailure": "failed",
    "PermissionDenied": "denied",
}
_REASON_KEYS = ("reason", "message", "denial_reason", "error")


def _log_file() -> Path:
    base = os.environ.get("AGENTWIRE_DIR", os.path.expanduser("~/.agentwire"))
    log_dir = Path(base) / "logs" / "damage-control"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def build_row(payload: dict) -> dict:
    """The outcome row for one hook payload. Pure: no I/O."""
    event = payload.get("hook_event_name") or ""
    response = payload.get("tool_response")
    row = {
        "kind": "outcome",
        "timestamp": datetime.now().isoformat(),
        "event": event,
        "result": RESULTS.get(event, "unknown"),
        "tool_use_id": payload.get("tool_use_id"),
        "tool": payload.get("tool_name"),
        "conversation_id": payload.get("session_id"),
        "agent_id": payload.get("agent_id") or "main",
        "permission_mode": payload.get("permission_mode"),
        "cwd": payload.get("cwd"),
        # Discovery aid: which fields this Claude Code version actually sends.
        "payload_keys": sorted(payload.keys()),
    }
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        row["duration_ms"] = duration
    if isinstance(response, dict) and isinstance(response.get("interrupted"), bool):
        row["interrupted"] = response["interrupted"]
    if isinstance(payload.get("is_interrupt"), bool):
        row["interrupted"] = payload["is_interrupt"]
    if event == "PermissionDenied":
        for key in _REASON_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                row["reason"] = value[:200]
                break
    return row


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
        row = build_row(payload)
        with open(_log_file(), "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
