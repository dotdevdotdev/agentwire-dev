"""Workflow YAML loader + schema validator.

Supports `name`, `description`, `version`, `inputs` (CLI-supplied
variables), and a `nodes` map. Each node builds into an ActionNode with
optional `outputs` extraction specs. DAG dependencies via `depends_on`
are validated here (cycle detection) and executed by the runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentwire.workflows.node import ActionNode, OutputSpec

# Where workflow YAML files are discovered by `agentwire workflow list` / run.
# Order matters: first match wins.
# User overrides live in `~/.agentwire/workflows/defs/`. Bundled examples are
# resolved via `_repo_examples_dir()` at runtime (see discover_workflows).
DISCOVERY_DIRS = [
    Path.home() / ".agentwire" / "workflows" / "defs",
]


@dataclass
class InputSpec:
    """Workflow-level input: a CLI-supplied variable bound into the context."""

    name: str
    type: str = "string"     # "string" | "int" | "float" | "bool" | "json"
    required: bool = True
    default: Any = None
    description: str = ""

    def coerce(self, raw: str | Any) -> Any:
        """Turn a CLI --input value (always a string) into the declared type."""
        if raw is None:
            return None
        if self.type == "string":
            return str(raw)
        if self.type == "int":
            return int(raw)
        if self.type == "float":
            return float(raw)
        if self.type == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")
        if self.type == "json":
            import json as _json
            return _json.loads(raw) if isinstance(raw, str) else raw
        raise ValueError(f"input[{self.name}]: unknown type {self.type!r}")


@dataclass
class WorkflowDef:
    """Parsed workflow definition."""

    name: str
    nodes: list[ActionNode]
    description: str = ""
    version: int = 1
    inputs: list[InputSpec] = field(default_factory=list)
    source_path: Path | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("workflow.name is required")
        if not self.nodes:
            errors.append("workflow.nodes must contain at least one node")
        seen_ids: set[str] = set()
        for node in self.nodes:
            errors.extend(node.validate())
            if node.id in seen_ids:
                errors.append(f"duplicate node id: {node.id}")
            seen_ids.add(node.id)
        # depends_on references must resolve
        for node in self.nodes:
            for dep in node.depends_on:
                if dep not in seen_ids:
                    errors.append(
                        f"node[{node.id}].depends_on references unknown node: {dep}"
                    )
        # Cycle detection (only meaningful once refs resolve)
        if not errors:
            cycle = _find_cycle(self.nodes)
            if cycle:
                errors.append(f"dependency cycle detected: {' -> '.join(cycle)}")
        # on_error=branch requires on_error_goto pointing at a real node
        for node in self.nodes:
            if node.on_error == "branch":
                if not node.on_error_goto:
                    errors.append(
                        f"node[{node.id}].on_error=branch requires on_error_goto"
                    )
                elif node.on_error_goto not in seen_ids:
                    errors.append(
                        f"node[{node.id}].on_error_goto references unknown node: "
                        f"{node.on_error_goto}"
                    )
        # Duplicate input names
        seen_inputs: set[str] = set()
        for inp in self.inputs:
            if inp.name in seen_inputs:
                errors.append(f"duplicate input name: {inp.name}")
            seen_inputs.add(inp.name)
        return errors


_UNVISITED, _IN_PROGRESS, _DONE = 0, 1, 2


def _find_cycle(nodes: list[ActionNode]) -> list[str] | None:
    """Return the first cycle found as a list of node ids, or None if acyclic."""
    graph = {n.id: list(n.depends_on) for n in nodes}
    color = {nid: _UNVISITED for nid in graph}
    parent: dict[str, str | None] = {nid: None for nid in graph}

    def dfs(start: str) -> list[str] | None:
        stack = [start]
        while stack:
            nid = stack[-1]
            if color[nid] == _UNVISITED:
                color[nid] = _IN_PROGRESS
            advanced = False
            for dep in graph.get(nid, []):
                if color.get(dep) == _UNVISITED:
                    parent[dep] = nid
                    stack.append(dep)
                    advanced = True
                    break
                if color.get(dep) == _IN_PROGRESS:
                    # Reconstruct cycle path: dep ... nid -> dep
                    cycle = [dep]
                    cur: str | None = nid
                    while cur is not None and cur != dep:
                        cycle.append(cur)
                        cur = parent.get(cur)
                    cycle.append(dep)
                    return list(reversed(cycle))
            if not advanced:
                color[nid] = _DONE
                stack.pop()
        return None

    for nid in graph:
        if color[nid] == _UNVISITED:
            result = dfs(nid)
            if result:
                return result
    return None


def topological_sort(nodes: list[ActionNode]) -> list[ActionNode]:
    """Kahn's algorithm. Assumes validate() already ran (no cycles, deps resolve)."""
    by_id = {n.id: n for n in nodes}
    indegree = {n.id: len(n.depends_on) for n in nodes}
    dependents: dict[str, list[str]] = {n.id: [] for n in nodes}
    for n in nodes:
        for dep in n.depends_on:
            dependents[dep].append(n.id)

    # Use insertion order among tied nodes (YAML node ordering = authoring intent)
    ready = [n.id for n in nodes if indegree[n.id] == 0]
    ordered: list[ActionNode] = []
    while ready:
        nid = ready.pop(0)
        ordered.append(by_id[nid])
        for child in dependents[nid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return ordered


def _node_from_dict(
    node_id: str,
    data: dict,
    workflow_default_runner: str | None = None,
) -> ActionNode:
    """Build an ActionNode from a YAML node mapping.

    `workflow_default_runner` is the top-level `runner:` field on the workflow,
    used when a node doesn't declare its own. Falls back to ActionNode's hardcoded
    default ("pi") when neither is set.
    """
    if not isinstance(data, dict):
        raise ValueError(f"node[{node_id}] must be a mapping, got {type(data).__name__}")

    kwargs: dict = {"id": node_id, "prompt": data.get("prompt", "")}

    # Runner cascade: node-level → workflow-default → ActionNode default.
    if "runner" in data:
        kwargs["runner"] = str(data["runner"])
    elif workflow_default_runner is not None:
        kwargs["runner"] = str(workflow_default_runner)

    for key in (
        "provider", "model", "when", "on_error",
        "on_error_goto", "workdir", "effort",
    ):
        if key in data:
            kwargs[key] = data[key]

    # `thinking` is polymorphic: pi uses a short string ("off"|"medium"|...),
    # anthropic runner uses a mapping ({type: adaptive, ...}). Route accordingly.
    if "thinking" in data:
        raw = data["thinking"]
        if isinstance(raw, str):
            kwargs["thinking"] = raw
        elif isinstance(raw, dict):
            kwargs["thinking_config"] = dict(raw)
        else:
            raise ValueError(
                f"node[{node_id}].thinking must be a string (pi) or mapping (anthropic), "
                f"got {type(raw).__name__}"
            )

    if "tools" in data:
        tools = data["tools"]
        if not isinstance(tools, list):
            raise ValueError(f"node[{node_id}].tools must be a list")
        kwargs["tools"] = [str(t) for t in tools]

    if "max_thinking_tokens" in data:
        kwargs["max_thinking_tokens"] = int(data["max_thinking_tokens"])
    if "max_budget_usd" in data:
        kwargs["max_budget_usd"] = float(data["max_budget_usd"])
    if "task_budget_tokens" in data:
        kwargs["task_budget_tokens"] = int(data["task_budget_tokens"])

    if "depends_on" in data:
        deps = data["depends_on"]
        if isinstance(deps, str):
            deps = [deps]
        kwargs["depends_on"] = [str(d) for d in deps]

    for int_key in ("timeout", "retries", "retry_delay"):
        if int_key in data:
            kwargs[int_key] = int(data[int_key])

    if "extra_env" in data:
        env = data["extra_env"]
        if not isinstance(env, dict):
            raise ValueError(f"node[{node_id}].extra_env must be a mapping")
        kwargs["extra_env"] = {str(k): str(v) for k, v in env.items()}

    if "outputs" in data:
        outputs_raw = data["outputs"]
        if not isinstance(outputs_raw, list):
            raise ValueError(f"node[{node_id}].outputs must be a list")
        specs: list[OutputSpec] = []
        for idx, entry in enumerate(outputs_raw):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"node[{node_id}].outputs[{idx}] must be a mapping, "
                    f"got {type(entry).__name__}"
                )
            spec_name = entry.get("name")
            if not spec_name:
                raise ValueError(f"node[{node_id}].outputs[{idx}] missing 'name'")
            specs.append(OutputSpec(
                name=str(spec_name),
                source=str(entry.get("source", "text")),
                pattern=str(entry.get("pattern", "")),
                required=bool(entry.get("required", True)),
            ))
        kwargs["outputs"] = specs

    return ActionNode(**kwargs)


