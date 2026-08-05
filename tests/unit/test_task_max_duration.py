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
from agentwire.tasks import KNOWN_TASK_KEYS, parse_task_config, validate_task


class TestMaxDurationParsing:
    def test_defaults_to_unbounded(self):
        task = parse_task_config("t", {"prompt": "go"})
        assert task.max_duration == 0

    def test_parsed_from_config(self):
        task = parse_task_config("t", {"prompt": "go", "max_duration": 1800})
        assert task.max_duration == 1800

    def test_negative_is_a_validation_issue(self):
        task = parse_task_config("t", {"prompt": "go", "max_duration": -1})
        assert any("max_duration" in i for i in validate_task(task))

    def test_zero_is_valid(self):
        task = parse_task_config("t", {"prompt": "go", "max_duration": 0})
        assert validate_task(task) == []

    def test_max_duration_is_a_known_key(self):
        """The regression this whole PR exists for."""
        assert "max_duration" in KNOWN_TASK_KEYS


class TestUnknownTaskKeys:
    def test_known_keys_produce_no_warning(self):
        task = parse_task_config("t", {
            "prompt": "go", "idle_timeout": 300, "max_duration": 1800,
            "exit_on_complete": True, "pre": {"x": "echo hi"},
        })
        assert task.unknown_keys == []

    def test_unknown_key_is_recorded(self):
        task = parse_task_config("t", {"prompt": "go", "max_durationn": 1800})
        assert task.unknown_keys == ["max_durationn"]

    def test_unknown_keys_are_sorted_and_deduped(self):
        task = parse_task_config("t", {"prompt": "go", "zeta": 1, "alpha": 2})
        assert task.unknown_keys == ["alpha", "zeta"]

    def test_unknown_keys_never_fail_validation(self):
        """A typo must not break a 04:00 dispatch — ensure hard-fails on issues."""
        task = parse_task_config("t", {"prompt": "go", "nonsense": True})
        assert validate_task(task) == []


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

    def test_wait_continues_while_inside_the_budget(self):
        """The clock must not end the wait early — only at or past the ceiling."""
        agent, park = self._patches()
        clock = _Clock(step=1.0)
        with agent, park, \
             patch("agentwire.completion.time.sleep") as sleep, \
             patch("agentwire.completion.time.time", clock):
            with pytest.raises(CompletionTimeout):
                wait_for_completion_signal("s", poll_interval=0, max_duration=5)
        # started=1; expiry at t=6 → polls at 2,3,4,5 slept through first.
        assert sleep.call_count == 4

    def test_zero_max_duration_does_not_bound_the_wait(self):
        """Unbounded stays unbounded — the wait only ends when the session dies."""
        agent, park = self._patches(has_agent=False)
        with agent, park:
            with pytest.raises(CompletionTimeout) as exc:
                wait_for_completion_signal("s", poll_interval=0, max_duration=0)
        # The session-died path, not a duration expiry.
        assert "died or agent exited" in str(exc.value)
        assert "max_duration" not in str(exc.value)

    def test_a_dead_session_still_wins_over_the_clock(self):
        agent, park = self._patches(has_agent=False)
        with agent, park, patch("agentwire.completion.time.sleep"):
            with pytest.raises(CompletionTimeout) as exc:
                wait_for_completion_signal("s", poll_interval=0, max_duration=1)
        assert "died or agent exited" in str(exc.value)

    def test_summary_before_expiry_returns_normally(self, tmp_path):
        summary = tmp_path / "task-summary-s-t-2026-08-04T04-00-00.md"
        summary.write_text("---\nstatus: complete\nsummary: done\n---\n")
        agent, park = self._patches()
        with agent, park, \
             patch("agentwire.completion.TASKS_DIR", tmp_path / "nope"):
            result = wait_for_completion_signal(
                "s", poll_interval=0, summary_path=summary, max_duration=1800)
        assert result["status"] == "complete"


class TestVacuousWaitDoesNotHang:
    """Hypothesis 3 from #867: a fan-out that produced zero children must not
    leave the parent blocked on a join that never resolves."""

    def test_wait_with_no_ledger_returns_immediately_resolved(self, tmp_path, monkeypatch):
        from agentwire import cohort

        monkeypatch.setattr(cohort, "COHORT_ROOT", tmp_path / "cohorts")
        result = cohort.wait("memory-manager", timeout=999)
        assert result["resolved"] is True
        assert result["cohort"] is False
        assert result["pending"] == []

    def test_wait_does_not_sleep_when_there_is_no_cohort(self, tmp_path, monkeypatch):
        from agentwire import cohort

        monkeypatch.setattr(cohort, "COHORT_ROOT", tmp_path / "cohorts")
        with patch("agentwire.cohort.time.sleep") as sleep:
            cohort.wait("memory-manager", timeout=999)
        sleep.assert_not_called()

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
