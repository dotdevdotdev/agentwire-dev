"""Agentwire REPL application entry point.

- Print mode: one-shot SDK call, stream events, exit (single-shot stdout
  pipe — used by `agentwire workflow run`, scheduler tasks, `human_gate`
  seeds; never moves to Textual).
- Interactive: Textual TUI implemented in `agentwire/repl/textual_app.py`.

Slash commands live in `commands.py`; transcript persistence lives in
`persistence.py`. Streaming SDK plumbing (option building, rendering,
state machine) lives in `agentwire.sdk`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from agentwire.repl.context import load_session_context
from agentwire.sdk import (
    HEARTBEAT,
    StreamRenderState,
    build_options,
    heartbeat_iter,
    render_message,
)


BANNER = """\
╭─────────────────────────────────────────────────────────╮
│  agentwire repl — Anthropic SDK harness                 │
│  mode={mode} · model={model}
╰─────────────────────────────────────────────────────────╯
"""


def run_repl(
    mode: str = "bypass",
    model: str | None = None,
    print_prompt: str | None = None,
    system_prompt: str | None = None,
    session_name: str | None = None,
    resume: str | None = None,
    roles: list[str] | None = None,
    seed_message: str | None = None,
    view: str = "chat",
    cols: int = 3,
) -> int:
    """Run the REPL. Returns exit code.

    - Print mode (`-p PROMPT`): single-shot stdout pipe — one SDK call,
      stream events, exit. Used by `agentwire workflow run`, scheduler
      tasks, and `human_gate` seeds where a TUI would interfere.
    - Interactive (default `--view chat`): Textual TUI under
      `~/.agentwire/sessions/repl/<name>/` with full transcript persistence.
    - `--view fanout --cols N`: composite view fanning the master input out
      to N independent SDK clients side by side. Multi-generation A/B.

    `roles` overrides the role list from `.agentwire.yml` for this session;
    when None, project config wins.

    `seed_message` is sent as the first user turn before the prompt loop
    starts — used by the workflow human_gate runner to pre-load context.
    Has no effect in print mode (use `print_prompt` instead).
    """
    if print_prompt is not None:
        return asyncio.run(_run_print_mode(print_prompt, mode, model, system_prompt, roles))
    if view == "fanout":
        from agentwire.repl.views.fanout import run_fanout_repl
        return asyncio.run(run_fanout_repl(
            mode=mode, model=model, cols=cols,
            system_prompt=system_prompt, roles=roles,
        ))
    from agentwire.repl.textual_app import run_textual_repl
    return asyncio.run(run_textual_repl(
        mode=mode, model=model, system_prompt=system_prompt,
        session_name=session_name, resume=resume, roles=roles,
        seed_message=seed_message,
    ))


# -------- print mode (SDK-backed) --------


async def _run_print_mode(
    prompt: str,
    mode: str,
    model: str | None,
    system_prompt: str | None,
    roles: list[str] | None = None,
) -> int:
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
        print(f"error: claude-agent-sdk not installed: {e}", file=sys.stderr)
        return 1

    ctx = load_session_context(Path.cwd(), role_overrides=roles)
    if ctx.missing_roles:
        sys.stderr.write(f"[repl: roles not found: {', '.join(ctx.missing_roles)}]\n")
    options = build_options(
        ClaudeAgentOptions, mode, model, system_prompt, cwd=Path.cwd(),
        session_context=ctx,
    )

    # claude-agent-sdk refuses to nest — same CLAUDECODE unset as the workflow runner.
    saved_claudecode = os.environ.pop("CLAUDECODE", None)
    exit_code = 0
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            stream_state = StreamRenderState()
            async for message in heartbeat_iter(
                client.receive_response(), idle_timeout=5.0,
            ):
                if message is HEARTBEAT:
                    stream_state.heartbeat(sys.stdout)
                    continue
                try:
                    render_message(
                        message,
                        AssistantMessage=AssistantMessage,
                        UserMessage=UserMessage,
                        SystemMessage=SystemMessage,
                        ResultMessage=ResultMessage,
                        out=sys.stdout,
                        stream_state=stream_state,
                    )
                    if isinstance(message, ResultMessage) and getattr(message, "is_error", False):
                        exit_code = 1
                except Exception as exc:
                    print(f"[repl: render error: {exc}]", file=sys.stderr)
    except Exception as exc:
        print(f"[repl: {type(exc).__name__}: {exc}]", file=sys.stderr)
        return 1
    finally:
        if saved_claudecode is not None:
            os.environ["CLAUDECODE"] = saved_claudecode
    return exit_code


def _persist_sdk_message(
    message: Any,
    transcript: Any,
    AssistantMessage: Any,
    UserMessage: Any,
    SystemMessage: Any,
    ResultMessage: Any,
) -> None:
    """Translate one SDK message to event shape and append to the transcript.

    Reuses `agentwire.sdk.events` so REPL events and workflow events share
    vocabulary (portal history window can render both without a separate
    codepath).
    """
    try:
        from agentwire.sdk import events as ev
    except ImportError:
        return

    if isinstance(message, SystemMessage) and getattr(message, "subtype", None) == "init":
        for event in ev.translate_system_init(message):
            transcript.write_event(event)
    elif isinstance(message, AssistantMessage):
        transcript.write_event(ev.translate_assistant(message))
    elif isinstance(message, UserMessage):
        transcript.write_event(ev.translate_user(message))
    elif isinstance(message, ResultMessage):
        for event in ev.translate_result(message):
            transcript.write_event(event)
