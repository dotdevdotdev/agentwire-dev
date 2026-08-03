"""Zombie scheduler session detection + reap (#739).

`_dispatch_worktree_task` names every worktree branch `scheduler-<task>-<ts>`.
If the launch crashes before the agent starts, the tmux session drops to a
bare shell that the idle-reaper never touches. `agentwire.scheduler.zombie`
finds and kills those.
"""

from unittest.mock import MagicMock

from agentwire.scheduler import zombie


class TestIsBareShell:
    def test_recognizes_common_shells(self):
        for cmd in ("zsh", "-zsh", "bash", "-bash", "sh", "fish"):
            assert zombie._is_bare_shell(cmd), cmd

    def test_rejects_agent_and_daemon_commands(self):
        for cmd in ("claude", "node", "2.1.185", "python3.13", "uv"):
            assert not zombie._is_bare_shell(cmd), cmd


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
    def test_flags_bare_shell_scheduler_session(self, monkeypatch):
        old = 1000.0
        monkeypatch.setattr(zombie.time, "time", lambda: old + 120)
        session = "proj/scheduler-mytask-20260101-090000"
        monkeypatch.setattr(
            zombie.subprocess, "run",
            _fake_run(f"{session}\t{old}\n", {session: ["zsh"]}),
        )
        zombies = zombie.scan()
        assert len(zombies) == 1
        assert zombies[0]["session"] == session
        assert zombies[0]["branch"] == "scheduler-mytask-20260101-090000"
        assert zombies[0]["command"] == "zsh"
        assert zombies[0]["age_seconds"] == 120

    def test_ignores_non_scheduler_branches(self, monkeypatch):
        old = 1000.0
        monkeypatch.setattr(zombie.time, "time", lambda: old + 120)
        session = "proj/some-feature-branch"
        monkeypatch.setattr(
            zombie.subprocess, "run",
            _fake_run(f"{session}\t{old}\n", {session: ["zsh"]}),
        )
        assert zombie.scan() == []

    def test_ignores_sessions_without_a_branch(self, monkeypatch):
        old = 1000.0
        monkeypatch.setattr(zombie.time, "time", lambda: old + 120)
        monkeypatch.setattr(
            zombie.subprocess, "run",
            _fake_run(f"proj\t{old}\n", {"proj": ["zsh"]}),
        )
        assert zombie.scan() == []

    def test_ignores_remote_sessions(self, monkeypatch):
        old = 1000.0
        monkeypatch.setattr(zombie.time, "time", lambda: old + 120)
        session = "proj/scheduler-mytask-20260101-090000@gpu"
        monkeypatch.setattr(
            zombie.subprocess, "run",
            _fake_run(f"{session}\t{old}\n", {}),
        )
        assert zombie.scan() == []

    def test_running_agent_is_not_a_zombie(self, monkeypatch):
        old = 1000.0
        monkeypatch.setattr(zombie.time, "time", lambda: old + 120)
        session = "proj/scheduler-mytask-20260101-090000"
        monkeypatch.setattr(
            zombie.subprocess, "run",
            _fake_run(f"{session}\t{old}\n", {session: ["claude"]}),
        )
        assert zombie.scan() == []

    def test_too_young_is_skipped(self, monkeypatch):
        """A dispatch mid-launch briefly shows a bare shell before the agent
        starts — must not be reaped before it's had a chance to run."""
        created = 1000.0
        monkeypatch.setattr(zombie.time, "time", lambda: created + 5)
        session = "proj/scheduler-mytask-20260101-090000"
        monkeypatch.setattr(
            zombie.subprocess, "run",
            _fake_run(f"{session}\t{created}\n", {session: ["zsh"]}),
        )
        assert zombie.scan() == []

    def test_multiple_panes_are_not_flagged(self, monkeypatch):
        """A human may have attached and split panes — don't treat that as a
        zombie shell."""
        old = 1000.0
        monkeypatch.setattr(zombie.time, "time", lambda: old + 120)
        session = "proj/scheduler-mytask-20260101-090000"
        monkeypatch.setattr(
            zombie.subprocess, "run",
            _fake_run(f"{session}\t{old}\n", {session: ["zsh", "zsh"]}),
        )
        assert zombie.scan() == []

    def test_no_tmux_server_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            zombie.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=1, stdout=""))
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

    def test_no_zombies_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(zombie, "scan", lambda: [])
        assert zombie.reap() == {"killed": []}

    def test_tick_is_reap(self, monkeypatch):
        monkeypatch.setattr(zombie, "scan", lambda: [])
        assert zombie.tick() == {"killed": []}

    def test_notify_never_raises_on_email_failure(self, monkeypatch):
        import agentwire.core as core_mod

        def boom(**kwargs):
            raise RuntimeError("resend down")

        import agentwire.channels.email as email_mod
        monkeypatch.setattr(email_mod, "send_email", boom)
        monkeypatch.setattr(core_mod, "_post_desktop_notification", lambda *a, **k: False)
        monkeypatch.setattr(core_mod, "load_session_metadata", lambda s: {})
        zombie._notify("proj/scheduler-a-1", "scheduler-a-1", "zsh")  # must not raise


