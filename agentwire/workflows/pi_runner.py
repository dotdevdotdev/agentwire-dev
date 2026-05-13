"""Pi subprocess wrapper + JSONL event stream parser.

Runs `pi -p <prompt> --mode json --no-session ...` and parses the streaming
JSONL output into structured NodeResult. Event schema captured empirically
from pi 0.67.1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from agentwire.workflows.node import DEFAULT_TOOLS, ActionNode, NodeResult

CONFIG_PATH = Path.home() / ".agentwire" / "config.yaml"


# Pi's event types observed in `--mode json` output (pi 0.67.1):
#   session, agent_start, turn_start, message_start, message_end,
#   turn_end, agent_end
# Assistant messages carry role=assistant with content blocks; tool calls
# arrive as content blocks of type "tool_use" and results as "tool_result".


def _get_pi_api_key(provider: str) -> str:
    """Load API key for a pi provider from config, falling back to env."""
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except FileNotFoundError:
        data = {}
    if isinstance(data, dict):
        provider_cfg = (data.get("pi") or {}).get("providers", {}).get(provider, {})
        key = provider_cfg.get("api_key", "")
    else:
        key = ""
    env_var = f"{provider.upper().replace('-', '_')}_API_KEY"
    return key or os.environ.get(env_var, "")


def _extract_final_assistant_text(events: list[dict]) -> str:
    """Pick the last assistant message's text content."""
    for event in reversed(events):
        if event.get("type") != "message_end":
            continue
        message = event.get("message", {})
        if message.get("role") != "assistant":
            continue
        parts: list[str] = []
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "".join(parts).strip()
        if text:
            return text
    return ""


def _extract_tool_calls(events: list[dict]) -> list[dict]:
    """Collect tool_use blocks from assistant messages."""
    calls = []
    seen_ids: set[str] = set()
    for event in events:
        if event.get("type") not in ("message_end", "turn_end"):
            continue
        message = event.get("message", {})
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id", "")
            if tool_id in seen_ids:
                continue
            seen_ids.add(tool_id)
            calls.append({
                "id": tool_id,
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
    return calls


def _extract_token_usage(events: list[dict]) -> dict:
    """Sum usage across assistant message_end events."""
    total = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0}
    cost_total = 0.0
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message", {})
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage", {}) or {}
        for key in total:
            if key in usage and isinstance(usage[key], (int, float)):
                total[key] += usage[key]
        cost = usage.get("cost", {}) or {}
        if isinstance(cost.get("total"), (int, float)):
            cost_total += cost["total"]
    total["cost"] = cost_total
    return total


def build_pi_command(node: ActionNode, pi_binary: str = "pi") -> list[str]:
    """Compose the pi CLI invocation for a node.

    Separated so tests can assert on the command without running pi.
    """
    cmd = [
        pi_binary,
        "-p", node.prompt,
        "--provider", node.provider,
        "--model", node.model,
        "--thinking", node.thinking,
        "--mode", "json",
        "--no-session",
    ]
    # Empty tools list means "pi's default tools" — use DEFAULT_TOOLS rather
    # than --no-tools, matching the pre-Phase-6 default behaviour when
    # workflows didn't explicitly declare a tool list.
    tools = node.tools if node.tools else list(DEFAULT_TOOLS)
    cmd.extend(["--tools", ",".join(tools)])
    return cmd


def run_node(
    node: ActionNode,
    workflow_cwd: str | None = None,
    event_log_path: Path | None = None,
) -> NodeResult:
    """Execute one node. Synchronous. Streams JSONL to `event_log_path` if provided."""
    errors = node.validate()
    if errors:
        return NodeResult(
            node_id=node.id,
            status="failure",
            final_text="",
            error="; ".join(errors),
        )

    if shutil.which("pi") is None:
        return NodeResult(
            node_id=node.id,
            status="failure",
            final_text="",
            error="pi binary not found on PATH. "
                  "Install: npm install -g @mariozechner/pi-coding-agent",
        )

    cmd = build_pi_command(node)

    env = {**os.environ, **node.extra_env}
    api_key = _get_pi_api_key(node.provider)
    if api_key:
        env_var = f"{node.provider.upper().replace('-', '_')}_API_KEY"
        env[env_var] = api_key

    cwd = node.workdir or workflow_cwd or os.getcwd()

    events: list[dict] = []
    started_at = time.monotonic()

    log_file = None
    if event_log_path is not None:
        event_log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = event_log_path.open("w")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            text=True,
        )

        try:
            stdout, stderr = proc.communicate(timeout=node.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            duration_ms = int((time.monotonic() - started_at) * 1000)
            return NodeResult(
                node_id=node.id,
                status="timeout",
                final_text="",
                events=events,
                duration_ms=duration_ms,
                exit_code=-1,
                error=f"node exceeded timeout={node.timeout}s",
            )

        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if log_file:
                log_file.write(line + "\n")
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # Tolerate non-JSON lines (shouldn't occur in --mode json, but don't crash)
                continue

        duration_ms = int((time.monotonic() - started_at) * 1000)
        final_text = _extract_final_assistant_text(events)
        tool_calls = _extract_tool_calls(events)
        tokens_used = _extract_token_usage(events)

        error_msg: str | None = None
        if proc.returncode != 0:
            error_msg = (stderr or "").strip() or f"pi exited with code {proc.returncode}"

        # Pi emits stopReason=error on API failures while still exiting 0; surface it.
        for event in events:
            if event.get("type") == "message_end":
                msg = event.get("message", {})
                if msg.get("role") == "assistant" and msg.get("stopReason") == "error":
                    api_error = msg.get("errorMessage", "") or "pi api error"
                    if not error_msg:
                        error_msg = f"pi api error: {api_error}"

        status = "success" if proc.returncode == 0 and error_msg is None else "failure"

        return NodeResult(
            node_id=node.id,
            status=status,
            final_text=final_text,
            events=events,
            tool_calls=tool_calls,
            tokens_used=tokens_used,
            duration_ms=duration_ms,
            exit_code=proc.returncode,
            error=error_msg,
        )
    finally:
        if log_file:
            log_file.close()
