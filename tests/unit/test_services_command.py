"""Process ("command") custom services, and doctor's reporting leg (#983).

A custom service used to be one thing: an agentwire agent session. The voice
buddy's bridge is not that — it is a plain long-running process — and until it
had somewhere to live it was hand-launched, which means it survived no reboot
and appeared in no diagnostic.

These pin the generic half of that: what a `command:` entry parses to, that it
is supervised by tmux rather than by `agentwire new`, that its output lands on
no world-readable surface, and that doctor reports it beside the agent
services. Nothing here knows what the process is — the buddy is one caller of a
mechanism that has no idea it exists.
"""

import argparse
import json

import pytest

from agentwire import doctor_cli, services
from agentwire.config import (
    Config,
    CustomServiceConfig,
    HealthcheckConfig,
    ServicesConfig,
    _dict_to_config,
)


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "services-state.json"
    monkeypatch.setattr(services, "STATE_FILE", f)
    return f


class TestCommandServiceParsing:
    def test_command_entry_parses(self):
        cfg = _dict_to_config({"services": {"custom": [{
            "name": "buddy",
            "command": "agentwire buddy serve buddy --port 8788",
            "autostart": False,
        }]}})
        svc = cfg.services.custom[0]
        assert svc.command == "agentwire buddy serve buddy --port 8788"
        assert svc.autostart is False
        assert services.service_kind(svc) == "command"

    def test_an_agent_service_is_unchanged(self):
        cfg = _dict_to_config({"services": {"custom": [{"name": "tracker"}]}})
        svc = cfg.services.custom[0]
        assert svc.command is None
        assert services.service_kind(svc) == "agent"

    def test_empty_command_is_not_a_command_service(self):
        """`command: ""` must not silently become a process service that runs
        the empty string — tmux would open an idle shell and the healthcheck
        would call it healthy forever."""
        cfg = _dict_to_config({"services": {"custom": [
            {"name": "x", "command": ""},
        ]}})
        assert cfg.services.custom[0].command is None
        assert services.service_kind(cfg.services.custom[0]) == "agent"

    def test_agent_only_fields_are_dropped_and_announced(self, capsys):
        """roles/posture/context_policy describe an agent. On a process service
        they describe nothing, and a field that reads as a guard while nothing
        consumes it is worse than no field at all."""
        cfg = _dict_to_config({"services": {"custom": [{
            "name": "buddy",
            "command": "sleep 1",
            "roles": "worker",
            "posture": "bypass",
            "context_policy": "clear",
        }]}})
        svc = cfg.services.custom[0]
        assert svc.roles is None
        assert svc.posture is None
        assert svc.context_policy == "none"
        warned = capsys.readouterr().err
        assert "buddy" in warned
        assert "roles" in warned and "posture" in warned and "context_policy" in warned

    def test_agent_service_keeps_its_roles(self, capsys):
        cfg = _dict_to_config({"services": {"custom": [
            {"name": "tracker", "roles": "worker", "posture": "bypass"},
        ]}})
        svc = cfg.services.custom[0]
        assert svc.roles == "worker" and svc.posture == "bypass"
        assert capsys.readouterr().err == ""


