"""Workflow node dataclasses.

ActionNode represents a single runner invocation (pi). OutputSpec declares
how to extract named variables from a node's result for use in downstream
node templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_TOOLS = ["read", "bash", "edit", "write"]
VALID_PI_TOOLS = {"read", "bash", "edit", "write", "grep", "find", "ls"}
# Backwards-compat alias — some callers still import VALID_TOOLS.
VALID_TOOLS = VALID_PI_TOOLS
VALID_THINKING = {"off", "minimal", "low", "medium", "high", "xhigh"}
VALID_OUTPUT_SOURCES = {"text", "regex", "jsonpath"}


@dataclass
class OutputSpec:
    """How to extract one named variable from a node's final output."""

    name: str
    source: str            # "text" | "regex" | "jsonpath"
    pattern: str = ""      # regex pattern, jsonpath expr, or fence marker for text
    required: bool = True  # if False, extraction failure → value is None

    def validate(self, node_id: str) -> list[str]:
        errors: list[str] = []
        if not self.name or not self.name.strip():
            errors.append(f"node[{node_id}].outputs: each output needs a name")
        if self.source not in VALID_OUTPUT_SOURCES:
            errors.append(
                f"node[{node_id}].outputs[{self.name}].source={self.source!r} "
                f"not in {sorted(VALID_OUTPUT_SOURCES)}"
            )
        if self.source in ("regex", "jsonpath") and not self.pattern:
            errors.append(
                f"node[{node_id}].outputs[{self.name}] requires pattern "
                f"for source={self.source!r}"
            )
        return errors


@dataclass
class ActionNode:
    """A single runner invocation in a workflow (pi)."""

    id: str
    prompt: str

    # Which runner executes this node. Only `pi` is supported.
    runner: str = "pi"

    provider: str = "zai"
    model: str = "glm-5.1"
    # Empty list → pi applies DEFAULT_TOOLS.
    tools: list[str] = field(default_factory=list)

    # pi's thinking string: off|minimal|low|medium|high|xhigh.
    thinking: str = "medium"

    # Declared for YAML forward-compat; runner MVP does not use these yet.
    depends_on: list[str] = field(default_factory=list)
    when: str | None = None
    timeout: int = 300
    retries: int = 0
    retry_delay: int = 10
    on_error: str = "fail"
    on_error_goto: str | None = None

    workdir: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    outputs: list[OutputSpec] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Return a list of validation error strings (empty = valid)."""
        errors: list[str] = []
        if not self.id or not self.id.strip():
            errors.append("node.id is required")
        if not self.prompt or not self.prompt.strip():
            errors.append(f"node[{self.id}].prompt is required")
        seen_out: set[str] = set()
        for spec in self.outputs:
            errors.extend(spec.validate(self.id))
            if spec.name in seen_out:
                errors.append(f"node[{self.id}].outputs: duplicate name {spec.name!r}")
            seen_out.add(spec.name)

        # Runner validation — delayed import to avoid circular load (runners
        # package imports node.py at module load time).
        from agentwire.workflows.runners import available_runners
        valid_runners = available_runners()
        if self.runner not in valid_runners:
            errors.append(
                f"node[{self.id}].runner={self.runner!r} not in {valid_runners}"
            )

        # Runner-specific validation (pi is the only runner).
        if self.runner == "pi":
            if self.thinking not in VALID_THINKING:
                errors.append(
                    f"node[{self.id}].thinking={self.thinking!r} not in {sorted(VALID_THINKING)}"
                )
            bad_tools = [t for t in self.tools if t not in VALID_PI_TOOLS]
            if bad_tools:
                errors.append(
                    f"node[{self.id}].tools contains invalid: {bad_tools} "
                    f"(pi valid: {sorted(VALID_PI_TOOLS)})"
                )

        if self.on_error not in ("fail", "continue", "branch"):
            errors.append(
                f"node[{self.id}].on_error={self.on_error!r} "
                "must be one of fail|continue|branch"
            )
        return errors


@dataclass
class NodeResult:
    """Outcome of executing a single node."""

    node_id: str
    status: str  # "success" | "failure" | "timeout" | "skipped"
    final_text: str  # Final assistant message text
    events: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    tokens_used: dict = field(default_factory=dict)
    duration_ms: int = 0
    exit_code: int = 0
    attempts: int = 1  # how many runner invocations happened (1 = no retries)
    error: str | None = None
    runner: str = ""  # populated by the workflow loop from node.runner after run()
