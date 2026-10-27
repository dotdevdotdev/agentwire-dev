"""#1083 (backend 'none' really turns voice off) and #1084 (shims bind loopback)."""

import argparse
import sys
import types

from agentwire import channels_cli, tts_cli


def test_say_with_tts_none_plays_nothing(monkeypatch):
    played = []
    monkeypatch.setattr(channels_cli, "load_config", lambda: {"tts": {"backend": "none"}})
    for name in ("_local_say_os", "_local_say_kokoro", "_local_say", "_remote_say"):
        monkeypatch.setattr(channels_cli, name, lambda *a, _n=name, **k: played.append(_n) or 0)
    ns = argparse.Namespace(text=["hello"], json=False, voice=None, exaggeration=None, cfg=None)
    assert channels_cli.cmd_say(ns) == 0
    assert played == []


class _StopError(Exception):
    pass


def _capture_bind(monkeypatch):
    seen = []

    def fake_run(*a, **kw):
        seen.append(kw.get("host"))
        raise _StopError

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=fake_run))
    for mod in ("agentwire.tts.server", "agentwire.stt.stt_server", "agentwire.tts.kokoro_server"):
        monkeypatch.setitem(sys.modules, mod, types.SimpleNamespace(app=object()))
    return seen


def test_every_serve_path_binds_loopback_by_default(monkeypatch):
    seen = _capture_bind(monkeypatch)
    monkeypatch.setattr(tts_cli, "load_config", lambda: {})
    ns = argparse.Namespace(host=None, port=None, backend=None, venv=None, model=None,
                            engine=None, moonshine_model=None, foreground=False)
    bad = []
    for fn in (tts_cli.cmd_tts_serve, tts_cli.cmd_stt_serve, tts_cli.cmd_kokoro_serve):
        seen.clear()
        try:
            fn(ns)
        except _StopError:
            pass
        except Exception as e:  # noqa: BLE001 - surfaced by the assert
            bad.append((fn.__name__, repr(e)))
            continue
        if seen != ["127.0.0.1"]:
            bad.append((fn.__name__, seen))
    assert not bad, bad
