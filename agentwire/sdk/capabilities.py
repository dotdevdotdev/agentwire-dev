"""Capability table for Anthropic-runner nodes — strict, fail-fast validation.

Consumed by both the workflow validator (parse-time) and the AnthropicRunner
(runtime). If the YAML declares a setting that's unsupported by the chosen
model, the error surfaces at `agentwire workflow validate` and
`scheduler board` load, before any node actually runs.

Rationale: silent "warn and drop unsupported settings" is worse at scale —
users who thought they enabled `effort: xhigh` and got free high-quality
runs are surprised when they learn it was ignored. Fail at parse time.
"""

from __future__ import annotations

# Claude tool names the Anthropic runner accepts (CamelCase).
# Pi uses lowercase — different set, different runner, deliberately isolated.
VALID_ANTHROPIC_TOOLS = {
    "Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch",
}

# Typed effort levels surfaced by claude-agent-sdk 0.1.43.
# `xhigh` is Opus 4.7-only and passed via extra_args — see validate_effort below.
SDK_EFFORT_LEVELS = {"low", "medium", "high", "max"}
OPUS_ONLY_EFFORTS = {"max", "xhigh"}
OPUS_47_ONLY_EFFORTS = {"xhigh"}

# Models that do NOT support the `effort` param at all.
MODELS_WITHOUT_EFFORT = {
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5",
}

TASK_BUDGET_MIN_TOKENS = 20000


def is_opus(model: str) -> bool:
    return model.startswith("claude-opus-")


def is_opus_47(model: str) -> bool:
    return model == "claude-opus-4-7"


def supports_budget_tokens_thinking(model: str) -> bool:
    """`thinking: {type: enabled, budget_tokens: N}` is removed on 4.6/4.7 family."""
    if model.startswith("claude-opus-4-7") or model.startswith("claude-opus-4-6"):
        return False
    if model.startswith("claude-sonnet-4-6"):
        return False
    return True


def validate_node_settings(
    *,
    model: str,
    tools: list[str] | None,
    effort: str | None,
    task_budget_tokens: int | None,
    thinking: dict | None,
    node_id: str,
) -> list[str]:
    """Return a list of validation error strings for an Anthropic-runner node.

    Empty list = valid. Called from both ActionNode.validate() and
    AnthropicRunner.run() so any gap in one is caught by the other.
    """
    errors: list[str] = []
    prefix = f"node[{node_id}]"

    if not model:
        errors.append(f"{prefix}: runner=anthropic requires model (full string, e.g. claude-opus-4-7)")

    # --- tool-name check: CamelCase only -----------------------------------
    for tool in tools or []:
        if tool not in VALID_ANTHROPIC_TOOLS:
            errors.append(
                f"{prefix}: anthropic runner expects CamelCase tools from "
                f"{sorted(VALID_ANTHROPIC_TOOLS)}, got {tool!r}"
            )

    # --- effort ------------------------------------------------------------
    if effort is not None:
        if effort not in SDK_EFFORT_LEVELS and effort != "xhigh":
            errors.append(
                f"{prefix}: effort={effort!r} not in "
                f"{sorted(SDK_EFFORT_LEVELS | {'xhigh'})}"
            )
        elif model and model in MODELS_WITHOUT_EFFORT:
            errors.append(
                f"{prefix}: effort param not supported on {model}, omit it"
            )
        elif effort in OPUS_47_ONLY_EFFORTS and not is_opus_47(model):
            errors.append(
                f"{prefix}: effort: {effort} requires claude-opus-4-7, got {model}"
            )
        elif effort in OPUS_ONLY_EFFORTS and not is_opus(model):
            errors.append(
                f"{prefix}: effort: {effort} requires claude-opus-*, got {model}"
            )

    # --- task_budget_tokens (beta, Opus 4.7 only) --------------------------
    if task_budget_tokens is not None:
        if not is_opus_47(model):
            errors.append(
                f"{prefix}: task_budget_tokens requires claude-opus-4-7, got {model}"
            )
        if task_budget_tokens < TASK_BUDGET_MIN_TOKENS:
            errors.append(
                f"{prefix}: task_budget_tokens minimum is "
                f"{TASK_BUDGET_MIN_TOKENS}, got {task_budget_tokens}"
            )

    # --- thinking: {type: enabled, budget_tokens: N} -----------------------
    if thinking and isinstance(thinking, dict):
        ttype = thinking.get("type")
        if ttype not in (None, "adaptive", "enabled", "disabled"):
            errors.append(
                f"{prefix}: thinking.type={ttype!r} must be one of "
                "adaptive|enabled|disabled"
            )
        if ttype == "enabled" and "budget_tokens" in thinking and model:
            if not supports_budget_tokens_thinking(model):
                errors.append(
                    f"{prefix}: budget_tokens removed on {model}, "
                    "use thinking: {type: adaptive} + effort instead"
                )

    return errors
