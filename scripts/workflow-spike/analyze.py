"""Workflow spike analysis (#1333): watch the idle gate, then report what was observed.

watch  -- poll agentwire.background_tasks.evaluate() against the probe session
          while the workflow runs, appending one JSON line per sample.
report -- read the hook log, the session transcript, the gate samples and the
          worktree snapshots and print what fired and whether the gate held.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

POLL_SECONDS = 3
QUIET_SECONDS = 20
MAX_SECONDS = 900


def spike_dir(project: Path) -> Path:
    return project / ".wf-spike"


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def conversation_id(events: list[dict]) -> str:
    return next((e["session_id"] for e in events if e.get("session_id")), "")


def session_alive(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0


def watch(session: str, project: Path) -> int:
    from agentwire import background_tasks

    out = spike_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    events_file = out / "events.jsonl"
    gate_file = out / "gate.jsonl"
    started = time.time()
    last_count, last_change = 0, time.time()
    with gate_file.open("a") as sink:
        while time.time() - started < MAX_SECONDS and session_alive(session):
            events = read_jsonl(events_file)
            if len(events) != last_count:
                last_count, last_change = len(events), time.time()
            pending = background_tasks.evaluate(
                session, 0, conversation_id(events), str(project))
            sink.write(json.dumps({
                "ts": time.time(), "count": pending.count, "source": pending.source,
                "uncertain": pending.uncertain, "notes": pending.notes,
                "transcript_ids": pending.transcript_ids,
                "pane": (background_tasks._capture(session, 0) or "")[-1500:],
            }) + "\n")
            sink.flush()
            finished = any(e.get("hook_event_name") == "SubagentStop" for e in events)
            if finished and time.time() - last_change > QUIET_SECONDS:
                break
            time.sleep(POLL_SECONDS)
    print(f"gate samples written to {gate_file}")
    return 0


def find_transcript(project: Path, session_id: str) -> "Path | None":
    mangled = re.sub(r"[/.]", "-", str(project.resolve()))
    base = Path.home() / ".claude" / "projects" / mangled
    if session_id and (base / f"{session_id}.jsonl").exists():
        return base / f"{session_id}.jsonl"
    found = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def keys_with(obj, needle: str, prefix: str = "") -> list[str]:
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if needle in k.lower():
                hits.append(path)
            hits += keys_with(v, needle, path)
    elif isinstance(obj, list):
        for v in obj:
            hits += keys_with(v, needle, prefix)
    return hits


def events_section(events: list[dict]) -> list[str]:
    lines = ["== hook events =="]
    by_event = defaultdict(list)
    for e in events:
        by_event[e.get("hook_event_name", "?")].append(e)
    if not events:
        return lines + ["NO EVENTS LOGGED: hooks did not fire at all"]
    for name, rows in sorted(by_event.items()):
        with_agent = [r for r in rows if r.get("agent_id")]
        types = Counter(r.get("agent_type") for r in with_agent)
        lines.append(f"{name}: {len(rows)} total, {len(with_agent)} carry agent_id, "
                     f"agent_types={dict(types)}")
    ids = sorted({r["agent_id"] for r in events if r.get("agent_id")})
    lines.append(f"distinct agent_ids ({len(ids)}): {ids[:6]}")
    lines.append("payload keys per event: " + "; ".join(
        f"{n}={sorted(set().union(*[set(r) for r in rows]))}" for n, rows in sorted(by_event.items())))
    wf_keys = sorted({k for e in events for k in keys_with(e, "workflow")})
    wf_tools = [e for e in events if e.get("tool_name") == "Workflow"]
    lines.append(f"workflow-named payload keys: {wf_keys or 'none'}")
    lines.append(f"Workflow tool calls seen by hooks: {len(wf_tools)}")
    subagent_cwds = sorted({e.get("cwd") for e in events if e.get("agent_id") and e.get("cwd")})
    lines.append(f"cwd of agent-carrying events (isolation shows as a different path): {subagent_cwds}")
    return lines


def transcript_section(transcript: "Path | None") -> list[str]:
    from agentwire import background_tasks

    lines = ["== transcript =="]
    if transcript is None:
        return lines + ["no transcript found"]
    lines.append(f"path: {transcript}")
    launch_shapes, notifications = [], []
    for row in read_jsonl(transcript):
        result = row.get("toolUseResult")
        if isinstance(result, dict) and ({"backgroundTaskId", "taskId"} & set(result)
                                         or "workflow" in json.dumps(result).lower()[:4000]):
            launch_shapes.append(sorted(result))
        content = row.get("content")
        if row.get("type") == "queue-operation" and isinstance(content, str) \
                and "<task-notification>" in content:
            notifications.append(re.sub(r"\s+", " ", content)[:200])
    lines.append(f"toolUseResult shapes that look like a background start: {launch_shapes or 'none'}")
    lines.append(f"task-notification rows ({len(notifications)}):")
    lines += [f"  {n}" for n in notifications[:8]]
    lines.append(f"transcript pending at end of run (should be []): "
                 f"{background_tasks.pending_from_transcript(transcript)}")
    return lines


def gate_section(events: list[dict], samples: list[dict]) -> list[str]:
    lines = ["== idle gate =="]
    if not samples:
        return lines + ["no gate samples: run the watch step"]
    agent_ts = [e["_ts"] for e in events if e.get("agent_id") and "_ts" in e]
    if not agent_ts:
        return lines + ["no agent-carrying events, cannot bound the active span"]
    lo, hi = min(agent_ts), max(agent_ts)
    active = [s for s in samples if lo <= s["ts"] <= hi]
    lines.append(f"active span {hi - lo:.0f}s, {len(active)} samples inside it")
    lines.append(f"sources seen: {dict(Counter(s['source'] for s in active))}")
    lines.append(f"max pending count: {max((s['count'] or 0) for s in active) if active else 'n/a'}")
    blind = [s for s in active if not s["uncertain"] and not s["count"]]
    uncertain = [s for s in active if s["uncertain"]]
    lines.append(f"samples where gate said PROCEED (count 0/None, certain): {len(blind)}")
    lines.append(f"samples where gate said HOLD via uncertainty: {len(uncertain)}")
    if blind:
        lines.append("VERDICT: GATE CANNOT SEE A RUNNING WORKFLOW (a reap here would kill live work)")
        lines.append("pane at first blind sample:\n" + blind[0]["pane"])
    elif active:
        lines.append("VERDICT: gate held for the whole active span")
    return lines


def worktree_section(project: Path, events: list[dict]) -> list[str]:
    out = spike_dir(project)
    lines = ["== isolation worktrees =="]
    for name in ("before", "after"):
        path = out / f"worktrees.{name}"
        lines.append(f"git worktree list {name}:")
        lines += [f"  {line}" for line in (path.read_text().splitlines() if path.exists() else ["(missing)"])]
    removed = [e for e in events if e.get("hook_event_name") == "WorktreeRemove"]
    lines.append(f"WorktreeRemove events: {len(removed)}")
    return lines


def report(project: Path) -> int:
    out = spike_dir(project)
    events = read_jsonl(out / "events.jsonl")
    samples = read_jsonl(out / "gate.jsonl")
    sections = [
        events_section(events),
        transcript_section(find_transcript(project, conversation_id(events))),
        gate_section(events, samples),
        worktree_section(project, events),
    ]
    print("\n\n".join("\n".join(s) for s in sections))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch")
    w.add_argument("session")
    w.add_argument("--dir", required=True, type=Path)
    r = sub.add_parser("report")
    r.add_argument("--dir", required=True, type=Path)
    args = parser.parse_args(argv)
    return watch(args.session, args.dir) if args.cmd == "watch" else report(args.dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