class TestCommandServiceSupervision:
    """tmux is the supervisor, and that is the secret-handling answer."""

    def _svc(self, **kw):
        kw.setdefault("name", "buddy")
        kw.setdefault("command", "agentwire buddy serve buddy --port 8788")
        return CustomServiceConfig(**kw)

    def test_start_runs_the_command_under_tmux_not_agentwire_new(self, monkeypatch):
        calls = []
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        monkeypatch.setattr(services.time, "sleep", lambda s: None)
        monkeypatch.setattr(services.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or _ok())
        svc = self._svc(project="/tmp/proj")
        ok, msg = services.start_service(svc)
        assert (ok, msg) == (True, "started")
        assert calls == [
            ["tmux", "new-session", "-d", "-s", "buddy", "-c", svc.project,
             "sh -c 'while :; do sleep 3600; done'"],
            ["tmux", "set-option", "-w", "-t", "=buddy:", "remain-on-exit", "on"],
            ["tmux", "respawn-pane", "-k", "-c", svc.project, "-t", "=buddy:.0",
             "agentwire buddy serve buddy --port 8788"],
        ]
        # not `agentwire new` — the agent path is a different mechanism
        assert not any("new-session" in c and "agentwire" in c for c in calls)

    def test_start_redirects_nothing_to_a_file(self, monkeypatch):
        """The whole secret-handling argument. tmux captures stdout/stderr into
        the pane's scrollback, which lives in the tmux server's memory behind a
        0700 per-user socket dir; a shell redirect into a log file would put the
        same bytes somewhere with a mode nobody set. If a `>`, a `tee` or a
        `pipe-pane` ever appears here, that argument is void."""
        calls = []
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        monkeypatch.setattr(services.time, "sleep", lambda s: None)
        monkeypatch.setattr(services.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or _ok())
        services.start_service(self._svc())
        # The service's OWN command is the operator's business; agentwire must
        # not add redirection around it, in ANY of the spawn's steps.
        wrapper = " ".join(
            part for call in calls for part in call if part != self._svc().command
        )
        assert ">" not in wrapper
        assert "tee" not in wrapper
        assert "pipe-pane" not in wrapper

    def test_start_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        monkeypatch.setattr(services.subprocess, "run",
                            lambda *a, **k: pytest.fail("must not respawn"))
        assert services.start_service(self._svc()) == (True, "already running")

    def test_a_lost_spawn_race_is_benign(self, monkeypatch):
        """Autostart, watchdog and a manual `services up` can collide. The loser
        must report the winner's LIVE session — and only a live one, or a corpse
        would be reported as the winner."""
        import subprocess as sp
        exists = iter([False, True])
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: next(exists))
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)

        def boom(*a, **k):
            raise sp.CalledProcessError(1, "tmux", stderr=b"duplicate session")
        monkeypatch.setattr(services.subprocess, "run", boom)
        assert services.start_service(self._svc()) == (True, "already running")

    def test_start_failure_reports_tmux_stderr(self, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)

        def boom(*a, **k):
            raise sp.CalledProcessError(1, "tmux", stderr=b"no server running")
        monkeypatch.setattr(services.subprocess, "run", boom)
        ok, msg = services.start_service(self._svc())
        assert ok is False and "no server running" in msg

    def test_stop_kills_the_session_without_sending_exit(self, monkeypatch):
        """`agentwire kill`'s graceful leg types `/exit` at an agent. There is
        no agent in a process service — those two characters would go to the
        process's stdin."""
        calls = []
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        monkeypatch.setattr(services.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or _ok())
        assert services.stop_service(self._svc()) == (True, "stopped")
        assert calls == [["tmux", "kill-session", "-t", "=buddy"]]

    def test_stop_of_an_agent_service_still_goes_through_agentwire_kill(self, monkeypatch):
        calls = []
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        monkeypatch.setattr(services.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or _ok())
        services.stop_service(CustomServiceConfig(name="tracker"))
        assert calls[0][1:] == ["-m", "agentwire", "kill", "-s", "tracker", "--json"]

    def test_status_carries_the_kind(self, state_file, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        assert services.service_status(self._svc())["kind"] == "command"
        assert services.service_status(CustomServiceConfig(name="t"))["kind"] == "agent"


def _ok():
    class R:
        returncode = 0
        stdout = b""
        stderr = b""
    return R()


class TestADyingProcessIsNotASuccessfulStart:
    """`tmux new-session` succeeding says a PANE was created, not that the
    process in it survived.

    The shape that shipped: `services up` printed "started" while the process
    had already exited, doctor immediately printed "[!!] unhealthy — session
    not found" and prescribed the command that had just claimed success, and
    the process's own stderr died with the pane and existed nowhere. Screenless,
    that is a fix-loop behind a misleading all-clear — the exact failure this
    branch exists to remove. And it was specific to the command kind: the agent
    kind runs `agentwire new` in the FOREGROUND, so a failure there is already
    an exit code.

    Two halves, and the second is not decoration: a refusal that cannot say WHY
    still leaves the owner with nothing to act on.
    """

    def _svc(self):
        return CustomServiceConfig(name="buddy", command="agentwire buddy serve nope")

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        monkeypatch.setattr(services.time, "sleep", lambda s: None)

    def test_an_immediate_exit_is_reported_as_a_failed_start(self, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_tail",
                            lambda n: "FATAL: no OPENAI_API_KEY")
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _ok())
        ok, msg = services.start_service(self._svc())
        assert ok is False
        assert "exited immediately" in msg

    def test_the_refusal_carries_the_process_s_own_last_words(self, monkeypatch):
        """The half that makes the refusal actionable. tmux holds the dead
        pane's output in memory; without it the owner gets 'it failed' and no
        way to learn that the buddy name is unregistered."""
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_tail",
                            lambda n: "No voice buddy named 'nope'.")
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _ok())
        _ok_, msg = services.start_service(self._svc())
        assert "No voice buddy named 'nope'." in msg

    def test_a_survivor_still_reports_started(self, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _ok())
        assert services.start_service(self._svc()) == (True, "started")

    def test_the_pane_is_kept_alive_so_the_reason_survives(self, monkeypatch):
        """`remain-on-exit on` is what retains the dead pane's output — in tmux
        memory, behind the 0700 socket dir. No file is created, so the secret
        property of the command kind is unchanged."""
        calls = []
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        monkeypatch.setattr(services.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or _ok())
        services.start_service(self._svc())
        joined = [" ".join(c) for c in calls]
        assert any("remain-on-exit on" in j for j in joined), joined
        # The option must be set BEFORE the real command runs, or a process that
        # dies fast beats it and the reason is lost anyway.
        opt = next(i for i, j in enumerate(joined) if "remain-on-exit" in j)
        real = next(i for i, j in enumerate(joined) if "respawn-pane" in j)
        assert opt < real, joined
        assert "agentwire buddy serve nope" not in joined[opt]

    def test_a_dead_pane_is_cleared_on_respawn_not_called_already_running(
        self, monkeypatch,
    ):
        """The interaction that would wedge the watchdog: healthcheck says
        unhealthy, watchdog calls start, start sees a session and says 'already
        running' — forever. A dead pane is not a running service."""
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        dead = iter([True, False])
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: next(dead))
        monkeypatch.setattr(services, "_tmux_pane_tail", lambda n: "FATAL: boom")
        calls = []
        monkeypatch.setattr(services.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or _ok())
        ok, msg = services.start_service(self._svc())
        assert ok is True
        # and the reason the previous run died is read BEFORE it is destroyed
        assert "FATAL: boom" in msg
        assert any("kill-session" in " ".join(c) for c in calls)

    def test_a_live_pane_is_still_left_alone(self, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        monkeypatch.setattr(services.subprocess, "run",
                            lambda *a, **k: pytest.fail("must not respawn a live service"))
        assert services.start_service(self._svc()) == (True, "already running")


class TestTheHealthcheckMustSeeADeadPane:
    """Taking `remain-on-exit` means `has-session` alone stops being liveness.

    Measured: `tmux has-session` returns 0 for a session whose pane is DEAD. A
    healthcheck left on that predicate would report a crashed service healthy
    forever — trading a false success at start for a permanent one, which is
    strictly worse. And it is not only the command kind: `remain-on-exit` is a
    user tmux setting, so an agent session could always have been in this state.
    """

    def test_a_dead_pane_is_unhealthy(self, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_tail", lambda n: "FATAL: boom")
        healthy, detail = services.run_healthcheck(
            CustomServiceConfig(name="buddy", command="x"))
        assert healthy is False
        assert "exited" in detail and "FATAL: boom" in detail

    def test_an_agent_session_with_a_dead_pane_is_unhealthy_too(self, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_tail", lambda n: "")
        healthy, _detail = services.run_healthcheck(CustomServiceConfig(name="t"))
        assert healthy is False

    def test_a_live_pane_is_healthy(self, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        assert services.run_healthcheck(CustomServiceConfig(name="t")) == (
            True, "session exists")

    def test_status_running_is_false_for_a_dead_pane(self, state_file, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_tail", lambda n: "")
        assert services.service_status(
            CustomServiceConfig(name="buddy", command="x"))["running"] is False


class TestThePlaceholderMustNeverOutliveTheSpawn:
    """A spawn that fails partway must leave nothing behind claiming to be the
    service.

    The three-step spawn introduced a window the one-shot version did not have:
    steps 2 and 3 run against a session step 1 just created, and the old
    "lost a benign spawn race" handler treated ANY failure in the try block as
    "someone else's session is already up". It isn't — it is the placeholder,
    genuinely alive, running `sleep 3600`. `pane_dead` cannot see that (the
    sleep loop is not a corpse), so the healthcheck calls it healthy: a sleep
    loop reported as a running service, which is F1's false all-clear wearing a
    different costume.

    So `already running` must be reachable ONLY when the session genuinely
    pre-existed this call.
    """

    def _svc(self):
        return CustomServiceConfig(name="buddy", command="real-command --port 1")

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        monkeypatch.setattr(services.time, "sleep", lambda s: None)

    def _failing_at(self, monkeypatch, failing: str, calls: list):
        """Let every tmux call succeed except the one whose argv contains
        *failing*."""
        import subprocess as sp

        def run(cmd, **kw):
            calls.append(cmd)
            if failing in cmd:
                raise sp.CalledProcessError(1, "tmux", stderr=b"tmux said no")
            return _ok()
        monkeypatch.setattr(services.subprocess, "run", run)

    @pytest.mark.parametrize("failing", ["set-option", "respawn-pane"])
    def test_a_failure_after_the_placeholder_exists_is_a_failed_start(
        self, monkeypatch, failing,
    ):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        calls = []
        self._failing_at(monkeypatch, failing, calls)
        ok, msg = services.start_service(self._svc())
        assert ok is False, msg
        assert "already running" not in msg

    @pytest.mark.parametrize("failing", ["set-option", "respawn-pane"])
    def test_the_placeholder_is_killed_rather_than_left_running(
        self, monkeypatch, failing,
    ):
        """Otherwise the orphan sits there running a sleep loop, the healthcheck
        reports it healthy, and the real service never starts again."""
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        calls = []
        self._failing_at(monkeypatch, failing, calls)
        services.start_service(self._svc())
        assert any("kill-session" in c for c in calls), calls

    def test_a_genuinely_pre_existing_session_is_still_already_running(
        self, monkeypatch,
    ):
        """The case the handler was written for, which must keep working: two
        starts race, new-session loses on a duplicate, and the loser reports
        the winner rather than a failure."""
        import subprocess as sp
        exists = iter([False, True])
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: next(exists))
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        killed = []

        def run(cmd, **kw):
            if "new-session" in cmd:
                raise sp.CalledProcessError(1, "tmux", stderr=b"duplicate session")
            if "kill-session" in cmd:
                killed.append(cmd)
            return _ok()
        monkeypatch.setattr(services.subprocess, "run", run)
        assert services.start_service(self._svc()) == (True, "already running")
        # and it must NOT kill the winner's session on its way out
        assert killed == []


class TestNamesGoThroughTheOneMapping:
    """tmux rewrites `.` and `:` in a session name; `worktree.tmux_safe_name`
    is the single implementation of that mapping (#868/#878).

    Building `-s` from the raw name and then targeting `-t` with the raw name
    is the documented failure: tmux creates `a_b`, every subsequent target
    misses, and teardown reports success while the session survives. That is
    live here because the spawn now has FIVE targets, not one.
    """

    def test_every_tmux_target_uses_the_sanitized_name(self, monkeypatch):
        from agentwire.worktree import tmux_safe_name
        raw = "rev.dot:2"
        safe = tmux_safe_name(raw)
        assert safe == "rev_dot_2"  # the mapping, stated so a change is visible
        calls = []
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        monkeypatch.setattr(services.time, "sleep", lambda s: None)
        monkeypatch.setattr(services.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or _ok())
        services.start_service(CustomServiceConfig(name=raw, command="x"))
        targets = [part for call in calls for part in call if part.startswith("=")]
        assert targets, calls
        for target in targets:
            assert safe in target, target
            assert raw not in target, target
        assert ["-s", safe] == [p for call in calls for i, p in enumerate(call)
                                if p == "-s" or (i and call[i - 1] == "-s")]

    @pytest.mark.parametrize("helper", ["_tmux_session_exists", "_tmux_pane_dead",
                                        "_tmux_pane_tail"])
    def test_the_probes_ask_about_the_name_tmux_actually_chose(
        self, monkeypatch, helper,
    ):
        calls = []
        monkeypatch.setattr(services.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or _ok_text())
        getattr(services, helper)("rev.dot:2")
        flat = " ".join(calls[0])
        assert "rev_dot_2" in flat
        assert "rev.dot:2" not in flat


def _ok_text():
    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    return R()


class TestACrashLineIsRedactedBeforeItIsSpoken:
    """A captured crash line is exactly where a secret shows up, and `detail`
    does not stay on the terminal.

    It reaches the portal watchdog's `_notify_service_event`, which TOASTS it
    and SPEAKS it through `agentwire say`. Owner-facing, so surfacing it at all
    is a deliberate trade — but a process printing `bearer eyJ…` while dying
    would have put that verbatim into a spoken utterance. Redaction happens at
    the single choke point every consumer reads through, and uses the SAME
    pattern set as the argv check rather than a second list that can drift.
    """

    def _tail(self, monkeypatch, text):
        class R:
            returncode = 0
            stdout = text
            stderr = ""
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: R())
        return services._tmux_pane_tail("x")

    @pytest.mark.parametrize("line,secret", [
        ("FATAL: bad creds: bearer eyJLEAKED", "eyJLEAKED"),
        ("usage: bridge --token=SUPERSECRET123", "SUPERSECRET123"),
        ("usage: bridge --token SUPERSECRET123", "SUPERSECRET123"),
        ("usage: bridge --api-key sk-live-abc", "sk-live-abc"),
        ("env PASSWORD=hunter2 not found", "hunter2"),
    ])
    def test_the_secret_is_masked(self, monkeypatch, line, secret):
        tail = self._tail(monkeypatch, line)
        assert secret not in tail, tail
        assert "***" in tail

    def test_the_rest_of_the_line_survives(self, monkeypatch):
        """Redaction that ate the message would re-create the failure it is
        guarding: a refusal that cannot say why."""
        tail = self._tail(monkeypatch, "FATAL: bad creds: bearer eyJLEAKED")
        assert "FATAL: bad creds" in tail

    def test_an_ordinary_crash_line_is_untouched(self, monkeypatch):
        assert self._tail(monkeypatch, "No voice buddy named 'nope'.") == (
            "No voice buddy named 'nope'.")

    def test_a_very_long_line_is_clipped(self, monkeypatch):
        """`_TAIL_LINES` bounds LINES. Three lines of a 5000-column traceback is
        one spoken utterance nobody can listen to."""
        tail = self._tail(monkeypatch, "x" * 5000)
        assert len(tail) <= services._TAIL_CHARS + 1
        assert tail.endswith("…")

    def test_redacting_before_the_clip_keeps_the_actionable_part(self, monkeypatch):
        """Ordering, and the reason is legibility rather than safety.

        Clipping first would be equally safe — a cut only removes trailing
        material, and the key stays in front of whatever value survives, so the
        pattern still matches. But a 400-character token would eat the whole
        character budget and push the words the operator needs off the end.
        Redaction shortens the value, so doing it first spends the cap on the
        message.
        """
        secret = "SUPERSECRET" + "9" * 400
        tail = self._tail(monkeypatch, f"bridge --token={secret} FATAL-boom")
        assert "SUPERSECRET" not in tail, tail
        assert "***" in tail
        assert "FATAL-boom" in tail, tail

    def test_redaction_uses_the_argv_pattern_set(self):
        """One source of truth. A second list would drift from the argv check
        the moment either is extended — which just happened to the argv one."""
        for pattern in services._SECRET_ARGV_PATTERNS:
            assert services.redact_secrets(f"x {pattern}VALUE") .endswith("***"), pattern


@pytest.mark.requires_tmux
class TestAgainstRealTmux:
    """The same claims against the real binary.

    Everything above monkeypatches the tmux helpers, which is what makes it run
    in the hermetic CI gate — and is also exactly the fixture shape that let F1
    ship. `#{pane_dead}`, `remain-on-exit` and capture-pane-after-death are
    tmux behaviours, not ours, and a mock agrees with whatever it was told.
    """

    NAME = "zz983-realtmux-probe"

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        import subprocess as sp
        sp.run(["tmux", "kill-session", "-t", f"={self.NAME}"], capture_output=True)
        yield
        sp.run(["tmux", "kill-session", "-t", f"={self.NAME}"], capture_output=True)

    def _svc(self, command):
        return CustomServiceConfig(name=self.NAME, command=command)

    def test_a_dying_process_fails_the_start_and_says_why(self):
        svc = self._svc('sh -c "echo FATAL: no OPENAI_API_KEY >&2; exit 1"')
        ok, msg = services.start_service(svc)
        assert ok is False, msg
        assert "FATAL: no OPENAI_API_KEY" in msg

    def test_the_healthcheck_agrees_rather_than_reporting_healthy(self):
        svc = self._svc('sh -c "exit 1"')
        services.start_service(svc)
        # has-session alone would say yes here — that is the whole finding.
        assert services._tmux_session_exists(self.NAME) is True
        healthy, detail = services.run_healthcheck(svc)
        assert healthy is False, detail

    def test_a_survivor_starts_and_is_healthy_and_stops(self):
        svc = self._svc("sleep 30")
        ok, msg = services.start_service(svc)
        assert ok is True, msg
        assert services.run_healthcheck(svc)[0] is True
        assert services.start_service(svc) == (True, "already running")
        assert services.stop_service(svc)[0] is True
        assert services._tmux_session_exists(self.NAME) is False

    def test_a_crashed_service_can_be_respawned(self):
        """The watchdog's recovery path, end to end."""
        services.start_service(self._svc('sh -c "echo FATAL: boom >&2; exit 1"'))
        assert services.run_healthcheck(self._svc("x"))[0] is False
        ok, msg = services.start_service(self._svc("sleep 30"))
        assert ok is True, msg
        assert "FATAL: boom" in msg  # the previous run's reason, read before clearing
        assert services.run_healthcheck(self._svc("sleep 30"))[0] is True

    def test_a_crash_line_carrying_a_secret_is_redacted(self):
        """Against the real capture path, since that is where it would leak:
        `detail` is toasted AND spoken by the portal watchdog."""
        svc = self._svc('sh -c "echo bearer eyJLEAKED >&2; exit 1"')
        ok, msg = services.start_service(svc)
        assert ok is False
        assert "eyJLEAKED" not in msg, msg
        assert "***" in msg
        assert "eyJLEAKED" not in services.run_healthcheck(svc)[1]


@pytest.mark.requires_tmux
class TestARewrittenNameAgainstRealTmux:
    """Leg 1 of the placeholder finding, with zero mocks.

    A service name containing `.` or `:` is rewritten by tmux itself, so a
    spawn that creates with the raw name and then targets with the raw name
    fails at step 2 — and used to leave the placeholder running while
    `stop_service` reported "not running". A false all-clear plus an orphan,
    and the service never runs again.
    """

    RAW = "zz983.dot:probe"

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        import subprocess as sp

        from agentwire.worktree import tmux_safe_name
        for name in (self.RAW, tmux_safe_name(self.RAW)):
            sp.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)
        yield
        for name in (self.RAW, tmux_safe_name(self.RAW)):
            sp.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)

    def _svc(self, command):
        return CustomServiceConfig(name=self.RAW, command=command)

    def test_a_rewritten_name_starts_and_is_seen_and_stops(self):
        svc = self._svc("sleep 30")
        ok, msg = services.start_service(svc)
        assert ok is True, msg
        assert services.run_healthcheck(svc) == (True, "session exists")
        ok, msg = services.stop_service(svc)
        assert ok is True, msg
        # The claim `stop_service` makes must be TRUE of the session tmux
        # actually created, not of a name that never existed.
        assert services._tmux_session_exists(self.RAW) is False

    def test_no_orphan_placeholder_is_left_behind(self):
        import subprocess as sp

        from agentwire.worktree import tmux_safe_name
        services.start_service(self._svc("sleep 30"))
        services.stop_service(self._svc("sleep 30"))
        live = sp.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                      capture_output=True, text=True).stdout
        assert tmux_safe_name(self.RAW) not in live.split(), live

    def test_a_partial_spawn_leaves_nothing_running(self, monkeypatch):
        """Leg 2, against real tmux: only step 3 fails. Nothing may survive
        claiming to be the service — least of all the sleep loop, which
        `pane_dead` cannot distinguish from a healthy process."""
        import subprocess as sp
        real_run = sp.run

        def run(cmd, **kw):
            if isinstance(cmd, list) and "respawn-pane" in cmd:
                raise sp.CalledProcessError(1, "tmux", stderr=b"injected")
            return real_run(cmd, **kw)
        monkeypatch.setattr(services.subprocess, "run", run)

        ok, msg = services.start_service(self._svc("sleep 30"))
        assert ok is False, msg
        monkeypatch.undo()
        assert services._tmux_session_exists(self.RAW) is False
        # and nothing reports the corpse-free placeholder as healthy
        assert services.run_healthcheck(self._svc("sleep 30"))[0] is False


