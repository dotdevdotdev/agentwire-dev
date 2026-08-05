"""`core.tmux_session_cwd` against REAL tmux (#871 item 5).

The first version of this helper used `display-message -t "=<session>"`, which
returns an empty string — a session target doesn't resolve a pane-level format
— so it answered None for every session alive and the doctor check built on it
was inert for its own headline scenario. Every test in the file it shipped
with mocked this function, so nothing ever ran it.

Hence: real tmux, on an isolated socket, with a fixture built to discriminate.
Two axes, because each catches a different wrong implementation:

- **two windows, the second active** — catches `list-panes` without `-s`,
  which scopes to the session's ACTIVE WINDOW only.
- **`pane-base-index 1`, the second pane active** — catches a hardcoded pane
  index. `display-message` does not error on an unresolvable target; it
  silently returns the ACTIVE pane, so a wrong index is a plausible wrong
  answer rather than a failure.

A single-window or single-pane fixture cannot see either bug.
"""

import shutil
import subprocess

import pytest

from agentwire.core import tmux_session_cwd

pytestmark = pytest.mark.requires_tmux

SOCKET = "agentwire-test-cwd"


def tmux(*args, check=True):
    return subprocess.run(
        ["tmux", "-L", SOCKET, *args],
        capture_output=True, text=True, check=check, timeout=15,
    )


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A session whose AGENT pane (first window, first pane) is at `first`,
    with a second window and a second pane both elsewhere, both active."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")

    first = tmp_path / "first"
    second_pane = tmp_path / "second-pane"
    second_window = tmp_path / "second-window"
    for d in (first, second_pane, second_window):
        d.mkdir()

    name = "aw-cwd-probe"
    tmux("kill-session", "-t", f"={name}", check=False)
    # `-P -F '#{pane_id}'`, because target types differ per subcommand: `=name`
    # is an exact SESSION match and `split-window` wants a PANE.
    pane = tmux("new-session", "-d", "-s", name, "-c", str(first),
                "-P", "-F", "#{pane_id}").stdout.strip()
    # Panes base-1, so the agent pane is pane 1 and no pane 0 exists at all.
    tmux("set-option", "-g", "pane-base-index", "1")
    tmux("split-window", "-t", pane, "-c", str(second_pane))
    tmux("new-window", "-t", f"{name}:", "-c", str(second_window))

    # The real helper talks to the default socket, so point it at ours.
    real_run = subprocess.run

    def routed(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "tmux":
            cmd = ["tmux", "-L", SOCKET, *cmd[1:]]
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", routed)
    yield type("S", (), {"name": name, "first": first,
                         "second_pane": second_pane,
                         "second_window": second_window})
    monkeypatch.undo()
    tmux("kill-session", "-t", f"={name}", check=False)


class TestAgainstRealTmux:
    def test_returns_the_agent_panes_cwd(self, session):
        """Not the active pane's, and not the active window's."""
        assert tmux_session_cwd(session.name) == str(session.first)

    def test_the_active_pane_is_not_what_answers(self, session):
        """The fixture leaves a later pane active on purpose; if the helper
        ever falls back to 'whichever pane is active', it returns this."""
        assert tmux_session_cwd(session.name) not in (
            str(session.second_pane), str(session.second_window),
        )

    def test_an_unknown_session_is_none_not_a_guess(self, session):
        assert tmux_session_cwd("no-such-session-at-all") is None


class TestTmuxProbeAssumptions:
    """The measurements the implementation rests on, pinned so a future
    'simplification' back to `display-message` fails here first."""

    def test_a_session_target_does_not_resolve_a_pane_format(self, session):
        r = tmux("display-message", "-p", "-t", f"={session.name}",
                 "#{pane_current_path}")
        assert r.stdout.strip() == ""

    def test_display_message_answers_a_bogus_target_instead_of_failing(self, session):
        """rc=0 and a plausible path — which is why a hardcoded index is unsafe
        in either direction."""
        r = tmux("display-message", "-p", "-t", f"={session.name}:9.9",
                 "#{pane_current_path}", check=False)
        assert r.returncode == 0
        assert r.stdout.strip() != ""

    def test_list_panes_fails_loudly_on_a_bogus_target(self, session):
        r = tmux("list-panes", "-s", "-t", "=nope-not-here",
                 "-F", "#{pane_current_path}", check=False)
        assert r.returncode == 1

    def test_without_dash_s_list_panes_sees_only_the_active_window(self, session):
        """The `-s` flag is not decoration."""
        scoped = tmux("list-panes", "-t", f"={session.name}",
                      "-F", "#{pane_current_path}").stdout.splitlines()
        whole = tmux("list-panes", "-s", "-t", f"={session.name}",
                     "-F", "#{pane_current_path}").stdout.splitlines()
        assert scoped[0] != whole[0]
        assert whole[0] == str(session.first)
