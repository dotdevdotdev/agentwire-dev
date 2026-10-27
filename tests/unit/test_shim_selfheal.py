"""Health-aware managed-shim self-heal (#734).

A dead/wedged Kokoro (:8102) or Moonshine STT (:8101) shim inside a live tmux
session used to be treated as healthy forever (idempotency keyed on session
existence alone), so say/transcribe silently fell back to browser voice. These
tests pin the liveness predicate, the reuse-vs-reap decision in ``start``, and
the doctor surfacing — without touching any real tmux session or port.
"""

from types import SimpleNamespace


from agentwire import tts_cli

# === _shim_session_state: the shared liveness predicate ====================


# === start commands: reuse when live, reap+relaunch when dead ==============


class _StartHarness:
    """Records whether start reaped the session / launched a fresh one."""

    def __init__(self, monkeypatch, module=tts_cli):
        self.reaped = False
        self.launched = False
        monkeypatch.setattr(module, "tmux_session_exists", lambda s: True)
        monkeypatch.setattr(module, "_reap_shim_session", self._reap)
        monkeypatch.setattr(
            module, "_resolve_shim_python", lambda: ("/usr/bin/python3", None, None)
        )
        monkeypatch.setattr(module, "load_config", lambda: {"stt": {}})
        monkeypatch.setattr(module.subprocess, "run", self._run)

    def _reap(self, session):
        self.reaped = True

    def _run(self, cmd, *a, **k):
        # `tmux new-session` is the launch of a fresh shim.
        if isinstance(cmd, list) and "new-session" in cmd:
            self.launched = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _kokoro_args():
    return SimpleNamespace(port=None, host=None)


def test_kokoro_start_reaps_and_relaunches_dead_session(monkeypatch, capsys):
    h = _StartHarness(monkeypatch)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (False, None))
    rc = tts_cli.cmd_kokoro_start(_kokoro_args())
    assert rc == 0
    assert h.reaped is True and h.launched is True
    out = capsys.readouterr().out
    assert "not serving" in out and "already running" not in out


# === doctor: flag a present-but-dead shim, ignore warming / absent =========
