"""Node runner registry.

Each runner implements `NodeRunner` — it takes an ActionNode and returns a
NodeResult. The workflow DAG executor resolves the runner per node via
`get_runner(node.runner)`, so the engine itself stays runner-agnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from agentwire.workflows.node import ActionNode, NodeResult


class NodeRunner(Protocol):
    """Execute one workflow node and return its result."""

    name: str

    def run(
        self,
        node: ActionNode,
        workflow_cwd: str | None = None,
        event_log_path: Path | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> NodeResult:
        ...


RUNNERS: dict[str, NodeRunner] = {}


def register_runner(runner: NodeRunner) -> None:
    RUNNERS[runner.name] = runner


def get_runner(name: str) -> NodeRunner:
    """Return the runner registered under `name`. Raises KeyError if unknown."""
    if name not in RUNNERS:
        raise KeyError(
            f"unknown runner: {name!r}. Available: {sorted(RUNNERS.keys())}"
        )
    return RUNNERS[name]


def available_runners() -> list[str]:
    return sorted(RUNNERS.keys())


def _register_builtins() -> None:
    from agentwire.workflows.runners.pi import PiRunner
    register_runner(PiRunner())


_register_builtins()
