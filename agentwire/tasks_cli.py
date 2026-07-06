"""CLI for the protected task-execution file — `agentwire tasks review|promote` (#720).

`.agentwire.tasks.yml` is protected control-plane (see
`agentwire.safety._core.PROTECTED_CONTROL_PLANE_PATHS`) — the policed agent
cannot write it directly via Edit/Write/Bash. The authoring flow is
propose-and-promote, mirroring the worktree -> PR -> review -> merge model
because task definitions ARE executable code:

1. An agent drafts to the UNPROTECTED staging file
   `.agentwire.tasks.proposed.yml` with its normal file tools.
2. A human runs `agentwire tasks review` to see exactly what the draft would
   execute (a diff plus every shell-bearing field, surfaced explicitly).
3. The human runs `agentwire tasks promote` to copy the vetted draft into the
   live `.agentwire.tasks.yml`. agentwire itself (host-trusted) does the write;
   the agent never does.

Both commands are HOST-ONLY by design:

- They are deliberately NOT exposed as MCP tools. The `mcp-tool-damage-control`
  hook only gates the specific outbound-comms matchers (email_send/quo_send) —
  every other `mcp__agentwire__*` tool is open by default, so an MCP tool that
  shelled out to `tasks promote` would hand the agent an instant, unguarded
  bypass of the whole scheme. CLI-only keeps this on the human's own terminal.
- `agentwire tasks promote` is additionally hard-blocked as a Bash-tool pattern
  (`rules/agentwire.yaml`) so an agent can't just run the CLI itself from its
  Bash tool either. That block is defense-in-depth on top of the file
  protection above, not a replacement for it (kill-switch-off / an escape
  hatch could still open it — the deeper "police-at-execution" fix is a
  tracked follow-up, not implemented here).
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path
from typing import Optional

import yaml

from .core import _get_session_project_path, _output_json, _output_result
from .project_config import ensure_gitignored
from .tasks import PROPOSED_TASKS_FILENAME, TASKS_FILENAME, parse_task_config, validate_task


def _resolve_project_path(session: Optional[str]) -> Path:
    if session:
        resolved = _get_session_project_path(session)
        return resolved if resolved else Path.cwd()
    return Path.cwd()


def _load_yaml(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Parse a YAML file. Returns (data, error) — exactly one is None."""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        return None, f"Invalid YAML in {path}: {e}"
    if not isinstance(data, dict):
        return None, f"{path} must contain a mapping at the top level"
    return data, None


def _validate_draft(data: dict) -> list[str]:
    """Parse+validate every task in a proposed tasks-file dict. Returns issues."""
    issues: list[str] = []
    default_shell = data.get("shell")
    tasks = data.get("tasks", {}) or {}
    if not isinstance(tasks, dict):
        return ["'tasks' must be a mapping of task name -> config"]
    for name, cfg in tasks.items():
        if not isinstance(cfg, dict):
            issues.append(f"Task '{name}': config must be a mapping")
            continue
        try:
            task = parse_task_config(name, cfg, default_shell=default_shell)
            issues.extend(f"{name}: {i}" for i in validate_task(task))
        except Exception as e:  # noqa: BLE001 — surfaced to the reviewer, not raised
            issues.append(f"{name}: {e}")
    return issues


def _shell_bearing_fields(tasks: dict) -> list[str]:
    """Flatten every shell-executed string across all tasks — the review's purpose."""
    lines: list[str] = []
    for name, cfg in (tasks or {}).items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("shell"):
            lines.append(f"  {name}.shell: {cfg['shell']}")
        pre = cfg.get("pre", {})
        if isinstance(pre, dict):
            for var, pre_cfg in pre.items():
                cmd = pre_cfg.get("cmd") if isinstance(pre_cfg, dict) else pre_cfg
                if cmd:
                    lines.append(f"  {name}.pre.{var}: {cmd}")
                if isinstance(pre_cfg, dict) and pre_cfg.get("validate"):
                    lines.append(f"  {name}.pre.{var}.validate: {pre_cfg['validate']}")
        post = cfg.get("post", [])
        post_list = post if isinstance(post, list) else [post]
        for i, cmd in enumerate(post_list):
            if cmd:
                lines.append(f"  {name}.post[{i}]: {cmd}")
        if cfg.get("on_task_end"):
            preview = str(cfg["on_task_end"]).strip().splitlines()[0][:80]
            lines.append(f"  {name}.on_task_end (agent prompt): {preview}...")
        if cfg.get("unattended_allow"):
            lines.append(f"  {name}.unattended_allow: {cfg['unattended_allow']}")
    return lines


