"""Health-aware managed-shim self-heal (#734).

A dead/wedged Kokoro (:8102) or Moonshine STT (:8101) shim inside a live tmux
session used to be treated as healthy forever (idempotency keyed on session
existence alone), so say/transcribe silently fell back to browser voice. These
tests pin the liveness predicate, the reuse-vs-reap decision in ``start``, and
the doctor surfacing — without touching any real tmux session or port.
"""

from types import SimpleNamespace

import pytest

from agentwire import tts_cli

# === _shim_session_state: the shared liveness predicate ====================


def _patch_probe(monkeypatch, *, responded, status):
    monkeypatch.setattr(
        tts_cli, "_probe_shim_health", lambda port, timeout=2.0: (responded, status)
    )


def test_state_ok_is_live(monkeypatch):
    _patch_probe(monkeypatch, responded=True, status="ok")
    assert tts_cli._shim_session_state("s", 8102) == (True, "ok")


@pytest.mark.parametrize("warming", ["loading", "downloading", "absent"])
def test_state_warming_is_live(monkeypatch, warming):
    # A shim mid-warmup still answers /health — reuse it, never reap it.
    _patch_probe(monkeypatch, responded=True, status=warming)
    live, status = tts_cli._shim_session_state("s", 8102)
    assert live is True
    assert status == warming


def test_state_failed_is_not_live(monkeypatch):
    # Terminal engine failure: the process answers but will never be ready.
    _patch_probe(monkeypatch, responded=True, status="failed")
    assert tts_cli._shim_session_state("s", 8102) == (False, "failed")


def test_state_no_response_young_is_live(monkeypatch):
    # Just launched: the port may not be bound yet — give it the grace window.
    _patch_probe(monkeypatch, responded=False, status=None)
    monkeypatch.setattr(tts_cli, "_tmux_session_age", lambda s: 3.0)
    assert tts_cli._shim_session_state("s", 8102, warmup_grace=25.0) == (True, "starting")


def test_state_no_response_old_is_dead(monkeypatch):
    # Session has existed for hours but nothing answers — dead/wedged.
    _patch_probe(monkeypatch, responded=False, status=None)
    monkeypatch.setattr(tts_cli, "_tmux_session_age", lambda s: 9000.0)
    assert tts_cli._shim_session_state("s", 8102, warmup_grace=25.0) == (False, None)


def test_state_no_response_unknown_age_is_dead(monkeypatch):
    # Age unknowable (session vanished mid-probe) → not live.
    _patch_probe(monkeypatch, responded=False, status=None)
    monkeypatch.setattr(tts_cli, "_tmux_session_age", lambda s: None)
    assert tts_cli._shim_session_state("s", 8102) == (False, None)


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


def _stt_args():
    return SimpleNamespace(port=None, host=None, model=None, backend=None)


def test_kokoro_start_reuses_live_session(monkeypatch, capsys):
    h = _StartHarness(monkeypatch)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (True, "ok"))
    rc = tts_cli.cmd_kokoro_start(_kokoro_args())
    assert rc == 0
    assert h.reaped is False and h.launched is False
    assert "already running" in capsys.readouterr().out


def test_kokoro_start_reaps_and_relaunches_dead_session(monkeypatch, capsys):
    h = _StartHarness(monkeypatch)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (False, None))
    rc = tts_cli.cmd_kokoro_start(_kokoro_args())
    assert rc == 0
    assert h.reaped is True and h.launched is True
    out = capsys.readouterr().out
    assert "not serving" in out and "already running" not in out


def test_stt_start_reuses_live_session(monkeypatch, capsys):
    h = _StartHarness(monkeypatch)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (True, "ok"))
    rc = tts_cli.cmd_stt_start(_stt_args())
    assert rc == 0
    assert h.reaped is False and h.launched is False
    assert "already running" in capsys.readouterr().out


def test_stt_start_reaps_and_relaunches_dead_session(monkeypatch, capsys):
    h = _StartHarness(monkeypatch)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (False, "failed"))
    rc = tts_cli.cmd_stt_start(_stt_args())
    assert rc == 0
    assert h.reaped is True and h.launched is True
    out = capsys.readouterr().out
    assert "not serving" in out and "already running" not in out


# === doctor: flag a present-but-dead shim, ignore warming / absent =========


def _patch_doctor(monkeypatch, *, tts="default", stt="default", moonshine=True):
    from agentwire import doctor_cli

    cfg = SimpleNamespace(
        tts=SimpleNamespace(backend=tts),
        stt=SimpleNamespace(backend=stt),
    )
    # _find_dead_managed_shims imports these lazily from their home modules.
    import agentwire.config as cfg_mod
    import agentwire.core as core_mod
    import agentwire.stt as stt_mod

    monkeypatch.setattr(cfg_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(core_mod, "get_kokoro_session_name", lambda: "agentwire-kokoro")
    monkeypatch.setattr(core_mod, "get_stt_session_name", lambda: "agentwire-stt")
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: moonshine)
    return doctor_cli


def test_doctor_flags_dead_kokoro_shim(monkeypatch):
    doctor_cli = _patch_doctor(monkeypatch, stt="none")
    from agentwire import core as core_mod

    monkeypatch.setattr(core_mod, "tmux_session_exists", lambda s: True)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (False, None))

    dead = doctor_cli._find_dead_managed_shims()
    assert len(dead) == 1
    assert dead[0]["label"] == "Kokoro TTS" and dead[0]["port"] == 8102


def test_doctor_ignores_warming_shim(monkeypatch):
    doctor_cli = _patch_doctor(monkeypatch, stt="none")
    from agentwire import core as core_mod

    monkeypatch.setattr(core_mod, "tmux_session_exists", lambda s: True)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (True, "loading"))

    assert doctor_cli._find_dead_managed_shims() == []


def test_doctor_ignores_absent_session(monkeypatch):
    doctor_cli = _patch_doctor(monkeypatch)
    from agentwire import core as core_mod

    # No tmux session → nothing to self-heal, not a dead shim.
    monkeypatch.setattr(core_mod, "tmux_session_exists", lambda s: False)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (False, None))

    assert doctor_cli._find_dead_managed_shims() == []


def test_doctor_skips_stt_when_moonshine_absent(monkeypatch):
    # STT default tier without Moonshine transcribes in-browser: no shim to flag.
    doctor_cli = _patch_doctor(monkeypatch, tts="custom", moonshine=False)
    from agentwire import core as core_mod

    monkeypatch.setattr(core_mod, "tmux_session_exists", lambda s: True)
    monkeypatch.setattr(tts_cli, "_shim_session_state", lambda s, p: (False, None))

    assert doctor_cli._find_dead_managed_shims() == []
