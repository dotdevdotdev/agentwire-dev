"""Integration tests for scheduler board load/save round-trip and scheduling logic."""

import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agentwire.scheduler import (
    Board,
    Schedule,
    SchedulerTask,
    TaskState,
    _compute_next_eligible,
    _in_time_window,
    _is_in_flight,
    format_schedule,
    get_board_display,
    load_board,
    save_board,
    validate_board,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def board_env(tmp_path):
    """Set up a scheduler board file and mock config to point at it."""
    board_path = tmp_path / "scheduler.yaml"
    shutil.copy(FIXTURES_DIR / "sample_scheduler.yaml", board_path)

    # Mock the scheduler config to use our temp path
    class FakeSchedulerConfig:
        board_file = board_path
        events_file = tmp_path / "events.jsonl"
        live_state_file = tmp_path / "live.json"
        git_timeout = 10
        git_op_timeout = 15
        gate_timeout = 10
        portal_notify_timeout = 5
        session_create_timeout = 30
        max_loop_sleep = 60
        dispatch_cooldown = 60

    with patch("agentwire.scheduler._sched_config", return_value=FakeSchedulerConfig()):
        yield board_path


class TestSchedulerBoardRoundTrip:
    def test_load_board(self, board_env):
        board = load_board()
        assert "code-quality" in board.tasks
        assert "doc-drift" in board.tasks
        assert "disabled-task" in board.tasks
        assert board.tasks["doc-drift"].filler is True
        assert board.tasks["disabled-task"].enabled is False

    def test_schedule_parsed(self, board_env):
        board = load_board()
        sched = board.tasks["code-quality"].schedule
        assert sched.every == "1h"
        assert sched.at is None
        assert sched.after is None

    def test_dep_task_parsed(self, board_env):
        board = load_board()
        sched = board.tasks["dep-task"].schedule
        assert sched.after == "code-quality"
        assert sched.delay == 1800  # 30m
        assert sched.cooldown == 7200  # 2h

    def test_daily_task_parsed(self, board_env):
        board = load_board()
        sched = board.tasks["daily-task"].schedule
        assert sched.every == "day"
        assert sched.at == "08:00"
        assert sched.except_days == ["saturday", "sunday"]

    def test_state_parsed(self, board_env):
        board = load_board()
        state = board.state.get("code-quality")
        assert state is not None
        assert state.last_status == "complete"
        assert state.run_count == 5
        assert state.last_duration == 120

    def test_round_trip_preserves_state(self, board_env):
        board = load_board()

        # Mutate state
        board.state["code-quality"] = TaskState(
            last_run=datetime(2026, 2, 1, 15, 0, 0, tzinfo=timezone.utc),
            last_status="failed",
            last_duration=300,
            run_count=6,
            last_summary="Something broke",
            last_dispatch=datetime(2026, 2, 1, 14, 55, 0, tzinfo=timezone.utc),
        )

        save_board(board)

        # Reload and verify
        board2 = load_board()
        state = board2.state["code-quality"]
        assert state.last_status == "failed"
        assert state.run_count == 6
        assert state.last_duration == 300
        assert state.last_summary == "Something broke"
        assert state.last_dispatch is not None

        # Task definitions should be unchanged
        assert board2.tasks["code-quality"].schedule.every == "1h"
        assert board2.tasks["doc-drift"].filler is True


# Pure parser tests for _parse_duration / _parse_time / _day_matches live in
# tests/unit/test_scheduler_parsing.py — they have no scheduler-board state
# coupling.


class TestInTimeWindow:
    def test_within_window(self):
        sched = Schedule(not_before="06:00", not_after="22:00")
        with patch("agentwire.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 16, 12, 0)
            assert _in_time_window(sched) is True

    def test_before_window(self):
        sched = Schedule(not_before="08:00", not_after="22:00")
        with patch("agentwire.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 16, 5, 0)
            assert _in_time_window(sched) is False

    def test_after_window(self):
        sched = Schedule(not_before="08:00", not_after="22:00")
        with patch("agentwire.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 16, 23, 0)
            assert _in_time_window(sched) is False

    def test_no_window(self):
        sched = Schedule()
        assert _in_time_window(sched) is True


class TestIsInFlight:
    def test_no_dispatch(self):
        state = TaskState()
        assert _is_in_flight(state) is False

    def test_completed_after_dispatch(self):
        now = datetime.now(timezone.utc)
        state = TaskState(
            last_dispatch=now - timedelta(minutes=5),
            last_run=now - timedelta(minutes=2),
        )
        assert _is_in_flight(state) is False

    def test_dispatched_recently(self):
        now = datetime.now(timezone.utc)
        state = TaskState(
            last_dispatch=now - timedelta(minutes=5),
            last_run=now - timedelta(hours=3),  # Old run, before dispatch
        )
        assert _is_in_flight(state) is True

    def test_stale_dispatch(self):
        now = datetime.now(timezone.utc)
        state = TaskState(
            last_dispatch=now - timedelta(hours=3),  # Older than 2h grace
            last_run=now - timedelta(hours=5),
        )
        assert _is_in_flight(state) is False


class TestComputeNextEligible:
    def _make_board(self, tasks, state=None):
        board = Board()
        board.tasks = tasks
        board.state = state or {}
        return board

    def test_duration_never_run(self):
        board = self._make_board({
            "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                schedule=Schedule(every="1h")),
        })
        ts = _compute_next_eligible(board, "t1")
        assert ts == 0.0  # Immediately eligible

    def test_duration_with_last_run(self):
        now = datetime.now(timezone.utc)
        last_run = now - timedelta(minutes=30)
        board = self._make_board(
            {"t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                 schedule=Schedule(every="1h"))},
            {"t1": TaskState(last_run=last_run)},
        )
        ts = _compute_next_eligible(board, "t1")
        # Should be last_run + 1h
        expected = last_run.replace(tzinfo=timezone.utc).timestamp() + 3600
        assert abs(ts - expected) < 2

    def test_dependency_never_run(self):
        board = self._make_board({
            "dep": SchedulerTask(name="dep", project=".", session="s", task="t",
                                 schedule=Schedule(every="1h")),
            "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                schedule=Schedule(after="dep")),
        })
        ts = _compute_next_eligible(board, "t1")
        assert ts is None  # Blocked — dependency never ran

    def test_dependency_completed(self):
        now = datetime.now(timezone.utc)
        dep_run = now - timedelta(minutes=10)
        board = self._make_board(
            {
                "dep": SchedulerTask(name="dep", project=".", session="s", task="t",
                                     schedule=Schedule(every="1h")),
                "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                    schedule=Schedule(after="dep", delay=1800)),
            },
            {"dep": TaskState(last_run=dep_run, last_status="complete")},
        )
        ts = _compute_next_eligible(board, "t1")
        # Should be dep_run + delay
        expected = dep_run.replace(tzinfo=timezone.utc).timestamp() + 1800
        assert abs(ts - expected) < 2

    def test_dependency_wrong_status(self):
        now = datetime.now(timezone.utc)
        board = self._make_board(
            {
                "dep": SchedulerTask(name="dep", project=".", session="s", task="t",
                                     schedule=Schedule(every="1h")),
                "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                    schedule=Schedule(after="dep")),
            },
            {"dep": TaskState(last_run=now - timedelta(minutes=10), last_status="failed")},
        )
        ts = _compute_next_eligible(board, "t1")
        assert ts is None  # Blocked — dep failed

    def test_cooldown(self):
        now = datetime.now(timezone.utc)
        last_run = now - timedelta(minutes=30)
        board = self._make_board(
            {"t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                 schedule=Schedule(every="15m", cooldown=7200))},
            {"t1": TaskState(last_run=last_run)},
        )
        ts = _compute_next_eligible(board, "t1")
        # Cooldown (2h) is longer than interval (15m), so cooldown dominates
        expected = last_run.replace(tzinfo=timezone.utc).timestamp() + 7200
        assert abs(ts - expected) < 2


class TestValidateBoard:
    def _make_board(self, tasks):
        board = Board()
        board.tasks = tasks
        return board

    def test_valid_board(self):
        board = self._make_board({
            "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                schedule=Schedule(every="1h")),
        })
        assert validate_board(board) == []

    def test_missing_every_and_after(self):
        board = self._make_board({
            "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                schedule=Schedule()),
        })
        errors = validate_board(board)
        assert any("every" in e and "after" in e for e in errors)

    def test_missing_dependency(self):
        board = self._make_board({
            "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                schedule=Schedule(after="nonexistent")),
        })
        errors = validate_board(board)
        assert any("nonexistent" in e for e in errors)

    def test_circular_dependency(self):
        board = self._make_board({
            "a": SchedulerTask(name="a", project=".", session="s", task="t",
                               schedule=Schedule(after="b")),
            "b": SchedulerTask(name="b", project=".", session="s", task="t",
                               schedule=Schedule(after="a")),
        })
        errors = validate_board(board)
        assert any("circular" in e for e in errors)

    def test_disabled_dependency_warning(self):
        board = self._make_board({
            "dep": SchedulerTask(name="dep", project=".", session="s", task="t",
                                 schedule=Schedule(every="1h"), enabled=False),
            "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                schedule=Schedule(after="dep")),
        })
        errors = validate_board(board)
        assert any("disabled" in e for e in errors)

    def test_invalid_every(self):
        board = self._make_board({
            "t1": SchedulerTask(name="t1", project=".", session="s", task="t",
                                schedule=Schedule(every="invalid_value")),
        })
        errors = validate_board(board)
        assert any("invalid 'every'" in e for e in errors)


class TestFormatSchedule:
    def test_simple_duration(self):
        assert format_schedule(Schedule(every="2h")) == "every 2h"

    def test_daily_at(self):
        assert format_schedule(Schedule(every="day", at="08:00")) == "every day at 08:00"

    def test_dependency(self):
        result = format_schedule(Schedule(after="other-task", delay=1800))
        assert "after other-task" in result
        assert "+30m" in result

    def test_cooldown(self):
        result = format_schedule(Schedule(every="1h", cooldown=7200))
        assert "cd 2h" in result

    def test_except_days(self):
        result = format_schedule(Schedule(every="4h", except_days=["saturday", "sunday"]))
        assert "saturday" in result


class TestBoardDisplay:
    def test_display_uses_schedule_str(self, board_env):
        board = load_board()
        rows = get_board_display(board)
        for row in rows:
            assert "schedule_str" in row
            assert "interval_str" not in row