def cmd_tasks_review(args) -> int:
    """CLI command: agentwire tasks review [session]"""
    json_mode = getattr(args, "json", False)
    project_path = _resolve_project_path(getattr(args, "session", None))
    proposed_path = project_path / PROPOSED_TASKS_FILENAME
    active_path = project_path / TASKS_FILENAME

    if not proposed_path.exists():
        return _output_result(False, json_mode, f"No staged draft at {proposed_path}")

    data, err = _load_yaml(proposed_path)
    if err:
        return _output_result(False, json_mode, err)

    issues = _validate_draft(data)
    shell_lines = _shell_bearing_fields(data.get("tasks", {}))

    active_text = active_path.read_text() if active_path.exists() else ""
    proposed_text = proposed_path.read_text()
    diff = "".join(difflib.unified_diff(
        active_text.splitlines(keepends=True),
        proposed_text.splitlines(keepends=True),
        fromfile=str(active_path) if active_path.exists() else "(no live file yet)",
        tofile=str(proposed_path),
    ))

    if json_mode:
        _output_json({
            "success": not issues,
            "project": str(project_path),
            "diff": diff,
            "shell_commands": shell_lines,
            "validation_issues": issues,
        })
        return 1 if issues else 0

    print(f"Reviewing {proposed_path}\n")
    print(diff if diff else "(no textual diff against the live file)")
    print()
    if shell_lines:
        print("Shell commands / prompts this draft would run once promoted:")
        for line in shell_lines:
            print(line)
    else:
        print("No shell commands found in this draft.")

    if issues:
        print(f"\nValidation issues ({len(issues)}):")
        for issue in issues:
            print(f"  - {issue}")
        return 1

    print("\nNo validation issues. Run `agentwire tasks promote` to make this the live task config.")
    return 0


def cmd_tasks_promote(args) -> int:
    """CLI command: agentwire tasks promote [session] [--yes]"""
    json_mode = getattr(args, "json", False)
    assume_yes = getattr(args, "yes", False)
    project_path = _resolve_project_path(getattr(args, "session", None))
    proposed_path = project_path / PROPOSED_TASKS_FILENAME
    active_path = project_path / TASKS_FILENAME

    if not proposed_path.exists():
        return _output_result(False, json_mode, f"No staged draft at {proposed_path}")

    data, err = _load_yaml(proposed_path)
    if err:
        return _output_result(False, json_mode, err)

    issues = _validate_draft(data)
    if issues:
        return _output_result(
            False, json_mode,
            "Draft has validation issues — fix and re-review before promoting",
            issues=issues,
        )

    if not assume_yes:
        if json_mode or not sys.stdin.isatty():
            return _output_result(
                False, json_mode,
                "Refusing to promote without --yes (no interactive confirmation available)",
            )
        shell_lines = _shell_bearing_fields(data.get("tasks", {}))
        if shell_lines:
            print("This draft would run:")
            for line in shell_lines:
                print(line)
        print(f"\nPromote {proposed_path} -> {active_path}?")
        reply = input("Type 'yes' to confirm: ").strip().lower()
        if reply != "yes":
            return _output_result(False, json_mode, "Promotion cancelled")

    active_path.write_text(proposed_path.read_text())
    ensure_gitignored(project_path, TASKS_FILENAME, ".agentwire.tasks*.yml")
    proposed_path.unlink()

    return _output_result(
        True, json_mode, f"Promoted {TASKS_FILENAME}", project=str(project_path),
    )


def register_tasks_parser(subparsers) -> None:
    """Register the `tasks` command group (propose-and-promote for task-exec config)."""
    tasks_parser = subparsers.add_parser(
        "tasks",
        help="Review and promote the protected .agentwire.tasks.yml (host-only)",
        description=(
            "Propose-and-promote workflow for the protected .agentwire.tasks.yml "
            "(#720): an agent drafts to .agentwire.tasks.proposed.yml, a human "
            "reviews and promotes it here."
        ),
    )
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command")

    review = tasks_subparsers.add_parser(
        "review", help="Show the diff + every shell command in the staged draft"
    )
    review.add_argument("session", nargs="?", help="Session name (default: current directory)")
    review.add_argument("--json", action="store_true", help="Output JSON")
    review.set_defaults(func=cmd_tasks_review)

    promote = tasks_subparsers.add_parser(
        "promote", help="Copy the vetted draft into the live .agentwire.tasks.yml"
    )
    promote.add_argument("session", nargs="?", help="Session name (default: current directory)")
    promote.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    promote.add_argument("--json", action="store_true", help="Output JSON")
    promote.set_defaults(func=cmd_tasks_promote)
