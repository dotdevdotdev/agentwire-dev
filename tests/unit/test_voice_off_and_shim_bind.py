"""#1083 (backend 'none' really turns voice off) and #1084 (shims bind loopback)."""

import argparse
import asyncio
import runpy
import sys
import types

import pytest

from agentwire import channels_cli, server, tts_cli
from agentwire.config import Config, STTConfig, TTSConfig, voice_enabled
from agentwire.security import resolve_shim_host, shim_bind_findings, warn_if_exposed
from agentwire.voice_status import resolve_tts_status


def test_none_backend_accepted_and_disabled():
    assert TTSConfig(backend="none").backend == "none"
    assert STTConfig(backend="none").backend == "none"
    assert not voice_enabled("none")
    assert voice_enabled("default") and voice_enabled("custom")


def test_say_with_tts_none_plays_nothing(monkeypatch):
    played = []
    monkeypatch.setattr(channels_cli, "load_config", lambda: {"tts": {"backend": "none"}})
    for name in ("_local_say_os", "_local_say_kokoro", "_local_say", "_remote_say"):
        monkeypatch.setattr(channels_cli, name, lambda *a, _n=name, **k: played.append(_n) or 0)
    ns = argparse.Namespace(text=["hello"], json=False, voice=None, exaggeration=None, cfg=None)
    assert channels_cli.cmd_say(ns) == 0
    assert played == []


def test_voice_status_reports_disabled_for_none():
    cfg = Config()
    cfg.tts = TTSConfig(backend="none")
    st = resolve_tts_status(cfg, probe=False)
    assert st.tier == "none" and "disabled" in st.detail.lower()


def _bare_server(backend):
    srv = object.__new__(server.AgentWireServer)
    srv.config = Config()
    srv.config.tts = TTSConfig(backend=backend)
    srv.cmds = []

    async def fake_cmd(cmd, **kw):
        srv.cmds.append(cmd)
        return True, {}

    srv.run_agentwire_cmd = fake_cmd
    return srv


def test_speak_with_tts_none_does_nothing():
    srv = _bare_server("none")

    async def boom(*a, **k):
        raise AssertionError("session touched while TTS is off")

    srv._get_or_create_session = boom
    assert asyncio.run(srv.speak("s", "hi")) is False


def test_switching_to_none_stops_managed_shims_and_default_does_not(monkeypatch):
    monkeypatch.setattr("agentwire.tts.kokoro_importable", lambda: False)
    monkeypatch.setattr("agentwire.stt.moonshine_importable", lambda: False)

    async def run(tts, stt):
        srv = _bare_server(tts)
        srv.config.stt = STTConfig(backend=stt)
        srv._voice_status_cache = None
        server.reconcile_managed_voice(srv, srv.config)
        await asyncio.sleep(0.05)
        for t in asyncio.all_tasks() - {asyncio.current_task()}:
            await t
        return srv.cmds

    off = asyncio.run(run("none", "none"))
    assert ["kokoro", "stop"] in off and ["stt", "stop"] in off
    assert asyncio.run(run("default", "default")) == []


def test_resolve_and_warn(capsys):
    assert resolve_shim_host(None, None) == "127.0.0.1"
    assert resolve_shim_host("10.0.0.5", "127.0.0.1") == "10.0.0.5"
    warn_if_exposed("127.0.0.1", "TTS")
    assert capsys.readouterr().err == ""
    warn_if_exposed("0.0.0.0", "TTS")
    assert "no authentication" in capsys.readouterr().err


def test_doctor_findings_for_exposed_and_remote_loopback():
    assert shim_bind_findings({}) == []
    assert len(shim_bind_findings({"tts": {"host": "0.0.0.0"}, "stt": {"host": "0.0.0.0"}})) == 2
    remote = shim_bind_findings({}, tts_remote=True)
    assert len(remote) == 1 and "loopback" in remote[0]
    assert shim_bind_findings({"tts": {"host": "0.0.0.0"}}, tts_remote=True) == [
        "TTS/Kokoro (tts.host) binds '0.0.0.0': reachable from the network, no authentication"
    ]


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


def test_tts_server_main_binds_loopback_by_default(monkeypatch):
    seen = _capture_bind(monkeypatch)
    monkeypatch.delenv("TTS_HOST", raising=False)
    with pytest.raises(_StopError):
        runpy.run_module("agentwire.tts_server", run_name="__main__")
    assert seen == ["127.0.0.1"]


def test_configured_host_reaches_stt_and_kokoro_serve_and_warns_once(monkeypatch, capsys):
    seen = _capture_bind(monkeypatch)
    monkeypatch.setattr(tts_cli, "load_config",
                        lambda: {"stt": {"host": "10.1.1.1"}, "tts": {"host": "10.2.2.2"}})
    ns = argparse.Namespace(host=None, port=None, model=None, backend=None)
    got = {}
    for name, fn in (("stt", tts_cli.cmd_stt_serve), ("kokoro", tts_cli.cmd_kokoro_serve)):
        seen.clear()
        try:
            fn(ns)
        except _StopError:
            pass
        got[name] = list(seen)
        assert capsys.readouterr().err.count("no authentication") == 1, name
    assert got == {"stt": ["10.1.1.1"], "kokoro": ["10.2.2.2"]}