class TestInlineSecretsInArgv:
    """The one leak tmux does NOT close: argv is world-readable in `ps`."""

    @pytest.mark.parametrize("command,expected", [
        ("agentwire buddy serve buddy --port 8788", None),
        ("some-bridge --token=hunter2", "--token="),
        ("some-bridge --api-key=sk-live", "--api-key="),
        ("env PASSWORD=hunter2 some-bridge", "password="),
        ("curl -H 'Authorization: Bearer abc' x", "bearer "),
        # Space-joined reaches `ps` identically. Matching only the `=` form
        # would select for whoever writes it the other way.
        ("some-bridge --token hunter2", "--token "),
        ("some-bridge --api-key sk-live", "--api-key "),
        ("some-bridge --password hunter2", "--password "),
    ])
    def test_detection(self, command, expected):
        svc = CustomServiceConfig(name="x", command=command)
        assert services.command_secret_risk(svc) == expected

    def test_an_agent_service_has_no_argv_to_leak(self):
        assert services.command_secret_risk(CustomServiceConfig(name="x")) is None


class TestDoctorSection:
    """`agentwire doctor` reports a process service beside the agent ones."""

    def _patch(self, monkeypatch, custom, *, healthy=True, disabled=()):
        cfg = Config(services=ServicesConfig(custom=custom))
        monkeypatch.setattr("agentwire.config.load_config", lambda *a, **k: cfg)
        monkeypatch.setattr(services, "notifications_session_name", lambda: "notif")
        monkeypatch.setattr(services, "_source_dir", lambda: "/tmp/src")
        monkeypatch.setattr(services, "load_disabled", lambda: set(disabled))
        monkeypatch.setattr(services, "run_healthcheck",
                            lambda svc: (healthy, "session exists" if healthy
                                         else "session not found"))

    def test_a_healthy_command_service_is_reported_with_its_kind(self, monkeypatch, capsys):
        self._patch(monkeypatch, [CustomServiceConfig(
            name="buddy", command="agentwire buddy serve buddy --port 8788")])
        assert doctor_cli._render_custom_services_section() == 0
        out = capsys.readouterr().out
        assert "[ok] Service buddy (command): session exists" in out
        assert "[ok] Service notif (agent)" in out

    def test_a_dead_command_service_is_an_issue_with_a_fix(self, monkeypatch, capsys):
        self._patch(monkeypatch, [CustomServiceConfig(
            name="buddy", command="agentwire buddy serve buddy")], healthy=False)
        found = doctor_cli._render_custom_services_section()
        out = capsys.readouterr().out
        assert "[!!] Service buddy (command): unhealthy" in out
        assert "Run: agentwire services up buddy" in out
        assert found == 2  # the buddy and the built-in notifications bridge

    def test_a_downed_service_is_not_scored(self, monkeypatch, capsys):
        self._patch(monkeypatch, [CustomServiceConfig(name="buddy", command="x")],
                    healthy=False, disabled={"buddy", "notif"})
        assert doctor_cli._render_custom_services_section() == 0
        assert "stopped via 'services down'" in capsys.readouterr().out

    def test_autostart_off_is_not_scored(self, monkeypatch, capsys):
        """The buddy's own entry ships `autostart: false` until the owner opts
        in, and doctor must not nag about a service nobody asked to run."""
        self._patch(monkeypatch, [CustomServiceConfig(
            name="buddy", command="x", autostart=False)], healthy=False)
        found = doctor_cli._render_custom_services_section()
        out = capsys.readouterr().out
        assert "[..] Service buddy (command): not running (autostart off" in out
        assert found == 1  # notif only

    def test_an_inline_secret_is_flagged(self, monkeypatch, capsys):
        self._patch(monkeypatch, [CustomServiceConfig(
            name="bridge", command="some-bridge --token=hunter2")])
        found = doctor_cli._render_custom_services_section()
        out = capsys.readouterr().out
        assert "world-readable in the process table" in out
        assert "~/.agentwire/.env" in out
        assert found == 1

    def test_a_broken_healthcheck_does_not_hide_the_other_services(
        self, monkeypatch, capsys,
    ):
        """One bad entry must not abandon the rest of the report — the #905
        shape, one subsystem over."""
        self._patch(monkeypatch, [CustomServiceConfig(name="buddy", command="x")])

        def selective(svc):
            if svc.name == "notif":
                raise RuntimeError("tmux exploded")
            return True, "session exists"
        monkeypatch.setattr(services, "run_healthcheck", selective)
        doctor_cli._render_custom_services_section()
        out = capsys.readouterr().out
        assert "healthcheck error — tmux exploded" in out
        assert "[ok] Service buddy (command)" in out

    def test_unloadable_config_degrades_to_a_note(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise RuntimeError("bad yaml")
        monkeypatch.setattr("agentwire.config.load_config", boom)
        assert doctor_cli._render_custom_services_section() == 0
        assert "Could not check custom services: bad yaml" in capsys.readouterr().out


class TestServicesCLIExposesTheKind:
    @pytest.fixture
    def cli(self, state_file, monkeypatch):
        from agentwire import system_cli as main_mod
        monkeypatch.setattr(services, "notifications_session_name", lambda: "notif")
        monkeypatch.setattr(services, "_source_dir", lambda: "/tmp/src")
        cfg = Config(services=ServicesConfig(custom=[CustomServiceConfig(
            name="buddy", command="agentwire buddy serve buddy --port 8788",
            autostart=False, healthcheck=HealthcheckConfig(interval=30),
        )]))
        monkeypatch.setattr("agentwire.config.load_config", lambda *a, **k: cfg)
        return main_mod

    def _args(self, **kw):
        defaults = {"json": True, "name": None, "all": False}
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_list_json_carries_kind_and_command(self, cli, capsys):
        assert cli.cmd_services_list(self._args()) == 0
        data = json.loads(capsys.readouterr().out)
        by_name = {s["name"]: s for s in data["services"]}
        assert by_name["buddy"]["kind"] == "command"
        assert by_name["buddy"]["command"] == "agentwire buddy serve buddy --port 8788"
        assert by_name["notif"]["kind"] == "agent"
        assert by_name["notif"]["command"] is None

    def test_down_passes_the_service_not_a_bare_name(self, cli, monkeypatch, capsys):
        """The kill path branches on `command`, so it needs the entry. Passing a
        name would send `/exit` to a process."""
        seen = []
        monkeypatch.setattr(services, "stop_service",
                            lambda svc: (seen.append(svc) or True, "stopped"))
        assert cli.cmd_services_down(self._args(name="buddy")) == 0
        assert seen[0].command == "agentwire buddy serve buddy --port 8788"
