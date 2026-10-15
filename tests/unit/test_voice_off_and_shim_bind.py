"""#1083 (backend 'none' really turns voice off) and #1084 (shims bind loopback)."""

import argparse
import asyncio
import runpy
import sys
import types

import pytest

from agentwire import channels_cli, doctor_cli, server, tts_cli
from agentwire.config import Config, STTConfig, TTSConfig, voice_enabled
from agentwire.security import (
    parse_lsof_listeners,
    remote_tts_loopback_note,
    resolve_shim_host,
    shim_bind_findings,
    warn_if_exposed,
)
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


def test_doctor_findings_from_config_and_live_listeners():
    assert shim_bind_findings({}) == []
    both = shim_bind_findings({"tts": {"host": "0.0.0.0"}, "stt": {"host": "0.0.0.0"}})
    assert len(both) == 2
    # a flag/env bind that config never mentions is caught live
    live = shim_bind_findings({}, [(8100, "*"), (8101, "127.0.0.1"), (8102, "::1")])
    assert len(live) == 1 and ":8100" in live[0]


def test_lsof_parser_reads_wildcard_loopback_and_ipv6():
    out = "p1\nn127.0.0.1:8100\nn*:8101\nn[::1]:8102\nn[::]:8100\n"
    assert parse_lsof_listeners(out) == [
        (8100, "127.0.0.1"), (8101, "*"), (8102, "::1"), (8100, "::")]


def test_remote_tts_note_is_info_and_gated():
    custom = {"tts": {"backend": "custom"}}
    assert remote_tts_loopback_note(custom, tts_remote=True)
    assert remote_tts_loopback_note(custom, tts_remote=False) is None
    assert remote_tts_loopback_note({"tts": {"backend": "none"}}, tts_remote=True) is None
    assert remote_tts_loopback_note({}, tts_remote=True) is None
    # info line, never counted as an issue
    lines, issues = doctor_cli._voice_bind_report(custom, tts_remote=True, listening=[])
    assert issues == 0 and lines and lines[0].startswith("  [..]")
    lines, issues = doctor_cli._voice_bind_report(
        {"tts": {"backend": "none"}}, tts_remote=True, listening=[])
    assert (lines, issues) == ([], 0)


def test_doctor_tts_tier_line_for_none_and_default():
    assert "disabled" in doctor_cli._tts_tier_line("none")
    assert "default tier" in doctor_cli._tts_tier_line("default")
    assert doctor_cli._tts_tier_line("custom") is None


def test_api_local_tts_with_none_is_a_clean_disabled_response():
    srv = _bare_server("none")

    class Req:
        match_info = {"name": "s"}

        async def json(self):
            return {"text": "hello"}

    async def boom(*a, **k):
        raise AssertionError("TTS generation attempted while disabled")

    srv._tts_generate = boom
    resp = asyncio.run(srv.api_local_tts(Req()))
    assert resp.status == 200 and b"disabled" in resp.body


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


def test_start_paths_warn_visibly_once_when_exposed(monkeypatch, capsys):
    """Start launches serve inside a detached tmux pane nobody reads, so the
    start command itself must print the warning — exactly once."""
    monkeypatch.setattr(tts_cli, "load_config", lambda: {})
    monkeypatch.setattr(tts_cli, "tmux_session_exists", lambda *_: False)
    monkeypatch.setattr(tts_cli, "_resolve_shim_python", lambda: ("py", "/tmp", None))
    monkeypatch.setattr(tts_cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    ns = argparse.Namespace(host="0.0.0.0", port=None, model=None, backend=None)
    bad = {}
    for name, fn in (("stt", tts_cli.cmd_stt_start), ("kokoro", tts_cli.cmd_kokoro_start)):
        capsys.readouterr()
        fn(ns)
        n = capsys.readouterr().err.count("no authentication")
        if n != 1:
            bad[name] = n
    assert not bad, bad


def test_tts_start_local_warns_visibly_once_when_exposed(monkeypatch, capsys, tmp_path):
    (tmp_path / ".venv-chatterbox" / "bin").mkdir(parents=True)
    (tmp_path / ".venv-chatterbox" / "bin" / "activate").write_text("")
    monkeypatch.setattr(tts_cli, "load_config", lambda: {})
    monkeypatch.setattr(tts_cli, "tmux_session_exists", lambda *_: False)
    monkeypatch.setattr(tts_cli, "get_source_dir", lambda: tmp_path)
    monkeypatch.setattr(tts_cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    ns = argparse.Namespace(host="0.0.0.0", port=None, backend=None, venv=None)
    tts_cli._start_tts_local(ns, attach=False)
    assert capsys.readouterr().err.count("no authentication") == 1
