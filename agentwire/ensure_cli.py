"""CLI for scheduled-workload execution — ``agentwire ensure`` plus the
``task`` and ``lock`` command groups.

``ensure`` runs a named task from ``.agentwire.yml`` with locking, retries,
and hook-based completion detection — the primitive the scheduler dispatches.
``task`` lists/shows/validates task definitions; ``lock`` manages the session
locks ``ensure`` acquires. Per the CLAUDE.md SSOT rule the logic lives here;
the portal and MCP layers are thin wrappers over these commands.
"""

from __future__ import annotations

import datetime
import subprocess
import time
from pathlib import Path

from . import pane_manager
from .core import (
    _get_machine_config,
    _get_session_project_path,
    _output_json,
    _output_result,
    _parse_session_target,
    load_config,
    tmux_session_exists,
)

# =============================================================================
# Task Commands (Scheduled Workloads)
# =============================================================================

# Exit codes for ensure command (documented in CLAUDE.md)
ENSURE_EXIT_COMPLETE = 0
ENSURE_EXIT_FAILED = 1
ENSURE_EXIT_INCOMPLETE = 2
ENSURE_EXIT_LOCK_CONFLICT = 3
ENSURE_EXIT_PRE_FAILURE = 4
ENSURE_EXIT_TIMEOUT = 5
ENSURE_EXIT_SESSION_ERROR = 6
ENSURE_EXIT_USAGE_LIMIT = 7


def _ensure_remote(args, session: str, machine_id: str, json_mode: bool) -> int:
    """Delegate `ensure` to the remote machine via SSH.

    When the session target is `name@machine`, we reconstruct the full
    `agentwire ensure` command and run it on the remote machine natively.
    All local concerns (locking, idle detection, pre/post commands, summary
    files) happen on the remote machine where the session actually lives.
    """
    import shlex

    machine = _get_machine_config(machine_id)
    if machine is None:
        return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json", exit_code=ENSURE_EXIT_SESSION_ERROR)

    host = machine.get("host", machine_id)
    user = machine.get("user")
    port = machine.get("port")
    ssh_target = f"{user}@{host}" if user else host

    # Translate local project path to remote equivalent
    remote_project = None
    if hasattr(args, 'project') and args.project:
        local_path = Path(args.project).expanduser().resolve()
        # Get local projects dir from config
        config = load_config()
        local_projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser().resolve()
        # Get remote projects dir from machine config (or default)
        remote_projects_dir = machine.get("projects_dir", "~/projects")
        try:
            relative = local_path.relative_to(local_projects_dir)
            remote_project = f"{remote_projects_dir}/{relative}"
        except ValueError:
            # Path not under local projects dir — use basename only
            remote_project = f"{remote_projects_dir}/{local_path.name}"

    # Reconstruct ensure command for the remote (session without @machine)
    cmd_parts = ["agentwire", "ensure", "-s", session, "--task", args.task, "--json"]
    if remote_project:
        cmd_parts.extend(["--project", remote_project])
    if getattr(args, 'wait_lock', False):
        cmd_parts.append("--wait-lock")
    if getattr(args, 'lock_timeout', 60) != 60:
        cmd_parts.extend(["--lock-timeout", str(args.lock_timeout)])
    if getattr(args, 'skip_if_locked', False):
        cmd_parts.append("--skip-if-locked")

    remote_cmd = f"bash -l -c {shlex.quote(' '.join(shlex.quote(p) for p in cmd_parts))}"

    ssh_cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([ssh_target, remote_cmd])

    # Stream output in real-time — ensure can run for tens of minutes
    try:
        proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            print(line, end="", flush=True)
        proc.wait()
        return proc.returncode
    except Exception as e:
        return _output_result(False, json_mode, f"SSH to {machine_id} failed: {e}", exit_code=ENSURE_EXIT_SESSION_ERROR)