class TestNotifyRouting:
    """#743: reap alerts must reach the portal + parent, not just email."""

    def _no_op_toast(self, monkeypatch):
        import agentwire.core as core_mod
        posted = []
        monkeypatch.setattr(
            core_mod, "_post_desktop_notification",
            lambda text, **kw: posted.append((text, kw)) or True,
        )
        return posted

    def test_always_posts_a_portal_toast(self, monkeypatch):
        import agentwire.core as core_mod
        posted = self._no_op_toast(monkeypatch)
        monkeypatch.setattr(core_mod, "load_session_metadata", lambda s: {})
        monkeypatch.setattr("agentwire.channels.email.send_email", lambda **k: None)

        zombie._notify("proj/scheduler-a-1", "scheduler-a-1", "zsh")

        assert len(posted) == 1
        assert "proj/scheduler-a-1" in posted[0][0]
        assert posted[0][1].get("session") == "proj/scheduler-a-1"

    def test_routes_to_parent_via_msg_when_created_by_recorded(self, monkeypatch):
        import agentwire.core as core_mod
        import agentwire.inbox as inbox_mod
        self._no_op_toast(monkeypatch)
        monkeypatch.setattr(
            core_mod, "load_session_metadata",
            lambda s: {"created_by": "orchestrator"},
        )
        emailed = []
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: emailed.append(k),
        )
        sent = []
        monkeypatch.setattr(
            inbox_mod, "enqueue",
            lambda to, text, **kw: sent.append((to, text, kw)),
        )

        zombie._notify("proj/scheduler-a-1", "scheduler-a-1", "zsh")

        assert len(sent) == 1
        to, text, kw = sent[0]
        assert to == "orchestrator"
        assert kw.get("kind") == "escalation"
        assert not emailed  # email is the no-parent fallback only

    def test_falls_back_to_email_when_no_parent_recorded(self, monkeypatch):
        import agentwire.core as core_mod
        import agentwire.inbox as inbox_mod
        self._no_op_toast(monkeypatch)
        monkeypatch.setattr(core_mod, "load_session_metadata", lambda s: {})
        sent = []
        monkeypatch.setattr(inbox_mod, "enqueue", lambda *a, **k: sent.append((a, k)))
        emailed = []
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: emailed.append(k),
        )

        zombie._notify("proj/scheduler-a-1", "scheduler-a-1", "zsh")

        assert not sent
        assert len(emailed) == 1
        assert "proj/scheduler-a-1" in emailed[0]["subject"]

    def test_never_raises_when_msg_send_fails(self, monkeypatch):
        import agentwire.core as core_mod
        import agentwire.inbox as inbox_mod
        self._no_op_toast(monkeypatch)
        monkeypatch.setattr(
            core_mod, "load_session_metadata",
            lambda s: {"created_by": "orchestrator"},
        )

        def boom(*a, **k):
            raise RuntimeError("inbox write failed")

        monkeypatch.setattr(inbox_mod, "enqueue", boom)
        zombie._notify("proj/scheduler-a-1", "scheduler-a-1", "zsh")  # must not raise

    def test_never_raises_when_toast_fails(self, monkeypatch):
        import agentwire.core as core_mod

        def boom(*a, **k):
            raise RuntimeError("portal unreachable")

        monkeypatch.setattr(core_mod, "_post_desktop_notification", boom)
        monkeypatch.setattr(core_mod, "load_session_metadata", lambda s: {})
        monkeypatch.setattr("agentwire.channels.email.send_email", lambda **k: None)
        zombie._notify("proj/scheduler-a-1", "scheduler-a-1", "zsh")  # must not raise


class TestPaneTail:
    """#856: the kill destroys the only record of why the launch failed, so
    the pane must be read BEFORE it, and the tail carried into the alert."""

    def test_returns_last_non_empty_lines(self, monkeypatch):
        capture = "cd /tmp/wt && claude \\\n\n  --append-system-prompt \"$(</var/f\n\n"
        monkeypatch.setattr(
            zombie.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=0, stdout=capture),
        )
        tail = zombie._pane_tail("proj/scheduler-a-1", lines=2)
        assert "--append-system-prompt" in tail
        assert " / " in tail  # two lines joined
        assert "\n" not in tail

    def test_empty_on_tmux_failure(self, monkeypatch):
        monkeypatch.setattr(
            zombie.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=1, stdout=""),
        )
        assert zombie._pane_tail("proj/scheduler-a-1") == ""

    def test_never_raises(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("tmux gone")

        monkeypatch.setattr(zombie.subprocess, "run", boom)
        assert zombie._pane_tail("proj/scheduler-a-1") == ""

    def test_reap_reads_the_pane_before_killing_it(self, monkeypatch):
        order = []
        monkeypatch.setattr(zombie, "scan", lambda: [
            {"session": "proj/scheduler-a-1", "branch": "scheduler-a-1",
             "command": "zsh", "age_seconds": 90},
        ])
        monkeypatch.setattr(zombie, "_pane_tail",
                            lambda s, lines=3: order.append("read") or "stuck line")

        import agentwire.scheduler as sched_pkg
        monkeypatch.setattr(sched_pkg, "_kill_session",
                            lambda s: order.append("kill"))
        logged = []
        monkeypatch.setattr(sched_pkg, "_log_event",
                            lambda event, **f: logged.append(f))
        notified = []
        monkeypatch.setattr(zombie, "_notify",
                            lambda s, b, c, tail="": notified.append(tail))

        zombie.reap()

        assert order == ["read", "kill"]
        assert notified == ["stuck line"]
        assert logged[0]["pane_tail"] == "stuck line"

    def test_tail_reaches_the_parent_escalation(self, monkeypatch):
        import agentwire.core as core_mod
        import agentwire.inbox as inbox_mod

        monkeypatch.setattr(core_mod, "_post_desktop_notification", lambda *a, **k: None)
        monkeypatch.setattr(core_mod, "load_session_metadata",
                            lambda s: {"created_by": "orchestrator"})
        sent = {}
        monkeypatch.setattr(inbox_mod, "enqueue",
                            lambda target, text, **k: sent.update(text=text))

        zombie._notify("proj/scheduler-a-1", "scheduler-a-1", "zsh",
                       'claude --append-system-prompt "$(</var/f')

        assert "--append-system-prompt" in sent["text"]
