"""Wall-clock bound on a task attempt, and loud phantom task keys (#867).

`memory-manager` ran 2h unattended and reported `incomplete — Timeout waiting
for task completion`, having set `max_duration: 1800` in its
`.agentwire.tasks.yml`. That field was read by nothing: completion is agent-
driven (idle hook → summary file), and the wait had no wall clock at all, so an
agent that never goes idle was waited on until something else killed it.

Two things are covered here:

* `max_duration` is enforced, and its expiry is distinguishable on the board
  from "the session died" — the two used to share one summary line.
* A key in a task block that agentwire doesn't read is reported rather than
  silently dropped, which is what let `max_duration` look configured for the
  whole life of the file.
"""

from unittest.mock import MagicMock, patch

import pytest

from agentwire.completion import CompletionTimeout, wait_for_completion_signal






class _Clock:
    """Monotonic fake clock — one tick per call, so the wait can't run real time."""

    def __init__(self, step: float = 10.0):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class TestWaitHonorsMaxDuration:
    def _patches(self, has_agent=True):
        return (
            patch("agentwire.completion._session_has_agent", return_value=has_agent),
            patch("agentwire.usage_limit.check_and_park", return_value=False),
        )

    def test_expiry_raises_with_a_named_reason(self):
        agent, park = self._patches()
        with agent, park, \
             patch("agentwire.completion.time.sleep"), \
             patch("agentwire.completion.time.time", _Clock(step=10.0)):
            with pytest.raises(CompletionTimeout) as exc:
                wait_for_completion_signal("s", poll_interval=0, max_duration=10)
        assert "max_duration (10s)" in str(exc.value)
        assert "never signalled completion" in str(exc.value)






class TestVacuousWaitDoesNotHang:
    """Hypothesis 3 from #867: a fan-out that produced zero children must not
    leave the parent blocked on a join that never resolves."""



    def test_cli_reports_nothing_to_wait_on_and_exits_zero(self, tmp_path, monkeypatch, capsys):
        from agentwire import cohort
        from agentwire.wait_cli import cmd_wait

        monkeypatch.setattr(cohort, "COHORT_ROOT", tmp_path / "cohorts")
        args = MagicMock()
        args.session = "memory-manager"
        args.json = False
        args.timeout = 999
        rc = cmd_wait(args)
        assert rc == 0
        assert "nothing to wait on" in capsys.readouterr().out
