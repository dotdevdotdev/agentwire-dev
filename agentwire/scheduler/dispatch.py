"""Task dispatch — ensure subprocesses, session lifecycle, worktree+PR flow."""

import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .gates import _gated_tasks
from .models import (
    _EXIT_FAILED,
    _EXIT_LOCK_CONFLICT,
    _EXIT_TIMEOUT,
    _EXIT_TO_STATUS,
    Board,
    SchedulerTask,
    TaskState,
    _ts,
)


def _parse_ensure_summary(task: SchedulerTask, result, project: str | None = None,
                          session: str | None = None) -> tuple[str, list[str], list[str]]:
    """Try to extract summary info from ensure subprocess output.

    `project`/`session` override `task.project`/`task.session` for worktree
    dispatch, where the summary file lives in the worktree and is named after
    the worktree session.

    Returns:
        (summary_text, files_modified, blockers)
    """
    project = project or task.project
    session = session or task.session
    summary = ""
    files_modified: list[str] = []
    blockers: list[str] = []

    # Try parsing JSON stdout from ensure --json. Stdout may contain several
    # concatenated JSON objects when nested commands (e.g. session creation)
    # also print JSON — the last object is ensure's own result.
    data = None
    if hasattr(result, "stdout") and result.stdout:
        decoder = json.JSONDecoder()
        text = result.stdout
        idx = 0
        while idx < len(text):
            try:
                obj, idx = decoder.raw_decode(text, idx)
            except ValueError:
                idx += 1
                continue
            if isinstance(obj, dict):
                data = obj
    if data:
        try:
            summary = data.get("summary", "")
            summary_file = data.get("summary_file", "")
            if summary_file:
                from ..completion import parse_summary_file
                sp = Path(summary_file)
                if not sp.is_absolute():
                    sp = Path(project) / sp
                if sp.exists():
                    parsed = parse_summary_file(sp)
                    summary = summary or parsed.summary
                    files_modified = parsed.files_modified
                    blockers = parsed.blockers
        except Exception:
            pass
        # A failed dispatch has no summary file — record ensure's error so
        # the failure is self-describing in events/state instead of silent.
        if not summary and data.get("error"):
            summary = str(data["error"])

    # Fallback: glob for summary files if we didn't get one
    if not summary:
        try:
            agentwire_dir = Path(project) / ".agentwire"
            if agentwire_dir.exists():
                summaries = sorted(
                    agentwire_dir.glob(f"task-summary-{session}-{task.task}-*.md"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if summaries:
                    from ..completion import parse_summary_file
                    parsed = parse_summary_file(summaries[0])
                    summary = parsed.summary
                    files_modified = parsed.files_modified
                    blockers = parsed.blockers
        except Exception:
            pass

    return summary, files_modified, blockers


def _collect_descendants(pid: int, result: list[int]) -> None:
    """Recursively collect all descendant PIDs."""
    try:
        children = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        if children.returncode == 0:
            for line in children.stdout.strip().split('\n'):
                if line.strip():
                    child_pid = int(line.strip())
                    result.append(child_pid)
                    _collect_descendants(child_pid, result)
    except Exception:
        pass


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its descendants. No-op if already dead."""
    try:
        descendants: list[int] = []
        _collect_descendants(pid, descendants)

        # Kill children first (bottom-up), then parent
        for child_pid in reversed(descendants):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass

        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    except Exception:
        pass


def _sweep_orphaned_processes() -> None:
    """Kill orphaned agent processes not inside any active tmux session.

    Safety net for processes that survive _kill_session().
    """
    # Get all descendant PIDs of active tmux panes
    active_pids: set[int] = set()
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    try:
                        pid = int(line.strip())
                        active_pids.add(pid)
                        # Collect all descendants of active panes
                        descendants: list[int] = []
                        _collect_descendants(pid, descendants)
                        active_pids.update(descendants)
                    except ValueError:
                        pass
    except Exception:
        pass



def _kill_session(session: str) -> None:
    """Kill a tmux session and all processes inside it."""
    from agentwire import scheduler as _sched

    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"],
        capture_output=True,
    )
    if check.returncode != 0:
        return

    # Step 1: Get PIDs of processes running in all panes
    pids_result = subprocess.run(
        ["tmux", "list-panes", "-t", f"={session}", "-F", "#{pane_pid}"],
        capture_output=True, text=True, timeout=5,
    )
    pane_pids = []
    if pids_result.returncode == 0:
        for p in pids_result.stdout.strip().split('\n'):
            if p.strip():
                try:
                    pane_pids.append(int(p.strip()))
                except ValueError:
                    pass

    # Step 2: Send /exit to pane 0 for clean agent shutdown
    subprocess.run(
        ["tmux", "send-keys", "-t", f"={session}.0", "/exit", "Enter"],
        capture_output=True, timeout=5,
    )
    time.sleep(3)

    # Step 3: Kill tmux session
    subprocess.run(
        ["tmux", "kill-session", "-t", f"={session}"],
        capture_output=True, timeout=_sched._sched_config().git_op_timeout,
    )

    # Step 4: Kill any surviving process trees from the panes
    for pid in pane_pids:
        _kill_process_tree(pid)

    print(f"[{_ts()}] Killed session: {session}")
    time.sleep(1)


def _pre_create_session(task: SchedulerTask) -> None:
    """Pre-create session with scheduler posture/role overrides if needed.

    The scheduler may specify a different posture than the project's
    .agentwire.yml (e.g., auto for scheduled tasks). If overrides
    are set, we need to create the session before ensure runs, because
    ensure uses project defaults.

    If no overrides, this is a no-op — ensure --fresh handles everything.
    """
    from agentwire import scheduler as _sched

    if not task.posture and task.roles is None and not task.model:
        return  # No overrides, let ensure handle it

    # Only pre-create if session doesn't exist (ensure --fresh will have killed it)
    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={task.session}"],
        capture_output=True,
    )
    if check.returncode == 0:
        return  # Already exists

    cmd = ["agentwire", "new", "-s", task.session, "-p", task.project]
    if task.posture:
        cmd.extend(["--posture", task.posture])
    if task.roles is not None:
        cmd.extend(["--roles", ",".join(task.roles)])
    if task.model:
        cmd.extend(["--model", task.model])

    result = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=_sched._sched_config().session_create_timeout,
                            env=_unattended_env(task))
    if result.returncode == 0:
        print(f"[{_ts()}] Pre-created session: {task.session} (posture={task.posture or 'default'}, model={task.model or 'default'})")
    else:
        print(f"[{_ts()}] Warning: Failed to pre-create session {task.session}: {result.stderr.strip()}")


def _task_is_persistent(task: SchedulerTask) -> bool:
    """Whether the project task opts out of session teardown (exit_on_complete: false).

    Persistent sessions double as interactive receivers — the scheduler must
    not kill them at dispatch. `ensure` reuses the live session instead.
    """
    from ..tasks import load_task
    try:
        return not load_task(Path(task.project), task.task).exit_on_complete
    except Exception:
        return False


def _is_git_repo(project: str) -> bool:
    """True if `project` is inside a git work tree."""
    from agentwire import scheduler as _sched

    if not project:
        return False
    try:
        r = subprocess.run(
            ["git", "-C", project, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=_sched._sched_config().git_timeout,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def _is_worktree_task(task: SchedulerTask) -> bool:
    """Whether a task runs in an isolated worktree and opens a PR.

    Explicit `worktree:` wins; otherwise it auto-enables when the task's
    project is a git repo (worktree+PR is the default for repo work; set
    `worktree: false` for research/email tasks that produce no repo files).
    """
    if task.worktree is not None:
        return bool(task.worktree)
    return _is_git_repo(task.project)


def _remove_scheduler_worktree(worktree_path: str, branch: str,
                               project: str | None = None) -> None:
    """Remove a scheduler worktree, unregister it, delete its branch (best-effort).

    The canonical repo is resolved by ASKING GIT (``main_worktree``) while the
    worktree still exists — not by string-munging the ``<repo>-worktrees``
    parent, which only holds for one of the two path conventions in the wild
    and silently no-ops against the other (#855). Resolved BEFORE removal for
    the same reason: once the directory is gone, git can't answer — which is
    what ``project`` (the task's repo) is the fallback for.
    """
    from agentwire import scheduler as _sched
    from agentwire import worktree_registry
    from agentwire.worktree import main_worktree

    wt = Path(worktree_path)
    canonical: Path | None = None
    if wt.is_dir():
        resolved = main_worktree(wt)
        if resolved.is_dir() and resolved != wt:
            canonical = resolved
    if canonical is None and project:
        proj = Path(project).expanduser()
        canonical = proj if proj.is_dir() else None

    if wt.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            capture_output=True, text=True,
            timeout=_sched._sched_config().git_op_timeout,
            cwd=str(wt) if wt.is_dir() else None,
        )

    if canonical is not None:
        # Drop the registry entry created by `agentwire new` (#837) so a reaped
        # scheduler worktree doesn't linger in --list/--dangling.
        worktree_registry.unregister(
            canonical, branch=branch or None, worktree_path=str(wt),
        )
        if branch:
            subprocess.run(
                ["git", "branch", "-D", branch],
                capture_output=True, text=True, timeout=_sched._sched_config().git_timeout,
                cwd=str(canonical), check=False,
            )


def _pr_number_from_url(url: str) -> int | None:
    """Parse the trailing PR number from a `gh pr create` URL."""
    m = re.search(r"/pull/(\d+)", url.strip())
    return int(m.group(1)) if m else None


def _finalize_worktree_pr(task: SchedulerTask, task_name: str, status: str,
                          worktree_path: str, branch: str, summary: str) -> dict:
    """Commit + push + open a draft PR from an isolated worktree.

    `git add -A` is safe here — the worktree was forked fresh off the base
    branch, so there's no unrelated work to sweep. Returns
    ``{"number": int|None, "url": str}`` when a PR is opened, or ``{}`` when
    there was nothing to commit (in which case the empty worktree is removed).
    """
    from agentwire import scheduler as _sched

    cfg = _sched._sched_config()
    import shutil

    st = subprocess.run(
        ["git", "-C", worktree_path, "status", "--porcelain"],
        capture_output=True, text=True, timeout=cfg.git_timeout,
    )
    if st.returncode != 0:
        return {}
    if not st.stdout.strip():
        # Task produced no changes — don't leave an empty worktree lying around.
        _sched._remove_scheduler_worktree(worktree_path, branch, task.project)
        print(f"[{_ts()}] {task_name}: no changes — worktree removed, no PR")
        return {}

    msg = f"scheduler: {task_name} — {(summary or status).strip()[:60]}"
    subprocess.run(["git", "-C", worktree_path, "add", "-A"],
                   capture_output=True, timeout=cfg.git_timeout)
    commit = subprocess.run(
        ["git", "-C", worktree_path, "commit", "-m", msg, "--no-verify"],
        capture_output=True, text=True, timeout=cfg.git_op_timeout,
    )
    if commit.returncode != 0:
        print(f"[{_ts()}] {task_name}: commit failed: {commit.stderr.strip()}")
        return {}

    push = subprocess.run(
        ["git", "-C", worktree_path, "push", "-u", "origin", branch],
        capture_output=True, text=True, timeout=cfg.git_op_timeout,
    )
    if push.returncode != 0:
        # Keep the worktree so the work isn't lost; it'll be swept as an orphan.
        print(f"[{_ts()}] {task_name}: push failed: {push.stderr.strip()}")
        return {}

    if shutil.which("gh") is None:
        print(f"[{_ts()}] {task_name}: 'gh' not found — branch pushed, no PR")
        return {}

    pr_target = task.pr_target or task.base
    title = f"scheduler: {task_name} — {(summary or status).strip()[:50]}"
    body = (summary.strip() if summary else
            f"Automated changes from scheduled task `{task_name}` (status: {status}).")
    pr_cmd = ["gh", "pr", "create", "--base", pr_target, "--head", branch,
              "--title", title, "--body", body]
    if task.pr_draft:
        pr_cmd.append("--draft")
    pr = subprocess.run(pr_cmd, cwd=worktree_path, capture_output=True,
                        text=True, timeout=cfg.git_op_timeout)
    if pr.returncode != 0:
        print(f"[{_ts()}] {task_name}: PR creation failed: {pr.stderr.strip()}")
        return {}

    url = pr.stdout.strip()
    number = _pr_number_from_url(url)
    print(f"[{_ts()}] {task_name}: opened PR {url}")
    return {"number": number, "url": url}


def _pr_state(pr_number: int, cwd: str | None) -> str | None:
    """Return a PR's state (``OPEN``/``MERGED``/``CLOSED``) via gh, or None on error."""
    import shutil

    from agentwire import scheduler as _sched
    if shutil.which("gh") is None:
        return None
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state", "-q", ".state"],
            capture_output=True, text=True,
            timeout=_sched._sched_config().git_op_timeout, cwd=cwd,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


def reap_worktree_prs(board: Board) -> list[dict]:
    """Tear down worktrees whose PR is no longer OPEN (merged or closed).

    Kills the worker session if still alive, removes the worktree + branch,
    and clears the tracking fields so the task can run fresh next time.
    Self-limiting: only touches tasks that have an open tracked PR.
    """
    from agentwire import scheduler as _sched

    reaped: list[dict] = []
    for name, st in list(board.state.items()):
        if not st.pr_number or not st.worktree_path:
            continue

        cwd = st.worktree_path if Path(st.worktree_path).exists() else None
        if cwd is None:
            task = board.tasks.get(name)
            cwd = task.project if task and Path(task.project).exists() else None

        state = _sched._pr_state(st.pr_number, cwd)
        if state is None or state == "OPEN":
            continue  # error/unknown, or still under review — leave it be

        if st.worktree_session:
            _sched._kill_session(st.worktree_session)
        task = board.tasks.get(name)
        _sched._remove_scheduler_worktree(st.worktree_path, st.worktree_branch,
                                         task.project if task else None)

        record = {"task": name, "pr": st.pr_number, "pr_state": state,
                  "worktree": st.worktree_path}
        st.worktree_branch = ""
        st.worktree_path = ""
        st.worktree_session = ""
        st.pr_number = None
        st.pr_url = ""
        _sched.save_board(board)
        _sched._log_event("worktree_reaped", task=name, pr=record["pr"], pr_state=state)
        print(f"[{_ts()}] Reaped worktree for {name} (PR #{record['pr']} {state})")
        reaped.append(record)
    return reaped


def dispatch_task(board: Board, task_name: str) -> TaskState:
    """Run a task via `agentwire ensure` and return updated state.

    Args:
        board: Current board (for reading task config).
        task_name: Name of the task to dispatch.

    Returns:
        Updated TaskState with results.
    """
    from agentwire import scheduler as _sched

    task = board.tasks[task_name]
    existing_state = board.state.get(task_name, TaskState())

    _gated_tasks.discard(task_name)

    existing_state.last_dispatch = datetime.now(timezone.utc)
    board.state[task_name] = existing_state
    _sched.save_board(board)

    _sched._log_event("task_started", task=task_name, session=task.session,
                      project=task.project, attempt=existing_state.run_count + 1)

    return _dispatch_ensure_task(board, task, existing_state)


def _dispatch_ensure_task(board: Board, task: SchedulerTask, existing_state: TaskState) -> TaskState:
    """Dispatch via `agentwire ensure` subprocess (tmux session path)."""
    from agentwire import scheduler as _sched

    # An expired Claude login is MACHINE-wide, not per-task: every dispatch
    # after the first hits the same refusal (#906). On 2026-08-04 two tasks
    # discovered it independently and burned 7217s and 14400s doing so. Gated
    # here, above the worktree/in-place fork, because both paths cost a session
    # launch and a prompt before they would find out — the worktree one is
    # `ai-morning-briefing`, which is the run that spent four hours.
    #
    # `last_run` is deliberately NOT consumed: the outage is not the task's
    # fault and it must stay eligible the moment `/login` is run. The gate is
    # freshness-bounded (OUTAGE_TTL), so it can never wedge the board on a
    # stale file — after it, one dispatch probes and now fails in seconds.
    from ..auth_expired import outage_active

    outage = outage_active()
    if outage:
        _sched._log_event("task_skipped", task=task.name, session=task.session,
                          reason="auth_expired", detected_at=outage.get("detected_at"))
        return TaskState(
            last_run=existing_state.last_run,
            last_status="auth_expired",
            last_duration=0,
            run_count=existing_state.run_count,
            last_summary=(
                f"Claude login expired on this machine (first seen "
                f"{outage.get('detected_at')}) — dispatch skipped until `/login` is run"
            ),
        )

    if _is_worktree_task(task):
        return _dispatch_worktree_task(board, task, existing_state)
    return _dispatch_inplace_task(board, task, existing_state)


def _unattended_env(task: SchedulerTask) -> dict[str, str]:
    """Env overlay marking a headless dispatch as unattended (no human present).

    THE single chokepoint where the scheduler declares a run unattended. Every
    dispatch gets ``AGENTWIRE_UNATTENDED=1``; if the project task defines
    ``unattended_allow`` (its per-task extension to the global allowlist), those
    rule ids are passed via ``AGENTWIRE_UNATTENDED_ALLOW``. This overlay is the
    subprocess ``env=`` for the ensure/new dispatch calls; ``agentwire``'s
    ``_build_tmux_env_flags`` funnels both vars into the new tmux session before
    the agent launches, where the damage-control hook reads them. Interactive
    sessions never go through here, so the marker can't leak into them.
    """
    env = dict(os.environ)
    env["AGENTWIRE_UNATTENDED"] = "1"
    try:
        from ..tasks import load_task
        tc = load_task(Path(task.project), task.task)
        extra = getattr(tc, "unattended_allow", None)
        if extra:
            env["AGENTWIRE_UNATTENDED_ALLOW"] = ",".join(str(x) for x in extra if x)
    except Exception:
        pass
    return env


# How often the dispatch watchdog wakes to check on its child. Bounds how
# long a dead-child-with-held-pipes wedge can last (see _run_ensure).
_WATCHDOG_POLL = 30.0
# How long to wait for pipe drain after the child is known dead/killed.
_DRAIN_TIMEOUT = 10.0


def _drain_pipes(proc: "subprocess.Popen") -> tuple[str, str]:
    """Best-effort read of remaining stdout/stderr after the child is dead.

    Grandchildren (tmux, agents) inherit the pipe fds and can hold them open
    past the child's exit, so even this drain gets a bounded timeout; on
    expiry the pipes are closed and output is abandoned.
    """
    try:
        return proc.communicate(timeout=_DRAIN_TIMEOUT)
    except Exception:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        return "", ""


def _run_ensure(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, "subprocess.CompletedProcess | None", int]:
    """Run an `agentwire ensure` subprocess; return (exit_code, result, duration_s).

    Watchdogged (#677): a single dispatch may run at most
    ``dispatch_max_runtime`` seconds (0 disables) — on expiry the whole
    process group is SIGKILLed and the task reports ``timeout``, so one hung
    ensure can never starve the board. The wait also polls the child's exit
    directly: ``communicate()`` blocks on pipe EOF, not child exit, so a
    child killed externally while grandchildren hold the pipes open would
    otherwise wedge the daemon forever.
    """
    from agentwire import scheduler as _sched

    max_runtime = getattr(_sched._sched_config(), "dispatch_max_runtime", 14400)
    start_time = time.time()
    result = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, env=env,
        )
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_WATCHDOG_POLL)
                exit_code = proc.returncode
                break
            except subprocess.TimeoutExpired:
                if proc.poll() is not None:
                    # Child already exited but orphaned grandchildren hold the
                    # pipes open — reap what's left and take the real exit code.
                    # killpg still reaches group members reparented to init.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    _kill_process_tree(proc.pid)
                    stdout, stderr = _drain_pipes(proc)
                    exit_code = proc.returncode
                    break
                if max_runtime and (time.time() - start_time) >= max_runtime:
                    # Watchdog ceiling: kill the entire process group
                    # (start_new_session=True makes pgid == pid), then any
                    # stragglers that escaped it.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    _kill_process_tree(proc.pid)
                    stdout, stderr = _drain_pipes(proc)
                    print(f"[{_ts()}] Dispatch watchdog: killed hung ensure "
                          f"(pid {proc.pid}) after {int(time.time() - start_time)}s")
                    exit_code = _EXIT_TIMEOUT
                    break
        result = subprocess.CompletedProcess(cmd, exit_code, stdout, stderr)
    except Exception:
        exit_code = _EXIT_FAILED
    return exit_code, result, int(time.time() - start_time)


def _notify_dispatch_timeout(task: SchedulerTask, session: str, duration: int) -> None:
    """Surface a watchdog kill: distinct event + best-effort owner email.

    Reuses the outbound email channel (same wiring as usage-limit parks and
    unattended blocks) so a killed dispatch is never a silent failure.
    """
    from agentwire import scheduler as _sched

    _sched._log_event("dispatch_timeout", task=task.name, session=session,
                      duration=duration)
    try:
        import socket

        from ..channels.email import send_email
        hours = duration / 3600
        send_email(
            subject=f"[agentwire] scheduler watchdog killed hung task: {task.name}",
            body=(
                f"Dispatch of **{task.name}** (session `{session}`) on "
                f"`{socket.gethostname()}` exceeded the dispatch_max_runtime "
                f"ceiling and was killed after {hours:.1f}h.\n\n"
                f"- **Project:** {task.project}\n"
                f"- **Task:** {task.task}\n\n"
                "The task was marked `timeout` and the scheduler moved on. "
                "Common cause: the agent session wedged on an unrecognized "
                "interactive dialog (e.g. a usage-limit variant) — check "
                "`agentwire limits status` and the session scrollback."
            ),
        )
    except Exception:
        pass


def _apply_max_runs(board: Board, task: SchedulerTask, new_state: TaskState,
                    new_run_count: int) -> None:
    """Auto-disable a task that has hit its max_runs cap."""
    from agentwire import scheduler as _sched

    if task.max_runs is not None and new_run_count >= task.max_runs:
        task.enabled = False
        board.state[task.name] = new_state
        _sched.save_board(board)
        _sched._log_event("task_disabled", task=task.name, reason="max_runs_reached",
                          run_count=new_run_count, max_runs=task.max_runs)


def _dispatch_inplace_task(board: Board, task: SchedulerTask, existing_state: TaskState) -> TaskState:
    """Run a non-worktree task in place. Produces no commits — side effects
    only (email, files outside the repo). The repo is never modified."""
    from agentwire import scheduler as _sched

    task_name = task.name

    # A session parked on a usage limit is waiting out the reset — never
    # kill it or dispatch into it. Skip without consuming last_run so the
    # task stays eligible once the park clears.
    from ..usage_limit import is_parked
    if is_parked(task.session):
        _sched._log_event("task_skipped", task=task_name, session=task.session,
                          reason="usage_limit_parked")
        return TaskState(
            last_run=existing_state.last_run,
            last_status="usage_limit",
            last_duration=0,
            run_count=existing_state.run_count,
        )

    # Clean stale lock for this session before dispatching.
    from ..locking import remove_stale_lock
    remove_stale_lock(task.session)

    # Tasks with exit_on_complete: false keep a long-lived session that may be
    # in interactive use — never kill it at dispatch; ensure reuses it as-is.
    persistent = _task_is_persistent(task)

    has_overrides = bool(task.posture or task.roles is not None or task.model)
    if has_overrides:
        # Scheduler has posture/role overrides — kill + pre-create ourselves,
        # then let ensure reuse the session (no --fresh)
        if not persistent:
            _sched._kill_session(task.session)
        _sched._pre_create_session(task)  # no-op if the session already exists

    cmd = [
        "agentwire", "ensure",
        "-s", task.session,
        "--task", task.task,
        "--project", task.project,
        "--json",
    ]
    if not has_overrides and not persistent:
        # No type/role overrides — kill stale session so ensure creates fresh
        _sched._kill_session(task.session)

    exit_code, result, duration = _sched._run_ensure(cmd, env=_unattended_env(task))
    status = _EXIT_TO_STATUS.get(exit_code, "failed")

    # On lock conflict, don't update last_run so task remains eligible
    if exit_code == _EXIT_LOCK_CONFLICT:
        _sched._log_event("task_skipped", task=task_name, session=task.session,
                          reason="lock_conflict")
        return TaskState(
            last_run=existing_state.last_run,
            last_status="lock_conflict",
            last_duration=duration,
            run_count=existing_state.run_count,
        )

    if exit_code == _EXIT_TIMEOUT:
        # Watchdog kill — the agent session is wedged, not working. Tear it
        # down (persistent sessions excepted — they double as interactive
        # receivers) and make the failure loud.
        if not persistent:
            _sched._kill_session(task.session)
        _sched._notify_dispatch_timeout(task, task.session, duration)

    summary, files_modified, blockers_list = _sched._parse_ensure_summary(task, result)

    _sched._log_event("task_completed", task=task_name, session=task.session,
                      status=status, duration=duration, summary=summary,
                      files_modified=files_modified, blockers=blockers_list)
    _sched._notify_portal(task_name, status, duration, summary)

    new_run_count = existing_state.run_count + 1
    new_state = TaskState(
        last_run=datetime.now(timezone.utc),
        last_status=status,
        last_duration=duration,
        run_count=new_run_count,
        last_summary=summary,
        last_gate_commit=_sched._capture_head(task.project),
    )
    _apply_max_runs(board, task, new_state, new_run_count)
    return new_state


def _dispatch_worktree_task(board: Board, task: SchedulerTask, existing_state: TaskState) -> TaskState:
    """Run a task in a fresh git worktree forked off `base`, then commit +
    push + open a draft PR. The live project folder is never touched, and
    `git add -A` in the worktree is safe (no unrelated work to sweep)."""
    from agentwire import scheduler as _sched

    task_name = task.name
    from ..locking import remove_stale_lock
    cfg = _sched._sched_config()

    proj = Path(task.project)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"scheduler-{task_name}-{ts}"
    wt_session = f"{proj.name}/{branch}"  # slash → ensure/new treat as worktree

    remove_stale_lock(wt_session)
    _sched._kill_session(wt_session)  # paranoia — a timestamped session shouldn't exist

    # Create the worktree + session off the latest origin/<base>.
    # --kind orchestrator: a project/branch name would otherwise derive the
    # 'worker' kind on worktree topology, whose etiquette tells the agent to
    # open its OWN draft PR + notify. But the scheduler is the deterministic
    # finalizer (_finalize_worktree_pr opens + tracks the PR; reap_worktree_prs
    # only reaps TRACKED PRs) — an agent-opened PR would escape the reaper and
    # leak. The replaceable orchestrator kind lets the task's own roles win,
    # so no worker-worktree PR-opening contract is injected.
    new_cmd = [
        "agentwire", "new", "-s", wt_session, "-p", task.project,
        "--base", task.base, "--pull-first", "--kind", "orchestrator", "--json",
    ]
    if task.posture:
        new_cmd += ["--posture", task.posture]
    if task.roles is not None:
        new_cmd += ["--roles", ",".join(task.roles)]
    if task.model:
        new_cmd += ["--model", task.model]

    unattended_env = _unattended_env(task)
    worktree_path = ""
    new_res = None
    try:
        new_res = subprocess.run(new_cmd, capture_output=True, text=True,
                                 timeout=cfg.session_create_timeout,
                                 env=unattended_env)
        if new_res.returncode == 0:
            worktree_path = (json.loads(new_res.stdout) or {}).get("path", "")
    except Exception:
        worktree_path = ""

    if not worktree_path:
        err = (new_res.stderr or new_res.stdout).strip()[:200] if new_res else ""
        _sched._log_event("task_failed", task=task_name, session=wt_session,
                          reason=f"worktree create failed: {err}")
        print(f"[{_ts()}] {task_name}: worktree create failed")
        new_run_count = existing_state.run_count + 1
        return TaskState(
            last_run=datetime.now(timezone.utc), last_status="failed",
            last_duration=0, run_count=new_run_count,
            last_summary="worktree creation failed",
        )

    # Run the task inside the worktree.
    cmd = [
        "agentwire", "ensure", "-s", wt_session, "--task", task.task,
        "--project", worktree_path, "--json",
    ]
    exit_code, result, duration = _sched._run_ensure(cmd, env=unattended_env)
    status = _EXIT_TO_STATUS.get(exit_code, "failed")

    if exit_code == _EXIT_LOCK_CONFLICT:
        _sched._log_event("task_skipped", task=task_name, session=wt_session,
                          reason="lock_conflict")
        _sched._remove_scheduler_worktree(worktree_path, branch, task.project)  # task didn't run
        return TaskState(
            last_run=existing_state.last_run, last_status="lock_conflict",
            last_duration=duration, run_count=existing_state.run_count,
        )

    if exit_code == _EXIT_TIMEOUT:
        # Watchdog kill — tear down the wedged worktree session and make the
        # failure loud. The worktree itself flows into _finalize_worktree_pr
        # below, so any partial work is preserved as a draft PR.
        _sched._kill_session(wt_session)
        _sched._notify_dispatch_timeout(task, wt_session, duration)

    summary, files_modified, blockers_list = _sched._parse_ensure_summary(
        task, result, project=worktree_path, session=wt_session)

    _sched._log_event("task_completed", task=task_name, session=wt_session,
                      status=status, duration=duration, summary=summary,
                      files_modified=files_modified, blockers=blockers_list)
    _sched._notify_portal(task_name, status, duration, summary)

    if status == "auth_expired":
        # The turn was refused before any model ran, so the worktree is
        # untouched — there is nothing to finalize and a PR would be empty
        # (#906). `last_run` IS consumed here, unlike the pre-dispatch gate:
        # this attempt really did spend a session launch, and the machine-wide
        # state recorded by the detection now short-circuits the rest of the
        # fleet. Left alive for the same reason usage_limit is — the operator
        # runs `/login` and the session picks up where it stopped.
        print(f"[{_ts()}] {task_name}: Claude login expired — worktree + session left in place")
        return TaskState(
            last_run=datetime.now(timezone.utc),
            last_status="auth_expired",
            last_duration=duration,
            run_count=existing_state.run_count + 1,
            last_summary=summary,
            last_gate_commit=_sched._capture_head(task.project),
        )

    if status == "usage_limit":
        # Session parked mid-task — committing/pushing half-done work would be
        # wrong, and the session must stay alive for the watchdog to resume.
        # The worktree is left in place; the session finishes via the idle
        # hook after resume (PR must then be opened manually — see docs).
        print(f"[{_ts()}] {task_name}: parked on usage limit — worktree + session left in place")
        new_run_count = existing_state.run_count + 1
        return TaskState(
            last_run=datetime.now(timezone.utc),
            last_status="usage_limit",
            last_duration=duration,
            run_count=new_run_count,
            last_summary=summary,
            last_gate_commit=_sched._capture_head(task.project),
        )

    pr = _finalize_worktree_pr(task, task_name, status, worktree_path, branch, summary)

    new_run_count = existing_state.run_count + 1
    new_state = TaskState(
        last_run=datetime.now(timezone.utc),
        last_status=status,
        last_duration=duration,
        run_count=new_run_count,
        last_summary=summary,
        last_gate_commit=_sched._capture_head(task.project),
        worktree_branch=branch if pr else "",
        worktree_path=worktree_path if pr else "",
        worktree_session=wt_session if pr else "",
        pr_number=pr.get("number") if pr else None,
        pr_url=pr.get("url", "") if pr else "",
    )
    _apply_max_runs(board, task, new_state, new_run_count)
    return new_state


def _capture_head(project: str) -> str:
    """Return the current HEAD SHA of a git repo, or '' if unavailable."""
    from agentwire import scheduler as _sched

    if not project:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", project, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=_sched._sched_config().git_timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


# Workflow dispatch ---------------------------------------------------------

_WORKFLOW_STATUS_TO_SCHED = {
    "success": "complete",
    "partial": "incomplete",
    "failure": "failed",
}
