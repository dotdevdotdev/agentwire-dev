"""#1083 (backend 'none' is valid config) and #1084 (shims bind loopback)."""

import argparse

from agentwire import tts_cli
from agentwire.config import STTConfig, TTSConfig
from agentwire.security import resolve_shim_host


def test_none_backend_accepted_for_tts_and_stt():
    assert TTSConfig(backend="none").backend == "none"
    assert STTConfig(backend="none").backend == "none"


def test_resolve_shim_host_defaults_loopback_and_warns_on_exposure(capsys):
    assert resolve_shim_host(None, None, "TTS") == "127.0.0.1"
    assert resolve_shim_host("127.0.0.1", "0.0.0.0", "TTS") == "127.0.0.1"
    assert capsys.readouterr().err == ""
    assert resolve_shim_host(None, "0.0.0.0", "TTS") == "0.0.0.0"
    assert "no authentication" in capsys.readouterr().err


def test_every_shim_start_path_binds_loopback_by_default(monkeypatch):
    """tts start/serve, stt start/serve, kokoro start/serve: no host given."""
    seen = []
    monkeypatch.setattr(tts_cli, "load_config", lambda: {})
    monkeypatch.setattr(tts_cli, "tmux_session_exists", lambda *_: False)
    monkeypatch.setattr(tts_cli, "get_source_dir", lambda: None)

    class StopError(Exception):
        pass

    def fake_uvicorn(*a, **kw):
        seen.append(kw.get("host"))
        raise StopError

    import sys
    import types

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=fake_uvicorn))
    for mod in ("agentwire.tts.server", "agentwire.stt.stt_server", "agentwire.tts.kokoro_server"):
        monkeypatch.setitem(sys.modules, mod, types.SimpleNamespace(app=object()))

    ns = argparse.Namespace(host=None, port=None, backend=None, venv=None, model=None,
                            engine=None, moonshine_model=None, foreground=False)
    bad = []
    for fn in (tts_cli.cmd_tts_serve, tts_cli.cmd_stt_serve, tts_cli.cmd_kokoro_serve):
        seen.clear()
        try:
            fn(ns)
        except StopError:
            pass
        except Exception as e:  # noqa: BLE001 - surface in the assert below
            bad.append((fn.__name__, repr(e)))
            continue
        if seen != ["127.0.0.1"]:
            bad.append((fn.__name__, seen))
    assert not bad, bad
