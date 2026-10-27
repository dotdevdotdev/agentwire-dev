"""Zombie scheduler session detection + reap (#739).

`_dispatch_worktree_task` names every worktree branch `scheduler-<task>-<ts>`.
If the launch crashes before the agent starts, the tmux session drops to a
bare shell that the idle-reaper never touches. `agentwire.scheduler.zombie`
finds and kills those.
"""

from unittest.mock import MagicMock

from agentwire.scheduler import zombie


def _fake_run(sessions_line, panes_by_session):
    """Build a subprocess.run stand-in serving list-sessions / list-panes."""

    def _run(args, **kwargs):
        if "list-sessions" in args:
            return MagicMock(returncode=0, stdout=sessions_line)
        if "list-panes" in args:
            target = next(a for a in args if a.startswith("=")).lstrip("=")
            panes = panes_by_session.get(target)
            if panes is None:
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="\n".join(panes) + "\n")
        raise AssertionError(f"unexpected tmux invocation: {args}")

    return _run


class TestScan:




    def test_running_agent_is_not_a_zombie(self, monkeypatch):
        old = 1000.0
        monkeypatch.setattr(zombie.time, "time", lambda: old + 120)
        session = "proj/scheduler-mytask-20260101-090000"
        monkeypatch.setattr(
            zombie.subprocess, "run",
            _fake_run(f"{session}\t{old}\n", {session: ["claude"]}),
        )
        assert zombie.scan() == []





class TestReap:
    def test_kills_logs_and_notifies_each_zombie(self, monkeypatch):
        zombies = [
            {"session": "proj/scheduler-a-1", "branch": "scheduler-a-1",
             "command": "zsh", "age_seconds": 90},
            {"session": "proj/scheduler-b-2", "branch": "scheduler-b-2",
             "command": "bash", "age_seconds": 200},
        ]
        monkeypatch.setattr(zombie, "scan", lambda: zombies)

        killed = []
        logged = []
        notified = []
        import agentwire.scheduler as sched_pkg
        monkeypatch.setattr(sched_pkg, "_kill_session", lambda s: killed.append(s))
        monkeypatch.setattr(sched_pkg, "_log_event",
                            lambda event, **f: logged.append((event, f)))
        monkeypatch.setattr(zombie, "_pane_tail", lambda session, lines=3: "")
        monkeypatch.setattr(zombie, "_notify",
                            lambda session, branch, command, tail="": notified.append(session))

        result = zombie.reap()

        assert killed == ["proj/scheduler-a-1", "proj/scheduler-b-2"]
        assert notified == ["proj/scheduler-a-1", "proj/scheduler-b-2"]
        assert [e for e, _ in logged] == ["zombie_session_reaped", "zombie_session_reaped"]
        assert result == {"killed": ["proj/scheduler-a-1", "proj/scheduler-b-2"]}