def cmd_ensure(args) -> int:
    """Run a named task with reliable session management.

    Full lifecycle:
    1. Acquire lock (fail if locked, or wait with --wait-lock)
    2. Ensure session exists and is healthy
    3. Wait for session to be idle
    4. Run pre-commands, validate outputs
    5. Send templated prompt
    6. Wait for idle, send system summary prompt
    7. Parse summary file for status
    8. Send on_task_end prompt if defined
    9. Run post-commands
    10. Handle retries on failure
    """
    from .completion import (
        get_summary_prompt,
    )
    from .locking import LockConflict, LockTimeout, session_lock
    from .tasks import (
        TaskNotFound,
        TaskValidationError,
        load_task,
        validate_task,
    )
    from .templating import TemplateContext, preview_template

    session_name = args.session
    task_name = args.task
    dry_run = getattr(args, 'dry_run', False)
    wait_lock = getattr(args, 'wait_lock', False)
    lock_timeout = getattr(args, 'lock_timeout', 60)
    skip_if_locked = getattr(args, 'skip_if_locked', False)
    json_mode = getattr(args, 'json', False)

    # Parse session target
    session, machine_id = _parse_session_target(session_name)

    if machine_id:
        return _ensure_remote(args, session, machine_id, json_mode)

    # A session parked on a usage limit is waiting out the reset — never
    # prompt or re-dispatch into it. The watchdog resumes it.
    from .usage_limit import is_parked
    if is_parked(session):
        return _output_result(
            False, json_mode,
            f"Session '{session}' is parked on a usage limit (auto-resumes after reset)",
            exit_code=ENSURE_EXIT_USAGE_LIMIT,
        )

    # Find project path from --project flag, or session's working directory
    if hasattr(args, 'project') and args.project:
        project_path = Path(args.project).expanduser().resolve()
    else:
        project_path = _get_session_project_path(session)

    if not project_path.exists():
        return _output_result(False, json_mode, f"Project path not found: {project_path}", exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Load task configuration
    try:
        task = load_task(project_path, task_name)
    except TaskNotFound as e:
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_SESSION_ERROR)
    except TaskValidationError as e:
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Validate task
    issues = validate_task(task)
    if issues:
        return _output_result(False, json_mode, f"Task validation failed: {', '.join(issues)}", exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Determine shell
    shell = task.shell or "/bin/sh"

    # Initialize template context
    ctx = TemplateContext(
        session=session,
        task=task_name,
        project_root=str(project_path),
    )

    # Dry run mode
    if dry_run:
        print("=== DRY RUN ===\n")
        print(f"Session: {session}")
        print(f"Task: {task_name}")
        print(f"Shell: {shell}")
        print(f"Idle timeout: {task.idle_timeout}s")
        print(f"Retries: {task.retries}")
        print()

        if task.pre:
            print("Pre-commands (would execute):")
            for pre in task.pre:
                req = " (required)" if pre.required else ""
                val = f" validate: {pre.validate}" if pre.validate else ""
                print(f"  {pre.name}: {pre.cmd}{req}{val}")
            print()

        print("Prompt (with placeholders for pre-outputs):")
        print(preview_template(task.prompt, ctx))
        print()

        print("System summary prompt:")
        print(get_summary_prompt("<generated-filename>"))
        print()

        if task.on_task_end:
            print("On task end prompt:")
            print(preview_template(task.on_task_end, ctx))
            print()

        if task.post:
            print("Post-commands (would execute):")
            for cmd in task.post:
                print(f"  {preview_template(cmd, ctx)}")
            print()

        if task.output.save:
            print(f"Save output to: {preview_template(task.output.save, ctx)}")

        return 0

    # Acquire lock
    try:
        with session_lock(session, wait=wait_lock, timeout=lock_timeout):
            return _run_ensure_task(
                args, session, task, ctx, shell, project_path, json_mode
            )
    except LockConflict as e:
        if skip_if_locked:
            return 0
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_LOCK_CONFLICT)
    except LockTimeout as e:
        if skip_if_locked:
            return 0
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_LOCK_CONFLICT)