def _input_from_dict(input_name: str, data: Any) -> InputSpec:
    """Build an InputSpec. Accepts either a mapping or a bare type string."""
    if isinstance(data, str):
        return InputSpec(name=input_name, type=data)
    if not isinstance(data, dict):
        raise ValueError(
            f"input[{input_name}] must be a mapping or type string, "
            f"got {type(data).__name__}"
        )
    return InputSpec(
        name=input_name,
        type=str(data.get("type", "string")),
        required=bool(data.get("required", True)),
        default=data.get("default"),
        description=str(data.get("description", "")),
    )


def load_workflow(path: Path) -> WorkflowDef:
    """Load and parse a workflow YAML file. Does not validate."""
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: workflow root must be a mapping")

    name = data.get("name") or path.stem
    description = data.get("description", "")
    version = int(data.get("version", 1))
    workflow_default_runner = data.get("runner")

    raw_nodes = data.get("nodes", {})
    if not isinstance(raw_nodes, dict):
        raise ValueError(f"{path}: 'nodes' must be a mapping of id → node")

    nodes: list[ActionNode] = []
    for node_id, node_data in raw_nodes.items():
        nodes.append(_node_from_dict(
            str(node_id), node_data, workflow_default_runner=workflow_default_runner
        ))

    raw_inputs = data.get("inputs", {})
    inputs: list[InputSpec] = []
    if raw_inputs:
        if not isinstance(raw_inputs, dict):
            raise ValueError(f"{path}: 'inputs' must be a mapping of name → spec")
        for input_name, input_data in raw_inputs.items():
            inputs.append(_input_from_dict(str(input_name), input_data))

    return WorkflowDef(
        name=name,
        nodes=nodes,
        description=description,
        version=version,
        inputs=inputs,
        source_path=path,
    )


def _repo_examples_dir() -> Path:
    """Path to the bundled `agentwire/workflows/examples/` dir."""
    return Path(__file__).resolve().parent / "examples"


def discover_workflows() -> list[WorkflowDef]:
    """Find all workflow YAMLs in known discovery dirs."""
    search_dirs = [*DISCOVERY_DIRS, _repo_examples_dir()]
    found: dict[str, WorkflowDef] = {}
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for yaml_file in sorted(directory.glob("*.yaml")):
            try:
                wf = load_workflow(yaml_file)
            except Exception:
                continue
            # First match wins — user's ~/.agentwire dir overrides repo examples
            if wf.name not in found:
                found[wf.name] = wf
    return list(found.values())


def resolve_workflow(name_or_path: str) -> WorkflowDef:
    """Resolve a workflow by name or path. Raises FileNotFoundError if not found."""
    candidate = Path(name_or_path)
    if candidate.exists() and candidate.is_file():
        return load_workflow(candidate)

    for wf in discover_workflows():
        if wf.name == name_or_path:
            return wf

    raise FileNotFoundError(
        f"workflow {name_or_path!r} not found. "
        f"Searched: {[str(p) for p in [*DISCOVERY_DIRS, _repo_examples_dir()]]}"
    )
