"""The expired-login detector fires through the REAL dispatch paths (#906).

`tests/unit/test_auth_expired.py` proves the detector recognises the real
2026-08-04 transcript. That is necessary and not sufficient: #867's cost came
from `wait_for_completion_signal` polling forever and the scheduler dispatching
into the outage again, so what has to be proven here is that those two
functions — the actual ones, not stand-ins — change behaviour.

Every test below drives the shipped code path:

* ``wait_for_completion_signal`` with a real transcript on disk, asserting it
  RETURNS ``auth_expired`` instead of looping until the session dies. A test
  that patched the detector and asserted on a return value would pass against
  a `completion.py` that never called it.
* ``_dispatch_ensure_task`` with an outage recorded, asserting `ensure` is
  never invoked and ``last_run`` is not consumed.
"""

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from test_auth_expired import HUNG_RUN, write_transcript

from agentwire import auth_expired
from agentwire.completion import status_to_exit_code, wait_for_completion_signal
from agentwire.ensure_cli import ENSURE_EXIT_AUTH_EXPIRED
from agentwire.scheduler.models import _EXIT_TO_STATUS, SchedulerTask, TaskState


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    monkeypatch.setattr("agentwire.history.PROJECTS_DIR", tmp_path / "projects")
    ok = type("R", (), {"success": True, "error": None})()
    with patch("agentwire.channels.email.send_email", return_value=ok):
        yield tmp_path


