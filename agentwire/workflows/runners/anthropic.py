"""Anthropic (claude-agent-sdk) runner for workflow nodes.

Uses subscription auth — the same credentials the `claude` CLI uses from
`~/.claude/.credentials.json`. Tool execution is owned by Claude Code itself;
this runner configures `allowed_tools` + `permission_mode="bypassPermissions"`
+ `setting_sources=["user"]` so PreToolUse hooks referenced from
`~/.claude/settings.json` fire automatically — including AgentWire's
damage-control hooks at `~/.agentwire/hooks/damage-control/*`. Pi's tool path
is not touched by any of this.

The runner is sync on the outside (to match `NodeRunner` Protocol) and async
on the inside (claude-agent-sdk is async-only). `asyncio.run()` bridges them.
workflows/runner.py has no event loop and nodes run sequentially, so there's
no nested-loop concern.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from agentwire.workflows.node import ActionNode, NodeResult
from agentwire.sdk import events as ev
from agentwire.sdk.capabilities import validate_node_settings
from agentwire.sdk.errors import classify as _classify

logger = logging.getLogger("agentwire.workflows.anthropic")


class AnthropicRunner:
    name = "anthropic"

    def __init__(self, on_event: Callable[[dict], None] | None = None):
        self.on_event = on_event

    def run(
        self,
        node: ActionNode,
        workflow_cwd: str | None = None,
        event_log_path: Path | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> NodeResult:
        return asyncio.run(
            self._run_async(
                node=node,
                workflow_cwd=workflow_cwd,
                event_log_path=event_log_path,
                on_event=on_event or self.on_event,
            )
        )

    async def _run_async(
        self,
        node: ActionNode,
        workflow_cwd: str | None,
        event_log_path: Path | None,
        on_event: Callable[[dict], None] | None,
    ) -> NodeResult:
        # Import here so the module loads cleanly even if claude-agent-sdk
        # is missing — the registry can still list "anthropic" and validation
        # still works. Only actual execution requires the SDK.
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                ResultMessage,
                SystemMessage,
                UserMessage,
            )
        except ImportError as e:
            return NodeResult(
                node_id=node.id,
                status="failure",
                final_text="",
                error=f"claude-agent-sdk not installed: {e}",
            )

        # Strict validation BEFORE we call the SDK — same table the parser uses.
        setting_errors = validate_node_settings(
            model=node.model,
            tools=node.tools,
            effort=node.effort,
            task_budget_tokens=node.task_budget_tokens,
            thinking=node.thinking_config,
            node_id=node.id,
        )
        if setting_errors:
            return NodeResult(
                node_id=node.id,
                status="failure",
                final_text="",
                error="; ".join(setting_errors),
            )

        options = self._build_options(node, ClaudeAgentOptions, workflow_cwd)

        events: list[dict] = []
        started = time.monotonic()
        log_file = None
        if event_log_path is not None:
            event_log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = event_log_path.open("w")

        def _emit(event_dict: dict) -> None:
            events.append(event_dict)
            if log_file:
                log_file.write(json.dumps(event_dict) + "\n")
                log_file.flush()
            if on_event:
                try:
                    on_event(event_dict)
                except Exception:
                    logger.exception("on_event callback raised")

        error_msg: str | None = None

        # claude-agent-sdk refuses to nest — if we're currently running inside
        # a Claude Code session (CLAUDECODE=1), the spawned `claude` subprocess
        # aborts with "cannot be launched inside another Claude Code session".
        # Temporarily unset it for the duration of the SDK call; restore after.
        saved_claudecode = os.environ.pop("CLAUDECODE", None)

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(node.prompt)
                async for message in client.receive_response():
                    try:
                        if isinstance(message, SystemMessage) and getattr(message, "subtype", None) == "init":
                            for e in ev.translate_system_init(message):
                                _emit(e)
                        elif isinstance(message, AssistantMessage):
                            _emit(ev.translate_assistant(message))
                        elif isinstance(message, UserMessage):
                            _emit(ev.translate_user(message))
                        elif isinstance(message, ResultMessage):
                            for e in ev.translate_result(message):
                                _emit(e)
                            if getattr(message, "is_error", False):
                                error_msg = getattr(message, "result", None) or "result_message reported is_error"
                    except Exception:
                        logger.exception("event translation failed for %s", type(message).__name__)
        except Exception as e:
            err_type = type(e).__name__
            error_msg = f"{_classify(err_type, str(e))}: {err_type}: {e}"
        finally:
            if log_file:
                log_file.close()
            if saved_claudecode is not None:
                os.environ["CLAUDECODE"] = saved_claudecode

        duration_ms = int((time.monotonic() - started) * 1000)
        final_text = ev.extract_final_text_from_assistants(events)
        tool_calls = ev.extract_tool_calls(events)
        tokens_used = ev.extract_tokens_used(events)

        status = "success" if error_msg is None else "failure"
        return NodeResult(
            node_id=node.id,
            status=status,
            final_text=final_text,
            events=events,
            tool_calls=tool_calls,
            tokens_used=tokens_used,
            duration_ms=duration_ms,
            exit_code=0,
            error=error_msg,
        )

    def _build_options(
        self,
        node: ActionNode,
        ClaudeAgentOptions: Any,
        workflow_cwd: str | None,
    ) -> Any:
        """Compose ClaudeAgentOptions from the node's Anthropic settings."""
        kwargs: dict[str, Any] = {
            "model": node.model,
            "permission_mode": "bypassPermissions",
            "setting_sources": ["user"],       # load ~/.claude/hooks — damage-control
            "include_partial_messages": True,
        }
        if node.tools:
            kwargs["allowed_tools"] = list(node.tools)
        if workflow_cwd:
            kwargs["cwd"] = workflow_cwd
        if node.workdir:
            kwargs["cwd"] = node.workdir
        if node.extra_env:
            kwargs["env"] = dict(node.extra_env)
        if node.max_thinking_tokens is not None:
            kwargs["max_thinking_tokens"] = node.max_thinking_tokens
        if node.max_budget_usd is not None:
            kwargs["max_budget_usd"] = node.max_budget_usd

        # effort: pass typed values directly; "xhigh" goes via extra_args since
        # SDK 0.1.43's typed enum doesn't include it yet.
        if node.effort is not None:
            if node.effort == "xhigh":
                extra = dict(kwargs.get("extra_args") or {})
                extra["effort"] = "xhigh"
                kwargs["extra_args"] = extra
            else:
                kwargs["effort"] = node.effort

        # thinking: pass through the appropriate config dataclass.
        cfg = node.thinking_config
        if cfg:
            kwargs["thinking"] = self._build_thinking_config(cfg)

        # task_budget_tokens: beta header + extra_args passthrough (Opus 4.7 only).
        if node.task_budget_tokens is not None:
            extra = dict(kwargs.get("extra_args") or {})
            extra["task-budget-tokens"] = str(node.task_budget_tokens)
            kwargs["extra_args"] = extra
            betas = list(kwargs.get("betas") or [])
            if "task-budgets-2026-03-13" not in betas:
                betas.append("task-budgets-2026-03-13")
            # `betas` field on ClaudeAgentOptions is typed to a narrow Literal;
            # only set it when the user explicitly opts into task_budget_tokens,
            # and surface a runtime TypeError as a validation miss rather than
            # hiding it.

        return ClaudeAgentOptions(**kwargs)

    @staticmethod
    def _build_thinking_config(cfg: dict) -> dict:
        """Return a dict matching claude-agent-sdk's TypedDict ThinkingConfig* shape.

        The SDK's ThinkingConfig{Adaptive,Enabled,Disabled} are TypedDicts, so
        we pass them as regular dicts with the required `type` key.
        """
        ttype = cfg.get("type")
        if ttype == "adaptive":
            out: dict[str, Any] = {"type": "adaptive"}
            if "display" in cfg:
                out["display"] = cfg["display"]
            return out
        if ttype == "enabled":
            return {"type": "enabled", "budget_tokens": int(cfg.get("budget_tokens", 0))}
        if ttype == "disabled":
            return {"type": "disabled"}
        raise ValueError(f"unknown thinking.type: {ttype!r}")
