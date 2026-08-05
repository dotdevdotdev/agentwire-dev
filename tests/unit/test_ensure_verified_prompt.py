"""The scheduler's dispatch send is verified, and its result is acted on (#889).

`ensure` sent its task prompt through the blind paste path — fixed 1.0s sleep,
Enter, fixed 0.5s sleep, Enter — while every other send in the codebase had
moved to `session_ready.send_verified`, whose docstring says it "Replaces the
old blind fixed-delay-then-Enter". Two consequences, both covered here:

* those delays are constants and the paste they wait for is not, so a large
  payload can outrun them; and
* `send_to_pane` returns `None`, so `ensure` could not tell "delivered" from
  "sitting unsubmitted in the input box" — it just waited for a completion
  signal that could never arrive.

The second half is the substantive one: routing through `send_verified` while
ignoring what it returns would reproduce the same silence with more machinery.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agentwire.ensure_cli import send_task_prompt


class TestSendTaskPromptVerifies:
    def test_uses_the_verified_path_not_the_blind_one(self):
        """The whole point: this must not call `send_to_pane`/`send_to_target`."""
        with patch("agentwire.session_ready.send_verified", return_value=True) as sv, \
             patch("agentwire.pane_manager.send_to_target") as blind:
            assert send_task_prompt("s", "do the thing") is True
        sv.assert_called_once()
        blind.assert_not_called()

    def test_paste_is_marker_tagged_and_the_marker_is_passed_through(self):
        """Per-attempt marker (#839) makes every downstream check a fact."""
        with patch("agentwire.session_ready.send_verified", return_value=True) as sv:
            send_task_prompt("s", "do the thing")
        args, kwargs = sv.call_args
        pasted, marker = args[1], kwargs["marker"]
        assert marker  # a real token was minted
        assert pasted.startswith("do the thing")
        assert pasted.endswith(marker)  # rides INSIDE the pasted text

    def test_each_send_mints_a_fresh_marker(self):
        seen = []
        with patch("agentwire.session_ready.send_verified", return_value=True) as sv:
            send_task_prompt("s", "x")
            send_task_prompt("s", "x")
        seen = [c.kwargs["marker"] for c in sv.call_args_list]
        assert seen[0] != seen[1]

    def test_failure_is_reported_as_failure(self):
        with patch("agentwire.session_ready.send_verified", return_value=False), \
             patch("agentwire.session_ready.scrollback", return_value=""), \
             patch("agentwire.session_ready.message_on_scrollback", return_value=False):
            assert send_task_prompt("s", "do the thing") is False

    def test_ambiguous_confirm_that_actually_landed_is_not_a_failure(self):
        """A False from send_verified can mean the confirm read was ambiguous.

        The marker can only reach scrollback if THIS paste submitted, so its
        presence settles it as a fact rather than a text-similarity guess.
        """
        with patch("agentwire.session_ready.send_verified", return_value=False), \
             patch("agentwire.session_ready.scrollback", return_value="...output..."), \
             patch("agentwire.session_ready.message_on_scrollback", return_value=True):
            assert send_task_prompt("s", "do the thing") is True

    def test_scrollback_is_checked_against_the_marker_not_the_prompt(self):
        """Matching bare prompt text would false-positive on a generic prompt."""
        with patch("agentwire.session_ready.send_verified", return_value=False) as sv, \
             patch("agentwire.session_ready.scrollback", return_value="hay"), \
             patch("agentwire.session_ready.message_on_scrollback",
                   return_value=False) as mos:
            send_task_prompt("s", "continue")
        needle = mos.call_args[0][1]
        assert needle == sv.call_args.kwargs["marker"]
        assert needle != "continue"


def _task(**overrides):
    """A real TaskConfig — parsed, so no field can silently go missing."""
    from agentwire.tasks import parse_task_config

    return parse_task_config("t", {"prompt": "do the thing", **overrides})


class TestFailedSendEndsTheAttempt:
    """A prompt that never landed must not fall through into the wait.

    Waiting on a completion signal for a prompt sitting in the input box is
    exactly the shape of the 2h silence in #867 — so this exercises the real
    `_run_ensure_task` rather than reading its source.
    """

    def _run(self, tmp_path, sent, task=None, signal=None):
        from agentwire import ensure_cli
        from agentwire.templating import TemplateContext

        task = task or _task()
        ctx = TemplateContext(session="s", task="t", project_root=str(tmp_path))
        args = SimpleNamespace(session="s", task="t")
        (tmp_path / ".agentwire").mkdir(exist_ok=True)

        with patch.object(ensure_cli, "send_task_prompt", side_effect=sent) as send, \
             patch("agentwire.ensure_cli.tmux_session_exists", return_value=True), \
             patch("agentwire.session_ready.wait_for_session_ready", return_value=True), \
             patch("agentwire.completion.wait_for_completion_signal") as wait, \
             patch("agentwire.completion.write_task_context"), \
             patch("agentwire.completion.clear_task_context"), \
             patch("agentwire.ensure_cli.subprocess.run"), \
             patch("agentwire.ensure_cli.time.sleep"):
            wait.return_value = signal or {"status": "complete", "summary": "ok"}
            rc = ensure_cli._run_ensure_task(
                args, "s", task, ctx, "/bin/sh", tmp_path, json_mode=False)
        return rc, send, wait

    def test_unconfirmed_send_never_reaches_the_completion_wait(self, tmp_path, capsys):
        rc, send, wait = self._run(tmp_path, sent=[False])
        wait.assert_not_called()
        assert send.call_count == 1
        assert rc != 0
        out = capsys.readouterr().out
        assert "never landed" in out

    def test_a_confirmed_send_proceeds_normally(self, tmp_path):
        rc, send, wait = self._run(tmp_path, sent=[True])
        wait.assert_called_once()
        assert rc == 0

    def test_a_failed_send_is_retried_when_retries_are_configured(self, tmp_path):
        rc, send, wait = self._run(
            tmp_path, sent=[False, True], task=_task(retries=1))
        assert send.call_count == 2
        wait.assert_called_once()   # the retry landed, so the wait runs once
        assert rc == 0

    def test_retries_exhausted_reports_the_named_reason(self, tmp_path, capsys):
        rc, send, wait = self._run(
            tmp_path, sent=[False, False], task=_task(retries=1))
        assert send.call_count == 2
        wait.assert_not_called()
        assert rc != 0
        assert "not confirmed submitted" in capsys.readouterr().out

    def test_on_task_end_failure_does_not_rewrite_a_completed_task(
            self, tmp_path, capsys):
        """The task already reported its status; an unsent epilogue must warn."""
        rc, send, wait = self._run(
            tmp_path,
            sent=[True, False],                 # task prompt lands, epilogue doesn't
            task=_task(on_task_end="now push"),
        )
        assert send.call_count == 2
        assert rc == 0                          # still complete
        assert "not confirmed submitted" in capsys.readouterr().err