class TestCompletionWaitReturnsInsteadOfHanging:
    def test_real_wait_loop_reports_the_cause(self, env, monkeypatch):
        """The load-bearing integration assertion for #867.

        Drives the SHIPPED `wait_for_completion_signal` against the shape of
        the run that hung: session alive, agent process running, no usage-limit
        dialog, no summary file — every check that existed before #906 passes
        forever. It must now return a named cause in one tick.
        """
        from agentwire.history import encode_project_path

        project = env / "proj"
        project.mkdir()
        write_transcript(
            env / "projects" / encode_project_path(str(project)) / "conv.jsonl",
            HUNG_RUN,
        )
        # Everything the old loop looked at says "healthy, keep waiting".
        monkeypatch.setattr("agentwire.completion._session_has_agent", lambda s: True)
        monkeypatch.setattr("agentwire.usage_limit.check_and_park",
                            lambda *a, **k: False)

        started = time.time()
        result = wait_for_completion_signal(
            "memory-manager",
            poll_interval=0.01,
            summary_path=project / "task-summary-memory-manager-x-2026-08-04T08-00-00.md",
            # The real anchor: the attempt began before the prompt was sent,
            # so the refusal recorded 15ms later is inside the window.
            transcript_since=started - 60,
            # Bounds the un-detected case so a regression FAILS instead of
            # hanging: without the detector this loop has no other exit (the
            # session is alive, no dialog, no summary) — which is precisely
            # #867, and a test that reproduces it by hanging CI reports
            # nothing. Measured: mutating the detector out turned this from a
            # 1s pass into a 600s timeout until this bound was added.
            max_duration=2,
        )
        assert time.time() - started < 5, "must not poll on a turn that was refused"
        assert result["status"] == "auth_expired"
        assert "Login expired" in result["summary"]
        assert "authentication_failed" in result["summary"]
        assert "conv.jsonl" in result["summary"], "names the evidence"

    def test_the_outage_is_recorded_so_the_fleet_can_be_gated(self, env, monkeypatch):
        """Detection is machine-wide, not per-task — that is the #906 ask."""
        from agentwire.history import encode_project_path

        project = env / "proj"
        project.mkdir()
        write_transcript(
            env / "projects" / encode_project_path(str(project)) / "conv.jsonl", HUNG_RUN)
        monkeypatch.setattr("agentwire.completion._session_has_agent", lambda s: True)
        monkeypatch.setattr("agentwire.usage_limit.check_and_park", lambda *a, **k: False)

        assert auth_expired.outage_active() is None
        wait_for_completion_signal(
            "memory-manager", poll_interval=0.01,
            summary_path=project / "task-summary-a-b-2026-08-04T08-00-00.md",
            transcript_since=time.time() - 60, max_duration=2)
        outage = auth_expired.outage_active()
        assert outage is not None
        assert "memory-manager" in outage["sessions"]

    def test_the_window_starts_at_the_attempt_not_at_the_wait(self, env, monkeypatch):
        """Regression for the window bug this integration test caught.

        The refusal is written ~15ms after the prompt submits; `send_verified`
        then spends seconds confirming submission before the wait is entered.
        Anchoring the transcript window at the wait's own start therefore puts
        the evidence just BEFORE the window — the detector would have been
        blind to the exact run it was built for. `ensure` passes the attempt's
        start instead; this proves the two anchors actually differ.
        """
        from agentwire.history import encode_project_path

        project = env / "proj"
        project.mkdir()
        write_transcript(
            env / "projects" / encode_project_path(str(project)) / "conv.jsonl", HUNG_RUN)
        monkeypatch.setattr("agentwire.completion._session_has_agent", lambda s: False)
        monkeypatch.setattr("agentwire.usage_limit.check_and_park", lambda *a, **k: False)
        summary = project / "task-summary-a-b-2026-08-04T08-00-00.md"

        # Wait-anchored (the default): transcript predates the floor, missed.
        from agentwire.completion import CompletionTimeout

        with pytest.raises(CompletionTimeout):
            wait_for_completion_signal("s", poll_interval=0.01, summary_path=summary)
        assert auth_expired.outage_active() is None

        # Attempt-anchored (what ensure passes): seen.
        result = wait_for_completion_signal(
            "s", poll_interval=0.01, summary_path=summary,
            transcript_since=time.time() - 60, max_duration=2)
        assert result["status"] == "auth_expired"

    def test_a_successful_turn_reopens_the_gate(self, env, monkeypatch):
        """The hook behind "reopens on the first successful turn".

        `doctor` and the escalation email both make that promise to an
        operator. Before this hook existed the gate stayed shut until
        `last_seen + OUTAGE_TTL` no matter what, so the text described
        behavior nothing implemented — which is #906's own defect one scale
        down, and would misdirect the next reader exactly the way
        "incomplete — Timeout waiting for task completion" misdirected a day
        of investigation.
        """
        auth_expired.record_outage({"session": "memory-manager", "transcript": "/t"})
        assert auth_expired.outage_active() is not None

        project = env / "proj"
        project.mkdir()
        summary = project / "task-summary-a-b-2026-08-05T04-00-00.md"
        summary.write_text("---\nstatus: complete\nsummary: did the thing\n---\n")
        monkeypatch.setattr("agentwire.completion._session_has_agent", lambda s: True)
        monkeypatch.setattr("agentwire.usage_limit.check_and_park", lambda *a, **k: False)

        result = wait_for_completion_signal("s", poll_interval=0.01, summary_path=summary)
        assert result["status"] == "complete"
        assert auth_expired.outage_active() is None, "the promise must be true"
        assert auth_expired.read_state() is None, "the record itself is gone"

    def test_clearing_a_gate_that_was_never_set_is_harmless(self, env, monkeypatch):
        """The hook runs on EVERY successful completion, outage or not."""
        project = env / "proj"
        project.mkdir()
        summary = project / "task-summary-a-b-2026-08-05T04-00-00.md"
        summary.write_text("---\nstatus: complete\nsummary: fine\n---\n")
        monkeypatch.setattr("agentwire.completion._session_has_agent", lambda s: True)
        monkeypatch.setattr("agentwire.usage_limit.check_and_park", lambda *a, **k: False)

        assert auth_expired.read_state() is None
        assert wait_for_completion_signal(
            "s", poll_interval=0.01, summary_path=summary)["status"] == "complete"

    def test_a_healthy_session_is_untouched(self, env, monkeypatch):
        """No transcript failure → the loop behaves exactly as before.

        Guards the direction that matters most: this check runs on every tick
        of every task, and a false positive ends a healthy run.
        """
        project = env / "proj"
        project.mkdir()
        summary = project / "task-summary-a-b-2026-08-04T08-00-00.md"
        summary.write_text("## Status: complete\n\nDid the thing.\n")
        monkeypatch.setattr("agentwire.completion._session_has_agent", lambda s: True)
        monkeypatch.setattr("agentwire.usage_limit.check_and_park", lambda *a, **k: False)

        result = wait_for_completion_signal("s", poll_interval=0.01, summary_path=summary)
        assert result["status"] != "auth_expired"
        assert auth_expired.outage_active() is None


