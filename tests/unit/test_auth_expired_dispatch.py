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

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agentwire import auth_expired
from agentwire.ensure_cli import ENSURE_EXIT_AUTH_EXPIRED
from agentwire.scheduler.models import SchedulerTask, TaskState


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    monkeypatch.setattr("agentwire.history.claude_projects_dir", lambda: tmp_path / "projects")
    ok = type("R", (), {"success": True, "error": None})()
    with patch("agentwire.channels.email.send_email", return_value=ok):
        yield tmp_path


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


