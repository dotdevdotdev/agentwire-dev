"""Human-in-the-loop workflow runner.

Pauses a workflow at the node, spawns an interactive REPL pre-loaded with
the upstream context, and feeds the human's final assistant text back as
`NodeResult.final_text` for downstream nodes.

Boundaries:
- Requires an interactive TTY. Scheduler / overnight-queue / piped runs fail
  fast with a clear error rather than hang waiting for input.
- Runs in-process via `agentwire.repl.app.run_repl(seed_message=...)`. No
  tmux pane spawn — keeps the contract simple (one process, one stdin) and
  avoids dragging in tmux as a workflow runtime dep.
- The seed turn is the rendered `node.prompt`, which the workflow templating
  layer has already filled with upstream node outputs. We don't add anything
  to it here — what the YAML author wrote is what the human sees.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agentwire.workflows.node import ActionNode, NodeResult

logger = logging.getLogger("agentwire.workflows.human_gate")


class HumanGateRunner:
    name = "human_gate"

    def __init__(self, on_event: Callable[[dict], None] | None = None):
        self.on_event = on_event

    def run(
        self,
        node: ActionNode,
        workflow_cwd: str | None = None,
        event_log_path: Path | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> NodeResult:
        if not sys.stdin.isatty():
            return NodeResult(
                node_id=node.id,
                status="failure",
                final_text="",
                error=(
                    "human_gate requires an interactive terminal; "
                    "stdin is not a TTY (scheduler/overnight runs cannot use this node)"
                ),
            )

        from agentwire.repl.app import run_repl
        from agentwire.repl import persistence

        session_name = f"workflow-{node.id}-{int(time.time())}"
        started = time.monotonic()

        sys.stdout.write(
            f"\n[human_gate · node={node.id} · session={session_name}]\n"
            "Pre-loading the prompt as your first turn. Reply, iterate, then "
            "exit (Ctrl+D or /exit) to release the workflow. Final assistant "
            "message becomes the node output.\n\n"
        )
        sys.stdout.flush()

        rc = run_repl(
            mode="bypass",
            session_name=session_name,
            seed_message=node.prompt,
        )

        # Read transcript for final assistant text + token totals.
        meta = persistence.load_session(session_name)
        events_path = persistence.DEFAULT_REPL_HOME / session_name / "events.jsonl"
        final_text = _extract_final_assistant_text(events_path)
        tokens_used = _extract_tokens(meta)
        duration_ms = int((time.monotonic() - started) * 1000)

        status = "success" if rc == 0 else "failure"
        error = None if rc == 0 else f"REPL exited with code {rc}"
        return NodeResult(
            node_id=node.id,
            status=status,
            final_text=final_text,
            tokens_used=tokens_used,
            duration_ms=duration_ms,
            exit_code=rc,
            error=error,
        )


def _extract_final_assistant_text(events_path: Path) -> str:
    """Walk the JSONL events, return the last assistant text content."""
    if not events_path.exists():
        return ""
    last = ""
    try:
        import json
        for line in events_path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Phase 2 PR 2 transcript shape: events translated by anthropic_events
            # surface assistant content as type="assistant" with text under
            # `text` or inside `content`.
            if event.get("type") == "assistant":
                t = event.get("text")
                if not t:
                    blocks = event.get("content") or []
                    for b in blocks:
                        if isinstance(b, dict) and b.get("type") == "text":
                            t = b.get("text", "")
                            break
                if t:
                    last = t
    except OSError:
        pass
    return last


def _extract_tokens(meta: dict | None) -> dict:
    if not meta:
        return {}
    return {
        "input_tokens": meta.get("total_input_tokens", 0) or 0,
        "output_tokens": meta.get("total_output_tokens", 0) or 0,
        "total_cost_usd": meta.get("total_cost_usd", 0.0) or 0.0,
    }