class TestEnsureWiresTheAnchorAndStopsRetrying:
    """Drives the real `_run_ensure_task` — a wire that isn't connected is
    invisible to a test that only exercises `wait_for_completion_signal`."""

    def _run(self, tmp_path, signal, task_overrides=None):
        from types import SimpleNamespace

        from agentwire import ensure_cli
        from agentwire.tasks import parse_task_config
        from agentwire.templating import TemplateContext

        task = parse_task_config("t", {"prompt": "do the thing",
                                       **(task_overrides or {})})
        ctx = TemplateContext(session="s", task="t", project_root=str(tmp_path))
        args = SimpleNamespace(session="s", task="t")
        (tmp_path / ".agentwire").mkdir(exist_ok=True)

        with patch.object(ensure_cli, "send_task_prompt", return_value=True) as send, \
             patch("agentwire.ensure_cli.tmux_session_exists", return_value=True), \
             patch("agentwire.session_ready.wait_for_session_ready", return_value=True), \
             patch("agentwire.completion.wait_for_completion_signal") as wait, \
             patch("agentwire.completion.write_task_context"), \
             patch("agentwire.completion.clear_task_context"), \
             patch("agentwire.ensure_cli.subprocess.run"), \
             patch("agentwire.ensure_cli.time.sleep"):
            wait.return_value = signal
            rc = ensure_cli._run_ensure_task(
                args, "s", task, ctx, "/bin/sh", tmp_path, json_mode=False)
        return rc, send, wait

    def test_the_attempt_anchor_is_passed_down(self, env, tmp_path):
        """Not the wait's own start — see the window regression above."""
        before = time.time()
        _, _, wait = self._run(tmp_path, {"status": "complete", "summary": "ok"})
        since = wait.call_args.kwargs["transcript_since"]
        assert since is not None, "ensure must anchor the window at the attempt"
        assert before <= since <= time.time()

    def test_auth_expired_is_not_retried(self, env, tmp_path, capsys):
        """Every retry refuses identically — spending them is pure waste."""
        rc, send, _ = self._run(
            tmp_path,
            {"status": "auth_expired", "summary": "Claude login expired — …"},
            task_overrides={"retries": 3},
        )
        assert send.call_count == 1, "no re-launch, no re-prompt into a dead login"
        assert rc == ENSURE_EXIT_AUTH_EXPIRED
        assert "Login expired" in capsys.readouterr().out

    def test_an_ordinary_failure_still_retries(self, env, tmp_path):
        """Guard the blast radius: only auth_expired short-circuits."""
        rc, send, _ = self._run(
            tmp_path, {"status": "failed", "summary": "nope"},
            task_overrides={"retries": 1})
        assert send.call_count == 2


class TestSchedulerGatesTheRestOfTheFleet:
    def _task(self):
        return SchedulerTask(name="ai-morning-briefing", project="/tmp/p",
                             session="ai-briefing", task="briefing")

    def test_dispatch_is_skipped_while_the_outage_is_fresh(self, env):
        """On 08-04 the second task discovered the outage by burning 14400s."""
        auth_expired.record_outage({"session": "memory-manager", "transcript": "/t"})
        prior = TaskState(last_run=datetime(2026, 8, 4, tzinfo=timezone.utc), run_count=7)

        from agentwire.scheduler.dispatch import _dispatch_ensure_task

        with patch("agentwire.scheduler.dispatch._dispatch_worktree_task") as wt, \
             patch("agentwire.scheduler.dispatch._dispatch_inplace_task") as ip:
            state = _dispatch_ensure_task(None, self._task(), prior)

        wt.assert_not_called()
        ip.assert_not_called(), "no session launch, no prompt, no ceiling to burn"
        assert state.last_status == "auth_expired"
        assert state.last_run == prior.last_run, "stays eligible the moment /login runs"
        assert state.run_count == prior.run_count
        assert "login expired" in state.last_summary.lower()

    def test_dispatch_resumes_once_the_outage_goes_stale(self, env):
        """The gate must never be able to wedge the board permanently."""
        auth_expired.record_outage({"session": "memory-manager"})
        state = auth_expired.read_state()
        state["last_seen"] = (
            auth_expired._now() - auth_expired.OUTAGE_TTL - auth_expired.OUTAGE_TTL
        ).isoformat()
        auth_expired.write_state(state)

        from agentwire.scheduler.dispatch import _dispatch_ensure_task

        with patch("agentwire.scheduler.dispatch._dispatch_inplace_task",
                   return_value=TaskState(last_status="complete")) as ip:
            result = _dispatch_ensure_task(None, self._task(), TaskState())
        ip.assert_called_once(), "one probe is allowed through"
        assert result.last_status == "complete"

    def test_no_outage_means_no_change_in_behaviour(self, env):
        from agentwire.scheduler.dispatch import _dispatch_ensure_task

        with patch("agentwire.scheduler.dispatch._dispatch_inplace_task",
                   return_value=TaskState(last_status="complete")) as ip:
            _dispatch_ensure_task(None, self._task(), TaskState())
        ip.assert_called_once()


class TestExitCodeIsDistinct:
    def test_auth_expired_is_not_a_timeout(self, env):
        """`incomplete` sent three investigations at the wrong subsystem."""
        assert status_to_exit_code("auth_expired") == ENSURE_EXIT_AUTH_EXPIRED == 8
        assert status_to_exit_code("incomplete") == 2
        assert status_to_exit_code("usage_limit") == 7

    def test_the_scheduler_maps_the_code_back(self, env):
        """A code ensure emits that the scheduler cannot read is a silent drop."""
        assert _EXIT_TO_STATUS[ENSURE_EXIT_AUTH_EXPIRED] == "auth_expired"
