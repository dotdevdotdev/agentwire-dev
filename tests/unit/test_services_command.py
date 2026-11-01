"""Process ("command") custom services, and doctor's reporting leg (#983).

A custom service used to be one thing: an agentwire agent session. A bridge
process is not that — it is a plain long-running process — and until it
had somewhere to live it was hand-launched, which means it survived no reboot
and appeared in no diagnostic.

These pin the generic half of that: what a `command:` entry parses to, that it
is supervised by tmux rather than by `agentwire new`, that its output lands on
no world-readable surface, and that doctor reports it beside the agent
services. Nothing here knows what the process is — a bridge is one caller of a
mechanism that has no idea it exists.
"""

import pytest

from agentwire import services
from agentwire.config import (
    CustomServiceConfig,
)


class TestCommandServiceSupervision:
    """tmux is the supervisor, and that is the secret-handling answer."""

    def _svc(self, **kw):
        kw.setdefault("name", "bridge")
        kw.setdefault("command", "my-bridge serve --port 8788")
        return CustomServiceConfig(**kw)

    def test_start_runs_the_command_under_tmux_not_agentwire_new(self, monkeypatch):
        calls = []
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: False)
        monkeypatch.setattr(services.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            services.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _ok()
        )
        svc = self._svc(project="/tmp/proj")
        ok, msg = services.start_service(svc)
        assert (ok, msg) == (True, "started")
        assert calls == [
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "bridge",
                "-c",
                svc.project,
                "sh -c 'while :; do sleep 3600; done'",
            ],
            ["tmux", "set-option", "-w", "-t", "=bridge:", "remain-on-exit", "on"],
            [
                "tmux",
                "respawn-pane",
                "-k",
                "-c",
                svc.project,
                "-t",
                "=bridge:.0",
                "my-bridge serve --port 8788",
            ],
        ]
        # not `agentwire new` — the agent path is a different mechanism
        assert not any("new-session" in c and "agentwire" in c for c in calls)


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
        return CustomServiceConfig(name="bridge", command="my-bridge serve nope")

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        monkeypatch.setattr(services.time, "sleep", lambda s: None)

    def test_an_immediate_exit_is_reported_as_a_failed_start(self, monkeypatch):
        monkeypatch.setattr(services, "_tmux_session_exists", lambda n: False)
        monkeypatch.setattr(services, "_tmux_pane_dead", lambda n: True)
        monkeypatch.setattr(services, "_tmux_pane_tail", lambda n: "FATAL: no OPENAI_API_KEY")
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _ok())
        ok, msg = services.start_service(self._svc())
        assert ok is False
        assert "exited immediately" in msg


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
        healthy, detail = services.run_healthcheck(CustomServiceConfig(name="bridge", command="x"))
        assert healthy is False
        assert "exited" in detail and "FATAL: boom" in detail


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

    @pytest.mark.parametrize(
        "line,secret",
        [
            ("FATAL: bad creds: bearer eyJLEAKED", "eyJLEAKED"),
            ("usage: bridge --token=SUPERSECRET123", "SUPERSECRET123"),
            ("usage: bridge --token SUPERSECRET123", "SUPERSECRET123"),
            ("usage: bridge --api-key sk-live-abc", "sk-live-abc"),
            ("env PASSWORD=hunter2 not found", "hunter2"),
        ],
    )
    def test_the_secret_is_masked(self, monkeypatch, line, secret):
        tail = self._tail(monkeypatch, line)
        assert secret not in tail, tail
        assert "***" in tail
