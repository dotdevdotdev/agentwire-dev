"""Tests for agentwire/sdk/capabilities.py.

One test per row of the capability table in docs/missions/anthropic-sdk-runner.md.
Keep it strict — silent validation failures are worse than loud ones.
"""

from __future__ import annotations

import pytest

from agentwire.sdk.capabilities import (
    TASK_BUDGET_MIN_TOKENS,
    validate_node_settings,
)


def _v(**kw):
    """Helper: call validator with defaults filled in."""
    kw.setdefault("model", "claude-opus-4-7")
    kw.setdefault("tools", None)
    kw.setdefault("effort", None)
    kw.setdefault("task_budget_tokens", None)
    kw.setdefault("thinking", None)
    kw.setdefault("node_id", "n")
    return validate_node_settings(**kw)


class TestHappyPath:
    def test_no_optional_settings(self):
        assert _v() == []

    def test_minimal_valid_combo(self):
        assert _v(effort="high") == []

    def test_adaptive_thinking(self):
        assert _v(thinking={"type": "adaptive"}) == []

    def test_adaptive_thinking_summarized(self):
        assert _v(thinking={"type": "adaptive", "display": "summarized"}) == []

    def test_disabled_thinking(self):
        assert _v(thinking={"type": "disabled"}) == []

    def test_task_budget_minimum(self):
        assert _v(task_budget_tokens=TASK_BUDGET_MIN_TOKENS) == []

    def test_camel_case_tools(self):
        assert _v(tools=["Read", "Write", "Bash", "Grep"]) == []


class TestEffortCapability:
    def test_effort_max_allowed_on_opus(self):
        assert _v(model="claude-opus-4-7", effort="max") == []
        assert _v(model="claude-opus-4-6", effort="max") == []

    def test_effort_max_rejected_on_sonnet(self):
        errs = _v(model="claude-sonnet-4-6", effort="max")
        assert any("effort: max requires claude-opus-*" in e for e in errs)

    def test_effort_xhigh_requires_opus_47(self):
        errs = _v(model="claude-opus-4-6", effort="xhigh")
        assert any("effort: xhigh requires claude-opus-4-7" in e for e in errs)

    def test_effort_xhigh_ok_on_opus_47(self):
        assert _v(model="claude-opus-4-7", effort="xhigh") == []

    def test_effort_rejected_on_haiku(self):
        errs = _v(model="claude-haiku-4-5", effort="low")
        assert any("effort param not supported on claude-haiku-4-5" in e for e in errs)

    def test_effort_rejected_on_sonnet_4_5(self):
        errs = _v(model="claude-sonnet-4-5", effort="medium")
        assert any("effort param not supported on claude-sonnet-4-5" in e for e in errs)

    def test_unknown_effort_value(self):
        errs = _v(effort="ludicrous")
        assert any("effort='ludicrous'" in e for e in errs)


class TestTaskBudget:
    def test_task_budget_requires_opus_47(self):
        errs = _v(model="claude-opus-4-6", task_budget_tokens=20000)
        assert any("task_budget_tokens requires claude-opus-4-7" in e for e in errs)

    def test_task_budget_requires_opus_47_not_sonnet(self):
        errs = _v(model="claude-sonnet-4-6", task_budget_tokens=30000)
        assert any("task_budget_tokens requires claude-opus-4-7" in e for e in errs)

    def test_task_budget_below_minimum(self):
        errs = _v(task_budget_tokens=19999)
        assert any(
            f"task_budget_tokens minimum is {TASK_BUDGET_MIN_TOKENS}" in e for e in errs
        )

    def test_task_budget_exactly_minimum(self):
        assert _v(task_budget_tokens=TASK_BUDGET_MIN_TOKENS) == []


class TestThinkingBudgetTokens:
    def test_enabled_budget_rejected_on_opus_47(self):
        errs = _v(
            model="claude-opus-4-7",
            thinking={"type": "enabled", "budget_tokens": 8000},
        )
        assert any("budget_tokens removed on claude-opus-4-7" in e for e in errs)

    def test_enabled_budget_rejected_on_opus_46(self):
        errs = _v(
            model="claude-opus-4-6",
            thinking={"type": "enabled", "budget_tokens": 8000},
        )
        assert any("budget_tokens removed on claude-opus-4-6" in e for e in errs)

    def test_enabled_budget_rejected_on_sonnet_46(self):
        errs = _v(
            model="claude-sonnet-4-6",
            thinking={"type": "enabled", "budget_tokens": 8000},
        )
        assert any("budget_tokens removed on claude-sonnet-4-6" in e for e in errs)

    def test_enabled_budget_allowed_on_pre_46_model(self):
        # Pre-4.6 models still accept budget_tokens — validator stays silent.
        errs = _v(
            model="claude-sonnet-4-5",
            thinking={"type": "enabled", "budget_tokens": 8000},
        )
        # No budget_tokens-specific error (sonnet-4-5 might have other issues
        # with effort, but thinking passes).
        assert not any("budget_tokens removed" in e for e in errs)


class TestToolNames:
    def test_lowercase_tool_rejected(self):
        errs = _v(tools=["read", "bash"])
        assert any("expects CamelCase" in e for e in errs)
        assert any("'read'" in e for e in errs)

    def test_unknown_tool_rejected(self):
        errs = _v(tools=["Teleport"])
        assert any("'Teleport'" in e for e in errs)

    def test_all_valid_tools_pass(self):
        assert _v(tools=["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch"]) == []


class TestMissingModel:
    def test_empty_model_errors(self):
        errs = _v(model="")
        assert any("runner=anthropic requires model" in e for e in errs)
