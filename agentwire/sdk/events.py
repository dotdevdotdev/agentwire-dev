"""Translate claude-agent-sdk Message objects → pi-shape JSONL.

The workflow engine's event log (`<run_id>/nodes/<node_id>.events.jsonl`)
is consumed by `agentwire workflow show <id> --events` and portal UI, both
of which expect pi's event vocabulary. By translating here, those consumers
stay renderer-agnostic.

Pi event types (reference): session, agent_start, turn_start, message_start,
message_end, turn_end, agent_end. Pi tool_use / tool_result blocks live inside
message_end events under `message.content[]`.

Output extractors in `agentwire/workflows/outputs.py` read only
NodeResult.final_text — they don't touch the events JSONL. So correct
`final_text` assembly is the only hard requirement for output extraction
to keep working. Event JSONL parity is for display only.
"""

from __future__ import annotations

import time
from typing import Any


def _ts() -> float:
    return time.time()


def translate_system_init(message: Any) -> list[dict]:
    """SystemMessage(subtype=init) → session + agent_start."""
    data = getattr(message, "data", {}) or {}
    session_id = data.get("session_id") or data.get("sessionId") or ""
    model = data.get("model") or ""
    return [
        {"type": "session", "ts": _ts(), "session_id": session_id},
        {"type": "agent_start", "ts": _ts(), "model": model},
    ]


def _block_type(block: Any) -> str:
    """Detect content-block type from either an SDK dataclass or a dict/namespace.

    claude-agent-sdk uses dataclass blocks (TextBlock, ToolUseBlock, ThinkingBlock,
    ToolResultBlock) without a `type` attribute — the class identifies the role.
    Our unit tests use dicts/namespaces with `type` set. Handle both shapes.
    """
    if hasattr(block, "type"):
        return getattr(block, "type")
    if isinstance(block, dict) and "type" in block:
        return block.get("type", "")
    cls = type(block).__name__
    return {
        "TextBlock": "text",
        "ToolUseBlock": "tool_use",
        "ThinkingBlock": "thinking",
        "ToolResultBlock": "tool_result",
    }.get(cls, "")


def _block_attr(block: Any, name: str, default=None) -> Any:
    """Dual getter: attribute access for dataclasses, .get for dicts."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def translate_assistant(message: Any) -> dict:
    """AssistantMessage → message_end (role=assistant) with content blocks.

    Pi's `message_end` carries `message.content[]` with text / tool_use blocks
    preserved as dicts. We replicate that shape so `workflow show` renders
    identically regardless of which runner produced the run.
    """
    content_blocks: list[dict] = []
    for block in getattr(message, "content", []) or []:
        btype = _block_type(block)
        if btype == "text":
            content_blocks.append({"type": "text", "text": _block_attr(block, "text", "") or ""})
        elif btype == "tool_use":
            content_blocks.append({
                "type": "tool_use",
                "id": _block_attr(block, "id", "") or "",
                "name": _block_attr(block, "name", "") or "",
                "input": _block_attr(block, "input", {}) or {},
            })
        elif btype == "thinking":
            content_blocks.append({
                "type": "thinking",
                "thinking": _block_attr(block, "thinking", "") or "",
            })
    return {
        "type": "message_end",
        "ts": _ts(),
        "message": {
            "role": "assistant",
            "content": content_blocks,
            "model": getattr(message, "model", None),
        },
    }


def translate_user(message: Any) -> dict:
    """UserMessage carrying tool_result blocks → message_end (role=user)."""
    content = getattr(message, "content", None)
    content_blocks: list[dict]
    if isinstance(content, str):
        content_blocks = [{"type": "text", "text": content}]
    else:
        content_blocks = []
        for block in content or []:
            btype = _block_type(block)
            if btype == "tool_result":
                content_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": _block_attr(block, "tool_use_id", "") or "",
                    "content": _block_attr(block, "content", None),
                })
            elif btype == "text":
                content_blocks.append({"type": "text", "text": _block_attr(block, "text", "") or ""})
    return {
        "type": "message_end",
        "ts": _ts(),
        "message": {"role": "user", "content": content_blocks},
    }


def translate_result(message: Any) -> list[dict]:
    """ResultMessage → turn_end + agent_end carrying tokens + cost."""
    usage = getattr(message, "usage", None) or {}
    if not isinstance(usage, dict):
        usage = {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
        }
    cost = getattr(message, "total_cost_usd", 0) or 0
    return [
        {
            "type": "turn_end",
            "ts": _ts(),
            "usage": {
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "cost": {"total": float(cost)},
            },
        },
        {
            "type": "agent_end",
            "ts": _ts(),
            "duration_ms": getattr(message, "duration_ms", 0),
            "is_error": getattr(message, "is_error", False),
            "session_id": getattr(message, "session_id", ""),
        },
    ]


def extract_final_text_from_assistants(events: list[dict]) -> str:
    """Concatenate text from every assistant message_end event.

    Matches pi's behaviour in `pi_runner._extract_final_assistant_text` closely
    enough for output extractors (regex / jsonpath / text) to work identically.
    Pi picks the LAST assistant message's text; we concatenate ALL of them
    because SDK streaming produces multiple turns and users expect the full
    reasoning output in final_text. Tests lock both behaviours in.
    """
    parts: list[str] = []
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message", {}) or {}
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    return "".join(parts).strip()


def extract_tool_calls(events: list[dict]) -> list[dict]:
    """Collect unique tool_use blocks across all assistant message_end events."""
    calls: list[dict] = []
    seen: set[str] = set()
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message", {}) or {}
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []) or []:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            tid = block.get("id", "")
            if tid in seen:
                continue
            seen.add(tid)
            calls.append({
                "id": tid,
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
    return calls


def extract_tokens_used(events: list[dict]) -> dict:
    """Sum usage across turn_end events to produce pi-shaped tokens dict."""
    total = {"input": 0, "output": 0, "cost": 0.0}
    for event in events:
        if event.get("type") != "turn_end":
            continue
        usage = event.get("usage", {}) or {}
        total["input"] += usage.get("input", 0)
        total["output"] += usage.get("output", 0)
        cost = usage.get("cost", {}) or {}
        if isinstance(cost, dict):
            total["cost"] += float(cost.get("total", 0) or 0)
    return total
