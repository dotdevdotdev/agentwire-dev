"""Voice-loop preflight stage checks (agentwire/doctor_voice.py).

Each stage is exercised in isolation, proving the issue's verification
contract: break one dependency and EXACTLY that stage goes red while the
others stay green — without disrupting the live :8101 shim / :8765 portal.
"""

from types import SimpleNamespace

import pytest

from agentwire import doctor_voice as dv


def _cfg(**stt):
    """A config-shaped object with an stt section."""
    stt_fields = {"backend": "default", "url": None, "cloud": {}, **stt}
    return SimpleNamespace(
        stt=SimpleNamespace(**stt_fields),
        portal=SimpleNamespace(url="http://localhost:9"),  # dead by default
        server=SimpleNamespace(ssl=SimpleNamespace(cert=None, key=None)),
    )


# === Stage 1: mic / audio capture =========================================


def test_mic_fails_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(dv, "_find_ffmpeg", lambda: None)
    r = dv.check_mic()
    assert r.failed
    assert "ffmpeg" in r.detail
    assert any("install ffmpeg" in f for f in r.fixes)


def test_mic_fails_when_no_input_device(monkeypatch):
    monkeypatch.setattr(dv, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(dv.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dv, "_avfoundation_audio_devices", lambda f: [])
    r = dv.check_mic()
    assert r.failed
    assert "mic permission denied" in r.detail
    assert any("Microphone" in f for f in r.fixes)


def test_mic_ok_with_devices(monkeypatch):
    monkeypatch.setattr(dv, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(dv.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dv, "_avfoundation_audio_devices", lambda f: ["Built-in Mic"])
    r = dv.check_mic()
    assert r.status == "ok"
    assert "Built-in Mic" in r.detail


def test_mic_ok_on_linux_with_ffmpeg(monkeypatch):
    monkeypatch.setattr(dv, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(dv.platform, "system", lambda: "Linux")
    r = dv.check_mic()
    assert r.status == "ok"


def test_mic_info_when_enumeration_unparseable(monkeypatch):
    monkeypatch.setattr(dv, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(dv.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dv, "_avfoundation_audio_devices", lambda f: None)
    r = dv.check_mic()
    assert r.status == "info"


# === Stage 2: STT process ==================================================


def test_stt_default_fails_when_shim_down(monkeypatch):
    import agentwire.voice_status as vs
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (False, "connection refused"))
    import agentwire.stt as stt_mod
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: True)
    r = dv.check_stt(_cfg())
    assert r.failed
    assert "not responding" in r.detail
    assert any("agentwire stt start" in f for f in r.fixes)


def test_stt_default_ok_when_shim_healthy(monkeypatch):
    import agentwire.voice_status as vs
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (True, None))
    import agentwire.stt as stt_mod
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: True)
    r = dv.check_stt(_cfg())
    assert r.status == "ok"
    assert "healthy" in r.detail
    assert "8101" in r.detail


def test_stt_default_info_when_moonshine_absent(monkeypatch):
    import agentwire.stt as stt_mod
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: False)
    r = dv.check_stt(_cfg())
    assert r.status == "info"
    assert "browser" in r.detail.lower()


def test_stt_custom_fails_when_shim_down(monkeypatch):
    import agentwire.voice_status as vs
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (False, "connection refused"))
    r = dv.check_stt(_cfg(backend="custom", url="http://localhost:9"))
    assert r.failed
    assert "custom" in r.detail


def test_stt_cloud_fails_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = dv.check_stt(_cfg(backend="cloud"))
    assert r.failed
    assert "OPENAI_API_KEY" in r.detail


def test_stt_cloud_ok_with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    r = dv.check_stt(_cfg(backend="cloud"))
    assert r.status == "ok"


# === Stage 3: tunnel / portal reachability =================================


def test_portal_fails_when_down(monkeypatch):
    monkeypatch.setattr(dv, "_http_health", lambda url, timeout=2.0: (False, None))
    r = dv.check_portal(_cfg())
    assert r.failed
    assert "not responding" in r.detail
    assert any("agentwire portal start" in f for f in r.fixes)


def test_portal_ok_when_up_no_tunnels(monkeypatch):
    monkeypatch.setattr(dv, "_http_health", lambda url, timeout=2.0: (True, {"status": "ok"}))
    r = dv.check_portal(_cfg(), ctx=None)
    assert r.status == "ok"


def test_portal_fails_when_tunnel_down(monkeypatch):
    monkeypatch.setattr(dv, "_http_health", lambda url, timeout=2.0: (True, {"status": "ok"}))

    spec = SimpleNamespace(local_port=8765, remote_machine="pc", remote_port=8765)
    ctx = SimpleNamespace(get_required_tunnels=lambda: [spec])

    class _TM:
        def check_tunnel(self, s):
            return SimpleNamespace(status="down")

    import agentwire.tunnels as tunnels_mod
    monkeypatch.setattr(tunnels_mod, "TunnelManager", _TM)
    r = dv.check_portal(_cfg(), ctx=ctx)
    assert r.failed
    assert "tunnel" in r.detail.lower()
    assert any("tunnels up" in f for f in r.fixes)


# === Stage 4: tmux wiring + PTT binding ====================================


def test_tmux_fails_when_binary_missing(monkeypatch):
    monkeypatch.setattr(dv.shutil, "which", lambda n: None)
    r = dv.check_tmux_ptt(_cfg())
    assert r.failed
    assert "tmux" in r.detail


def test_tmux_info_when_no_server(monkeypatch):
    monkeypatch.setattr(dv.shutil, "which", lambda n: "/usr/bin/tmux")
    monkeypatch.setattr(dv, "_tmux_server_running", lambda: False)
    r = dv.check_tmux_ptt(_cfg())
    assert r.status == "info"


def test_tmux_ok_with_hammerspoon_binding(monkeypatch):
    monkeypatch.setattr(dv.shutil, "which", lambda n: "/usr/bin/tmux")
    monkeypatch.setattr(dv, "_tmux_server_running", lambda: True)
    monkeypatch.setattr(dv.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dv, "_hammerspoon_ptt_bound", lambda: True)
    r = dv.check_tmux_ptt(_cfg())
    assert r.status == "ok"
    assert "PTT binding present" in r.detail


# === Integration: exactly one stage red ===================================


def test_only_stt_red_when_shim_down(monkeypatch):
    """The headline verification: kill STT, only stage 2 goes red."""
    monkeypatch.setattr(dv, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(dv.platform, "system", lambda: "Linux")  # skip device enum

    import agentwire.stt as stt_mod
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: True)
    monkeypatch.setattr(dv.shutil, "which", lambda n: "/usr/bin/tmux")
    monkeypatch.setattr(dv, "_tmux_server_running", lambda: True)

    # STT shim down (dead) — resolver probes via voice_status._probe.
    import agentwire.voice_status as vs
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (False, "connection refused"))

    # Portal up — check_portal still uses dv._http_health.
    monkeypatch.setattr(dv, "_http_health", lambda url, timeout=2.0: (True, {"status": "ok"}))
    cfg = _cfg()
    cfg.portal = SimpleNamespace(url="http://localhost:8765")  # up per fake_health

    results = dv.run_voice_loop_checks(cfg, ctx=None)
    by_name = {r.name: r for r in results}
    assert by_name["STT process"].failed
    assert not by_name["Mic / audio capture"].failed
    assert not by_name["Tunnel / portal reachability"].failed
    assert not by_name["tmux wiring + PTT binding"].failed
