"""Integration tests for scheduler board load/save round-trip and scheduling logic."""

import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agentwire.scheduler import (
    Board,
    Schedule,
    SchedulerTask,
    TaskState,
    _atomic_write,
    _load_state_file,
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
        state_file = tmp_path / "scheduler-state.yaml"
        state_backups = 5
        events_file = tmp_path / "events.jsonl"
        live_state_file = tmp_path / "live.json"
        git_timeout = 10
        git_op_timeout = 15
        gate_timeout = 10
        portal_notify_timeout = 5
        session_create_timeout = 30
        max_loop_sleep = 60
        dispatch_cooldown = 60
        error_backoff_base = 30
        error_backoff_max = 1800

    with patch("agentwire.scheduler._sched_config", return_value=FakeSchedulerConfig()):
        yield board_path


class TestSchedulerBoardRoundTrip:





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








class TestValidateBoard:
    def _make_board(self, tasks):
        board = Board()
        board.tasks = tasks
        return board




    def test_circular_dependency(self):
        board = self._make_board({
            "a": SchedulerTask(name="a", project=".", session="s", task="t",
                               schedule=Schedule(after="b")),
            "b": SchedulerTask(name="b", project=".", session="s", task="t",
                               schedule=Schedule(after="a")),
        })
        errors = validate_board(board)
        assert any("circular" in e for e in errors)










# --- #449: state/config separation, atomic writes, backups, crash-loop guard ---

class TestStateConfigSeparation:
    """The daemon must NEVER rewrite board_file (user task definitions)."""

    def test_save_board_leaves_board_file_byte_for_byte_untouched(self, board_env, tmp_path):
        board = load_board()
        before = board_env.read_bytes()

        # A realistic state mutation (what a dispatch would record).
        board.state["code-quality"] = TaskState(
            last_run=datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc),
            last_status="complete",
            last_duration=42,
            run_count=99,
            last_summary="multi\nline\nsummary with: colons & \"quotes\"",
            last_dispatch=datetime(2026, 6, 22, 8, 59, 0, tzinfo=timezone.utc),
        )
        save_board(board)

        # board_file (tasks) is identical to the byte; state went elsewhere.
        assert board_env.read_bytes() == before
        state_file = tmp_path / "scheduler-state.yaml"
        assert state_file.exists()
        assert "code-quality" in state_file.read_text()
        # And the board_file still has no machine-written summary smeared in.
        assert "multi\nline" not in board_env.read_text()




class TestAtomicWrites:
    def test_no_partial_file_when_validation_fails(self, tmp_path):
        target = tmp_path / "out.yaml"
        target.write_text("original: good\n")

        def always_bad(_tmp):
            raise ValueError("nope")

        with pytest.raises(ValueError):
            _atomic_write(target, "new: content\n", validate=always_bad)

        # Original intact, no .tmp droppings left behind.
        assert target.read_text() == "original: good\n"
        leftovers = [p for p in tmp_path.iterdir() if p.name != "out.yaml"]
        assert leftovers == []




class TestBackupRotation:

    def test_load_heals_from_backup_when_live_state_corrupt(self, board_env, tmp_path):
        board = load_board()
        board.state["code-quality"].run_count = 12
        save_board(board)          # writes live
        board.state["code-quality"].run_count = 13
        save_board(board)          # rotates the run_count=12 copy into bak1

        # Corrupt the live state file with content that won't parse (the #449
        # failure was a mangled multi-line scalar → yaml.scanner.ScannerError).
        state_file = tmp_path / "scheduler-state.yaml"
        state_file.write_text('state:\n  code-quality:\n    last_summary: "unterminated\n')
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(state_file.read_text())

        healed = _load_state_file()
        # Falls back to bak1 (run_count 12), not a crash or empty.
        assert healed["code-quality"]["run_count"] == 12


