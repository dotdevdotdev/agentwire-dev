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
        with (
            patch("agentwire.session_ready.send_verified", return_value=True) as sv,
            patch("agentwire.pane_manager.send_to_target") as blind,
        ):
            assert send_task_prompt("s", "do the thing") is True
        sv.assert_called_once()
        blind.assert_not_called()


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

        with (
            patch.object(ensure_cli, "send_task_prompt", side_effect=sent) as send,
            patch("agentwire.ensure_cli.tmux_session_exists", return_value=True),
            patch("agentwire.session_ready.wait_for_session_ready", return_value=True),
            patch("agentwire.completion.wait_for_completion_signal") as wait,
            patch("agentwire.completion.write_task_context"),
            patch("agentwire.completion.clear_task_context"),
            patch("agentwire.ensure_cli.subprocess.run"),
            patch("agentwire.ensure_cli.time.sleep"),
        ):
            wait.return_value = signal or {"status": "complete", "summary": "ok"}
            rc = ensure_cli._run_ensure_task(
                args, "s", task, ctx, "/bin/sh", tmp_path, json_mode=False
            )
        return rc, send, wait

    def test_unconfirmed_send_never_reaches_the_completion_wait(self, tmp_path, capsys):
        rc, send, wait = self._run(tmp_path, sent=[False])
        wait.assert_not_called()
        assert send.call_count == 1
        assert rc != 0
        out = capsys.readouterr().out
        assert "never landed" in out