def _setup_task_branch(project_path, task, json_mode) -> tuple[str, str | None]:
    """Set up git branch for a task with starting_ref.

    Checks out starting_ref, pulls latest, creates the work branch.

    Returns:
        (work_branch_name, error_message) — error_message is None on success.
    """
    from .tasks import PreCommandError  # noqa: F401 (used for caller)

    starting_ref = task.starting_ref

    # Verify the ref exists
    result = subprocess.run(
        ["git", "rev-parse", "--verify", starting_ref],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "", f"starting_ref '{starting_ref}' not found in {project_path}"

    # Checkout the starting ref
    checkout = subprocess.run(
        ["git", "checkout", starting_ref],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        return "", f"Failed to checkout '{starting_ref}': {checkout.stderr.strip()}"

    # Pull if it's a branch (not detached HEAD)
    head_check = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        cwd=project_path,
        capture_output=True,
    )
    if head_check.returncode == 0:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=project_path,
            capture_output=True,
        )

    # Determine work branch name
    work_branch = task.work_branch
    if not work_branch:
        today = datetime.date.today().isoformat()
        work_branch = f"agent/{task.name}-{today}"

    # Handle collision: append -2, -3, ... until name is free
    check = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{work_branch}"],
        cwd=project_path,
        capture_output=True,
    )
    if check.returncode == 0:
        n = 2
        base = work_branch
        while True:
            candidate = f"{base}-{n}"
            check = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{candidate}"],
                cwd=project_path,
                capture_output=True,
            )
            if check.returncode != 0:
                work_branch = candidate
                break
            n += 1

    # Create and checkout work branch
    create = subprocess.run(
        ["git", "checkout", "-b", work_branch],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        return "", f"Failed to create work branch '{work_branch}': {create.stderr.strip()}"

    if not json_mode:
        print(f"Branch: {work_branch} (from {starting_ref})")

    return work_branch, None


def _create_task_pr(project_path, task, work_branch, last_summary, json_mode) -> str | None:
    """Commit, push, and open a PR for completed task work.

    Returns:
        PR URL if created, None if skipped or failed.
    """
    pr_target = task.pr_target or task.starting_ref

    # Check for uncommitted changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    has_changes = bool(status.stdout.strip())

    if not has_changes:
        if not json_mode:
            print("No changes to commit — skipping PR creation")
        # Still reset to starting_ref
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Commit all changes
    today = datetime.date.today().isoformat()
    commit_msg = f"chore: agent task {task.name} ({today})"
    subprocess.run(["git", "add", "-A"], cwd=project_path, capture_output=True)
    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        if not json_mode:
            print(f"Warning: commit failed: {commit_result.stderr.strip()}")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Push branch
    push = subprocess.run(
        ["git", "push", "-u", "origin", work_branch],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        if not json_mode:
            print(f"Warning: push failed: {push.stderr.strip()}")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Check gh is available
    gh_check = subprocess.run(["which", "gh"], capture_output=True)
    if gh_check.returncode != 0:
        if not json_mode:
            print("Warning: 'gh' not found — skipping PR creation (branch pushed)")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Create PR
    pr_title = f"agent: {task.name} ({today})"
    pr_body = last_summary if last_summary else f"Automated changes from agent task `{task.name}`."
    pr_cmd = [
        "gh", "pr", "create",
        "--base", pr_target,
        "--head", work_branch,
        "--title", pr_title,
        "--body", pr_body,
    ]
    if task.pr_draft:
        pr_cmd.append("--draft")

    pr_result = subprocess.run(
        pr_cmd,
        cwd=project_path,
        capture_output=True,
        text=True,
    )

    pr_url = None
    if pr_result.returncode == 0:
        pr_url = pr_result.stdout.strip()
        if not json_mode:
            print(f"PR created: {pr_url}")
    else:
        if not json_mode:
            print(f"Warning: PR creation failed: {pr_result.stderr.strip()}")

    # Reset to starting_ref
    subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)

    return pr_url


def _run_ensure_task(args, session, task, ctx, shell, project_path, json_mode) -> int:
    """Run the task (called within lock context).

    Uses hook-based completion detection:
    1. Write task context file (tells hook a scheduled task is running)
    2. Send task prompt
    3. Hook handles: first idle → send summary prompt
    4. Poll for summary file (agent writes it after receiving summary prompt)
    5. Parse summary and return result
    """
    from .completion import (
        CompletionTimeout,
        clear_task_context,
        generate_summary_filename,
        status_to_exit_code,
        wait_for_completion_signal,
        write_task_context,
    )
    from .tasks import PreCommandError, run_post_command, run_pre_command
    from .templating import TemplateError, expand_all

    max_attempts = task.retries + 1
    last_status = "incomplete"
    last_summary = ""

    for attempt in range(1, max_attempts + 1):
        ctx.attempt = attempt

        if not json_mode and max_attempts > 1:
            print(f"Attempt {attempt}/{max_attempts}")

        # Ensure session exists and has agent running.
        # The scheduler may have pre-created this session with --model and --type
        # overrides via _pre_create_session(). Don't kill it — just wait for agent.
        if not tmux_session_exists(session):
            if not json_mode:
                print(f"Creating session '{session}'...")

            # Fork starting_session if configured (carries over Claude conversation context)
            if task.starting_session and task.starting_session != session:
                if tmux_session_exists(task.starting_session):
                    if not json_mode:
                        print(f"Forking context from session '{task.starting_session}'...")
                    fork_result = subprocess.run(
                        ["agentwire", "fork", "-s", task.starting_session, "-t", session, "--json"],
                        capture_output=True, text=True,
                    )
                    if fork_result.returncode != 0 and not json_mode:
                        print("Warning: context fork failed, starting fresh session")
                elif not json_mode:
                    print(f"Warning: starting_session '{task.starting_session}' not found, starting fresh")

            if not tmux_session_exists(session):
                class NewArgs:
                    def __init__(self, task_role):
                        self.session = session
                        self.path = str(project_path)
                        self.force = False
                        self.type = None
                        self.roles = task_role if task_role else None
                        self.model = None
                        self.json = json_mode
                        # Force the pre-#715 unconditional-inherit behavior:
                        # ensure is the scheduler's dispatch primitive — the
                        # scheduler daemon fans out across many projects from
                        # one fixed tmux session by design, so #715's
                        # same-project default (meant for an interactive
                        # session's own cross-project spawns) would wrongly
                        # drop the parent link for nearly every dispatched
                        # task. Always parent to whichever session is
                        # actually running this ensure call.
                        self.created_by = pane_manager.get_current_session()

                from . import session_cli
                result = session_cli.cmd_new(NewArgs(task.role))
                if result != 0:
                    return _output_result(False, json_mode, f"Failed to create session '{session}'", exit_code=ENSURE_EXIT_SESSION_ERROR)

        # Wait for agent to be ready to accept input.
        # Handles both freshly-created sessions (agent still loading) and
        # pre-created sessions from scheduler (agent may be mid-startup).
        if not json_mode:
            print("Waiting for agent to be ready...")
        from agentwire.session_ready import wait_for_session_ready
        if not wait_for_session_ready(session, timeout=30):
            # Agent never started — session is dead, bail out
            if not json_mode:
                print(f"Agent not ready in session '{session}' after 30s")
            return _output_result(False, json_mode, f"Agent not running in session '{session}'", exit_code=ENSURE_EXIT_SESSION_ERROR)

        # Set up work branch if starting_ref is configured
        work_branch = None
        if task.starting_ref:
            work_branch, branch_error = _setup_task_branch(project_path, task, json_mode)
            if branch_error:
                return _output_result(False, json_mode, branch_error, exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Run pre-commands
        if task.pre:
            if not json_mode:
                print("Running pre-commands...")

            for pre in task.pre:
                try:
                    output = run_pre_command(pre, shell, project_path)
                    ctx.set_pre_output(pre.name, output)
                    if not json_mode:
                        print(f"  {pre.name}: {len(output)} chars")
                except PreCommandError as e:
                    if work_branch and task.starting_ref:
                        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
                    return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Expand prompt
        try:
            prompt = expand_all(task.prompt, ctx)
        except TemplateError as e:
            return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Generate summary filename (scoped to session to avoid collisions)
        summary_filename = generate_summary_filename(session, task.name)
        summary_path = project_path / summary_filename
        ctx.summary_file = summary_filename

        # Ensure .agentwire directory exists
        (project_path / ".agentwire").mkdir(exist_ok=True)

        # Create iterations directory for loop tasks
        if task.mode == "loop":
            (project_path / ".agentwire" / "iterations").mkdir(exist_ok=True)

        # Clear any stale completion signal from a previous run
        # This prevents immediate return if a previous run's signal wasn't cleaned up
        clear_task_context(session)

        # Write task context for hook coordination
        # Hook will: first idle → send summary prompt (ensure polls for summary file directly)
        # Loop mode: hook iterates (review → re-prompt) until complete or max_iterations
        write_task_context(
            session=session,
            task_name=task.name,
            summary_file=summary_filename,
            attempt=attempt,
            exit_on_complete=task.exit_on_complete,
            mode=task.mode,
            max_iterations=task.max_iterations,
            iteration=1,
            loop_review=task.loop_review,
            loop_delay=task.loop_delay,
            original_prompt=prompt,
        )

        # Find previous summaries for this task to give the agent context
        summary_glob = f".agentwire/task-summary-{session}-{task.name}-*.md"
        prev_summaries = sorted(
            project_path.glob(summary_glob),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        if prev_summaries:
            prompt += "\n\nPrevious task summaries (consider them when generating your output):"
            for p in prev_summaries:
                prompt += f"\n- {p}"

        if not json_mode:
            print("Sending task prompt...")

        # Send task prompt using pane_manager for proper multi-line handling
        pane_manager.send_to_pane(session, 0, prompt, enter=True)

        # Wait for completion signal from hook
        if not json_mode:
            print("Waiting for task completion...")

        try:
            signal = wait_for_completion_signal(
                session, summary_path=summary_path
            )
            last_status = signal.get("status", "incomplete")
            last_summary = signal.get("summary", "")
            ctx.status = last_status
            ctx.summary = last_summary
        except CompletionTimeout:
            # Don't clear task context here — the hook may still need it.
            # Hook cleans up after itself (exit_on_complete kills session).
            # Task context files are cleared at the START of next run.
            last_status = "incomplete"
            last_summary = "Timeout waiting for task completion"
            if attempt < max_attempts:
                if not json_mode:
                    print(f"Timeout, retrying in {task.retry_delay}s...")
                time.sleep(task.retry_delay)
                continue
            break

        # Don't clear task context here — hook owns context file lifecycle.
        # ensure waits for hook to delete it (signals cleanup complete).

        if last_status == "usage_limit":
            # Session parked mid-task — skip on_task_end/post/PR; the watchdog
            # nudges it after reset and the idle hook finishes the task.
            if not json_mode:
                print("Usage limit hit — session parked, auto-resumes after reset")
            break

        if not json_mode:
            print(f"Task status: {last_status}")
            if last_summary:
                print(f"Summary: {last_summary}")

        # on_task_end: send additional prompt after summary is written
        # Note: we don't wait for this to complete - it's fire-and-forget
        if task.on_task_end:
            try:
                end_prompt = expand_all(task.on_task_end, ctx)
                pane_manager.send_to_pane(session, 0, end_prompt, enter=True)
                if not json_mode:
                    print("Sent on_task_end prompt (not waiting for completion)")
            except TemplateError as e:
                if not json_mode:
                    print(f"Warning: template error in on_task_end: {e}")

        # Capture output
        output_result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{task.output.capture}"],
            capture_output=True,
            text=True,
        )
        ctx.output = output_result.stdout if output_result.returncode == 0 else ""

        # Run post-commands
        if task.post:
            if not json_mode:
                print("Running post-commands...")

            for cmd in task.post:
                try:
                    expanded_cmd = expand_all(cmd, ctx)
                    rc, stdout, stderr = run_post_command(expanded_cmd, shell, project_path)
                    if rc != 0 and not json_mode:
                        print(f"  Warning: post-command failed: {stderr}")
                except TemplateError as e:
                    if not json_mode:
                        print(f"  Warning: template error in post-command: {e}")

        # Save output if configured
        if task.output.save:
            try:
                save_path = Path(expand_all(task.output.save, ctx)).expanduser()
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(ctx.output)
                if not json_mode:
                    print(f"Output saved to: {save_path}")
            except Exception as e:
                if not json_mode:
                    print(f"Warning: Failed to save output: {e}")

        # Create PR if branch management is configured
        if work_branch and task.starting_ref:
            pr_url = _create_task_pr(project_path, task, work_branch, last_summary, json_mode)
            ctx.work_branch = work_branch
            if pr_url:
                ctx.pr_url = pr_url

        # Check if we should retry
        if last_status == "failed" and attempt < max_attempts:
            if not json_mode:
                print(f"Task failed, retrying in {task.retry_delay}s...")
            time.sleep(task.retry_delay)
            continue

        # Done (success or no more retries)
        break

    # Final result
    exit_code = status_to_exit_code(last_status)

    if json_mode:
        result_data = {
            "success": last_status == "complete",
            "status": last_status,
            "summary": last_summary,
            "attempt": ctx.attempt,
            "summary_file": ctx.summary_file,
        }
        if ctx.work_branch:
            result_data["work_branch"] = ctx.work_branch
        if ctx.pr_url:
            result_data["pr_url"] = ctx.pr_url
        _output_json(result_data)
    else:
        print(f"\nTask {task.name}: {last_status}")

    return exit_code


def cmd_task_list(args) -> int:
    """List tasks for a session/project."""
    from .tasks import list_tasks

    session = getattr(args, 'session', None)
    json_mode = getattr(args, 'json', False)

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    if not project_path or not project_path.exists():
        return _output_result(False, json_mode, f"Project path not found: {project_path}")

    tasks = list_tasks(project_path)

    if json_mode:
        _output_json({"tasks": tasks, "project": str(project_path)})
        return 0

    if not tasks:
        print(f"No tasks defined in {project_path / '.agentwire.yml'}")
        return 0

    print(f"Tasks in {project_path.name}:\n")
    print(f"{'Name':<25} {'Mode':<10} {'Pre':<5} {'Post':<5} {'Retries':<8}")
    print("-" * 60)
    for t in tasks:
        pre = "Yes" if t["has_pre"] else "-"
        post = "Yes" if t["has_post"] else "-"
        mode = t.get("mode", "standard")
        print(f"{t['name']:<25} {mode:<10} {pre:<5} {post:<5} {t['retries']:<8}")

    return 0


def cmd_task_show(args) -> int:
    """Show task definition details."""
    from .tasks import TaskNotFound, TaskValidationError, load_task, validate_task

    task_arg = args.task  # format: session/task or just task
    json_mode = getattr(args, 'json', False)

    # Parse task argument
    if "/" in task_arg:
        session, task_name = task_arg.split("/", 1)
    else:
        session = None
        task_name = task_arg

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    try:
        task = load_task(project_path, task_name)
    except (TaskNotFound, TaskValidationError) as e:
        return _output_result(False, json_mode, str(e))

    issues = validate_task(task)

    if json_mode:
        _output_json({
            "name": task.name,
            "prompt": task.prompt,
            "shell": task.shell,
            "retries": task.retries,
            "retry_delay": task.retry_delay,
            "idle_timeout": task.idle_timeout,
            "mode": task.mode,
            "max_iterations": task.max_iterations,
            "loop_review": task.loop_review,
            "loop_delay": task.loop_delay,
            "pre": [{"name": p.name, "cmd": p.cmd, "required": p.required, "validate": p.validate, "timeout": p.timeout} for p in task.pre],
            "on_task_end": task.on_task_end,
            "post": task.post,
            "output": {"capture": task.output.capture, "save": task.output.save},
            "validation_issues": issues,
        })
        return 0

    print(f"Task: {task.name}\n")
    print(f"Shell: {task.shell or '/bin/sh'}")
    print(f"Mode: {task.mode}")
    if task.mode == "loop":
        print(f"Max iterations: {task.max_iterations}")
        print(f"Loop review: {task.loop_review}")
        if task.loop_delay > 0:
            print(f"Loop delay: {task.loop_delay}s")
    print(f"Retries: {task.retries} (delay: {task.retry_delay}s)")
    print(f"Idle timeout: {task.idle_timeout}s")
    print()

    if task.pre:
        print("Pre-commands:")
        for p in task.pre:
            req = " (required)" if p.required else ""
            print(f"  {p.name}: {p.cmd}{req}")
        print()

    print("Prompt:")
    print(task.prompt[:200] + "..." if len(task.prompt) > 200 else task.prompt)
    print()

    if task.on_task_end:
        print("On task end:")
        print(task.on_task_end[:100] + "..." if len(task.on_task_end) > 100 else task.on_task_end)
        print()

    if task.post:
        print("Post-commands:")
        for cmd in task.post:
            print(f"  {cmd}")
        print()

    if task.output.save:
        print("Output:")
        print(f"  Save to: {task.output.save}")

    if issues:
        print(f"\nValidation issues: {', '.join(issues)}")

    return 0


def cmd_task_validate(args) -> int:
    """Validate task configuration."""
    from .tasks import TaskNotFound, TaskValidationError, load_task, validate_task

    task_arg = args.task
    json_mode = getattr(args, 'json', False)

    # Parse task argument
    if "/" in task_arg:
        session, task_name = task_arg.split("/", 1)
    else:
        session = None
        task_name = task_arg

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    try:
        task = load_task(project_path, task_name)
    except (TaskNotFound, TaskValidationError) as e:
        return _output_result(False, json_mode, str(e))

    issues = validate_task(task)

    if json_mode:
        _output_json({
            "valid": len(issues) == 0,
            "issues": issues,
            "task": task_name,
        })
        return 0 if not issues else 1

    if issues:
        print(f"Task '{task_name}' has issues:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    else:
        print(f"Task '{task_name}' is valid.")
        return 0


# =============================================================================
# Lock Management Commands
# =============================================================================


def cmd_lock_list(args) -> int:
    """List all locks with metadata."""
    from .locking import list_locks

    json_mode = getattr(args, 'json', False)
    locks = list_locks()

    if json_mode:
        _output_json({"locks": locks})
        return 0

    if not locks:
        print("No locks found.")
        return 0

    # Format output
    print(f"{'SESSION':<25} {'PID':<10} {'AGE':<12} {'STATUS'}")
    print("-" * 60)

    for lock in locks:
        session = lock["session"][:24]
        pid = str(lock["pid"]) if lock["pid"] else "-"
        age_seconds = lock["age_seconds"]

        # Format age
        if age_seconds < 60:
            age = f"{age_seconds}s"
        elif age_seconds < 3600:
            age = f"{age_seconds // 60}m {age_seconds % 60}s"
        elif age_seconds < 86400:
            hours = age_seconds // 3600
            mins = (age_seconds % 3600) // 60
            age = f"{hours}h {mins}m"
        else:
            days = age_seconds // 86400
            hours = (age_seconds % 86400) // 3600
            age = f"{days}d {hours}h"

        status = lock["status"]
        print(f"{session:<25} {pid:<10} {age:<12} {status}")

    return 0


def cmd_lock_clean(args) -> int:
    """Remove all stale locks."""
    from .locking import clean_stale_locks

    json_mode = getattr(args, 'json', False)
    dry_run = getattr(args, 'dry_run', False)

    removed = clean_stale_locks(dry_run=dry_run)

    if json_mode:
        _output_json({
            "removed": removed,
            "count": len(removed),
            "dry_run": dry_run,
        })
        return 0

    if not removed:
        print("No stale locks found.")
    elif dry_run:
        print(f"Would remove {len(removed)} stale lock(s): {', '.join(removed)}")
    else:
        print(f"Removed {len(removed)} stale lock(s): {', '.join(removed)}")

    return 0


def cmd_lock_remove(args) -> int:
    """Force-remove a specific lock."""
    from .locking import remove_lock

    session = args.session
    json_mode = getattr(args, 'json', False)

    removed = remove_lock(session)

    if json_mode:
        _output_json({
            "session": session,
            "removed": removed,
        })
        return 0 if removed else 1

    if removed:
        print(f"Removed lock: {session}")
        return 0
    else:
        print(f"No lock found for: {session}")
        return 1


def cmd_lock(args) -> int:
    """Lock command dispatcher - shows help if no subcommand."""
    # This will be called if no subcommand is provided
    # The help is printed in main() based on lock_command being None
    return 0


def register_ensure_parser(subparsers) -> None:
    """Register the ensure, task, and lock top-level commands."""
    # === ensure command (scheduled workloads) ===
    ensure_parser = subparsers.add_parser(
        "ensure",
        help="Run named task with reliable session management",
        description="Execute a task from .agentwire.yml with locking, retries, and completion detection.",
    )
    ensure_parser.add_argument("-s", "--session", required=True, help="Target session name")
    ensure_parser.add_argument("-p", "--project", help="Project path containing .agentwire.yml (defaults to ~/projects/{session})")
    ensure_parser.add_argument("--task", required=True, help="Task name from .agentwire.yml")
    ensure_parser.add_argument("--dry-run", action="store_true", help="Show what would execute without running")
    ensure_parser.add_argument("--wait-lock", action="store_true", help="Wait for lock instead of failing if locked")
    ensure_parser.add_argument("--lock-timeout", type=int, default=60, help="Max time to wait for lock (default: 60s)")
    ensure_parser.add_argument("--skip-if-locked", action="store_true", help="Exit 0 silently if session is locked (for cron use cases)")
    ensure_parser.add_argument("--json", action="store_true", help="Output JSON")
    ensure_parser.set_defaults(func=cmd_ensure)

    # === task command group ===
    task_parser = subparsers.add_parser(
        "task",
        help="Manage scheduled tasks",
        description="List, show, and validate tasks defined in .agentwire.yml.",
    )
    task_subparsers = task_parser.add_subparsers(dest="task_command")

    # task list
    task_list = task_subparsers.add_parser("list", help="List tasks for session/project")
    task_list.add_argument("session", nargs="?", help="Session name (default: current directory)")
    task_list.add_argument("--json", action="store_true", help="Output JSON")
    task_list.set_defaults(func=cmd_task_list)

    # task show
    task_show = task_subparsers.add_parser("show", help="Show task definition details")
    task_show.add_argument("task", help="Task name (session/task or just task)")
    task_show.add_argument("--json", action="store_true", help="Output JSON")
    task_show.set_defaults(func=cmd_task_show)

    # task validate
    task_validate = task_subparsers.add_parser("validate", help="Validate task configuration")
    task_validate.add_argument("task", help="Task name (session/task or just task)")
    task_validate.add_argument("--json", action="store_true", help="Output JSON")
    task_validate.set_defaults(func=cmd_task_validate)

    # === lock command group ===
    lock_parser = subparsers.add_parser(
        "lock",
        help="Manage session locks",
        description="List, clean, and remove session locks.",
    )
    lock_subparsers = lock_parser.add_subparsers(dest="lock_command")
    lock_parser.set_defaults(func=cmd_lock)

    # lock list
    lock_list_parser = lock_subparsers.add_parser("list", help="List all locks")
    lock_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_list_parser.set_defaults(func=cmd_lock_list)

    # lock clean
    lock_clean_parser = lock_subparsers.add_parser("clean", help="Remove stale locks")
    lock_clean_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    lock_clean_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_clean_parser.set_defaults(func=cmd_lock_clean)

    # lock remove
    lock_remove_parser = lock_subparsers.add_parser("remove", help="Force-remove a lock")
    lock_remove_parser.add_argument("session", help="Session name")
    lock_remove_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_remove_parser.set_defaults(func=cmd_lock_remove)
