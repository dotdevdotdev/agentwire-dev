"""CLI handlers for `agentwire workflow *` subcommands.

Subcommands:
  list     - discover and print available workflows
  validate - parse + validate a workflow without running it
  run      - execute a workflow end-to-end
  history  - list past runs
  show     - inspect a past run (metadata, per-node events)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agentwire.workflows import storage
from agentwire.workflows.definitions import (
    discover_workflows,
    resolve_workflow,
)
from agentwire.workflows.runner import run_workflow

RUNS_DIR = Path.home() / ".agentwire" / "workflows" / "runs"


def _fmt_duration(ms: int | None) -> str:
    """Compact human-ish duration: 120ms / 3.2s / 1m04s."""
    if not ms:
        return "0ms"
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m{secs:02d}s"


def _fmt_started_at(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _parse_input_pairs(pairs: list[str] | None) -> tuple[dict[str, Any], list[str]]:
    """Parse --input key=value flags into a dict. Last wins on duplicates."""
    result: dict[str, Any] = {}
    errors: list[str] = []
    for pair in pairs or []:
        if "=" not in pair:
            errors.append(f"--input expects KEY=VALUE, got: {pair!r}")
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            errors.append(f"--input missing key: {pair!r}")
            continue
        result[key] = value
    return result, errors


def _load_input_file(path: str | None) -> tuple[dict[str, Any], list[str]]:
    if not path:
        return {}, []
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        return {}, [f"--input-file {path!r}: {e}"]
    if not isinstance(data, dict):
        return {}, [f"--input-file {path!r}: must be a JSON object"]
    return data, []


def cmd_workflow_list(args) -> int:
    """`agentwire workflow list` — discover available workflows."""
    workflows = discover_workflows()

    if getattr(args, "json", False):
        payload = [
            {
                "name": wf.name,
                "description": wf.description,
                "version": wf.version,
                "nodes": [n.id for n in wf.nodes],
                "source": str(wf.source_path) if wf.source_path else None,
            }
            for wf in workflows
        ]
        print(json.dumps(payload, indent=2))
        return 0

    if not workflows:
        print("No workflows found.")
        print("  Drop YAML files in ~/.agentwire/workflows/defs/ or see workflows/examples/.")
        return 0

    for wf in workflows:
        node_count = len(wf.nodes)
        desc = f" — {wf.description}" if wf.description else ""
        print(f"  {wf.name} ({node_count} node{'s' if node_count != 1 else ''}){desc}")

    return 0


def cmd_workflow_validate(args) -> int:
    """`agentwire workflow validate <name-or-path>` — check a workflow YAML."""
    try:
        workflow = resolve_workflow(args.workflow)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error parsing workflow: {e}", file=sys.stderr)
        return 1

    errors = workflow.validate()
    if errors:
        print(f"Workflow {workflow.name!r} has {len(errors)} validation error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"Workflow {workflow.name!r} is valid ({len(workflow.nodes)} node(s)).")
    return 0


def _make_verbose_printer() -> Callable[[dict], None]:
    """Return a per-event printer for --verbose runs.

    Anthropic runner events land here live via the NodeRunner `on_event`
    callback. Pi runner doesn't invoke the callback (see runners/pi.py),
    so pi nodes stay silent under --verbose.
    """
    def _print(ev: dict) -> None:
        etype = ev.get("type")
        nid = ev.get("node_id") or "?"
        if etype == "message_end":
            msg = ev.get("message") or {}
            role = msg.get("role")
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input") or {}
                    args = " ".join(f"{k}={v!r}" for k, v in list(inp.items())[:2])
                    print(f"  [{nid}] → tool_use {name} {args}")
                elif btype == "tool_result":
                    content = block.get("content", "")
                    is_err = block.get("is_error", False)
                    tag = "err" if is_err else "ok"
                    snippet = str(content)[:60].replace("\n", " ")
                    print(f"  [{nid}] ← tool_result ({tag}) {snippet}")
                elif btype == "text" and role == "assistant":
                    t = (block.get("text") or "").strip()
                    if t:
                        print(f"  [{nid}] ▓ {t[:80]}")
        elif etype == "turn_end":
            u = ev.get("usage") or {}
            print(f"  [{nid}] ✓ turn {u.get('input', 0)}+{u.get('output', 0)} tok")
        elif etype == "agent_end":
            ms = ev.get("duration_ms", 0)
            print(f"  [{nid}] ■ agent_end {ms/1000:.1f}s")
    return _print


def cmd_workflow_run(args) -> int:
    """`agentwire workflow run <name-or-path>` — execute a workflow."""
    try:
        workflow = resolve_workflow(args.workflow)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    errors = workflow.validate()
    if errors:
        print(f"Workflow {workflow.name!r} has validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    dry_run = getattr(args, "dry_run", False)

    # Collect inputs: --input-file first (base), then --input overrides.
    file_inputs, file_errors = _load_input_file(getattr(args, "input_file", None))
    cli_inputs, pair_errors = _parse_input_pairs(getattr(args, "input", None))
    input_errors = file_errors + pair_errors
    if input_errors:
        for err in input_errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1
    merged_inputs: dict[str, Any] = {**file_inputs, **cli_inputs}

    if getattr(args, "verbose", False):
        print(f"Running workflow {workflow.name!r} ({len(workflow.nodes)} node(s))...")
        if dry_run:
            print("  (dry-run)")
        if merged_inputs:
            print(f"  inputs: {merged_inputs}")

    result = run_workflow(
        workflow,
        runs_dir=RUNS_DIR,
        dry_run=dry_run,
        inputs=merged_inputs,
        on_event=_make_verbose_printer() if getattr(args, "verbose", False) else None,
    )

    if getattr(args, "json", False):
        print(json.dumps({
            "workflow": result.workflow,
            "run_id": result.run_id,
            "status": result.status,
            "duration_ms": result.duration_ms,
            "error": result.error,
            "nodes": [
                {
                    "id": r.node_id,
                    "runner": r.runner,
                    "status": r.status,
                    "final_text": r.final_text,
                    "duration_ms": r.duration_ms,
                    "attempts": r.attempts,
                    "tokens": r.tokens_used,
                    "error": r.error,
                }
                for r in result.node_results
            ],
        }, indent=2))
        # partial workflows exit 0 — they completed, just not cleanly.
        return 0 if result.status in ("success", "partial") else 1

    print(f"\n=== Workflow {result.workflow!r} → {result.status} ===")
    print(f"  run_id: {result.run_id}")
    print(f"  duration: {result.duration_ms}ms")
    if result.error:
        print(f"  error: {result.error}")

    for node_result in result.node_results:
        attempts_note = (
            f" [attempts={node_result.attempts}]" if node_result.attempts > 1 else ""
        )
        print(f"\n  --- node[{node_result.node_id}] → {node_result.status} "
              f"({node_result.duration_ms}ms){attempts_note} ---")
        if node_result.status == "skipped":
            if node_result.error:
                print(f"  reason: {node_result.error}")
            continue
        if node_result.tool_calls:
            tools = ", ".join(tc["name"] for tc in node_result.tool_calls)
            print(f"  tools used: {tools}")
        if node_result.tokens_used:
            t = node_result.tokens_used
            print(f"  tokens: in={t.get('input', 0)} out={t.get('output', 0)} "
                  f"cost=${t.get('cost', 0):.4f}")
        if node_result.final_text:
            print(f"\n{node_result.final_text}")
        if node_result.error:
            print(f"  error: {node_result.error}")

    return 0 if result.status in ("success", "partial") else 1


def cmd_workflow_history(args) -> int:
    """`agentwire workflow history` — list past runs."""
    workflow = getattr(args, "workflow", None)
    limit = getattr(args, "limit", 20) or 20
    runs = storage.list_runs(RUNS_DIR, workflow=workflow, limit=limit)

    if getattr(args, "json", False):
        print(json.dumps(runs, indent=2))
        return 0

    if not runs:
        if workflow:
            print(f"No runs found for workflow {workflow!r}.")
        else:
            print("No runs found.")
            print(f"  (searched {RUNS_DIR})")
        return 0

    # Fixed-width columns for quick scanning. run_id is long; truncate gracefully.
    print(f"  {'run_id':<52}  {'workflow':<18}  {'runner':<9}  {'status':<8}  {'duration':>8}  started")
    print(f"  {'-' * 52}  {'-' * 18}  {'-' * 9}  {'-' * 8}  {'-' * 8}  {'-' * 19}")
    for run in runs:
        rid = run.get("run_id", "")
        if len(rid) > 52:
            rid = rid[:49] + "..."
        name = run.get("workflow", "")
        if len(name) > 18:
            name = name[:15] + "..."
        rnr = (run.get("runner") or "?")[:9]
        status = run.get("status", "")
        duration = _fmt_duration(run.get("duration_ms"))
        started = _fmt_started_at(run.get("started_at"))
        print(f"  {rid:<52}  {name:<18}  {rnr:<9}  {status:<8}  {duration:>8}  {started}")

    return 0


def cmd_workflow_show(args) -> int:
    """`agentwire workflow show <run-id>` — inspect a past run."""
    run_id = args.run_id
    node_filter = getattr(args, "node", None)
    want_events = getattr(args, "events", False)
    want_json = getattr(args, "json", False)

    meta = storage.load_run(RUNS_DIR, run_id)
    if meta is None:
        print(f"Error: run {run_id!r} not found.", file=sys.stderr)
        print(f"  (searched {RUNS_DIR / run_id})", file=sys.stderr)
        return 1

    # --node <id> or --events ⇒ dump event JSONL
    if node_filter or want_events:
        events = storage.load_events(RUNS_DIR, run_id, node_id=node_filter)
        if want_json:
            print(json.dumps(
                [{"node": nid, "event": evt} for nid, evt in events],
                indent=2,
            ))
            return 0
        # Raw JSONL — one line per event. Prefix with [node-id] when showing all.
        for nid, evt in events:
            line = json.dumps(evt)
            if node_filter:
                print(line)
            else:
                print(f"[{nid}] {line}")
        return 0

    if want_json:
        print(json.dumps(meta, indent=2))
        return 0

    print(f"Workflow: {meta.get('workflow', '?')}")
    print(f"Run ID:   {meta.get('run_id', run_id)}")
    print(f"Status:   {meta.get('status', '?')}")
    print(f"Runner:   {meta.get('runner') or '?'}")
    print(f"Started:  {_fmt_started_at(meta.get('started_at'))}")
    print(f"Duration: {_fmt_duration(meta.get('duration_ms'))}")
    if meta.get("error"):
        print(f"Error:    {meta['error']}")
    inputs = meta.get("inputs") or {}
    if inputs:
        print(f"Inputs:   {json.dumps(inputs)}")

    nodes = meta.get("nodes") or []
    if nodes:
        print("\nNodes:")
        for n in nodes:
            nid = n.get("id", "?")
            rnr = (n.get("runner") or "?")
            status = n.get("status", "?")
            dur = _fmt_duration(n.get("duration_ms"))
            attempts = n.get("attempts", 1)
            attempts_note = f" attempts={attempts}" if attempts > 1 else ""
            tokens = n.get("tokens") or {}
            token_note = ""
            if tokens:
                token_note = (
                    f", in={tokens.get('input', 0)} out={tokens.get('output', 0)} "
                    f"cost=${tokens.get('cost', 0):.4f}"
                )
            print(f"  {nid:<20} ({rnr:<9}) → {status:<8} ({dur}{attempts_note}{token_note})")
            if n.get("error"):
                print(f"    reason: {n['error']}")

        total_in = sum((n.get("tokens") or {}).get("input", 0) for n in nodes)
        total_out = sum((n.get("tokens") or {}).get("output", 0) for n in nodes)
        total_cost = sum((n.get("tokens") or {}).get("cost", 0.0) for n in nodes)
        if total_in or total_out or total_cost:
            print(f"\nTotals: in={total_in} out={total_out} cost=${total_cost:.4f}")

    return 0
