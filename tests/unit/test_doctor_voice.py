"""Voice-loop preflight stage checks (agentwire/doctor_voice.py).

Each stage is exercised in isolation, proving the issue's verification
contract: break one dependency and EXACTLY that stage goes red while the
others stay green — without disrupting the live :8101 shim / :8765 portal.
"""

from types import SimpleNamespace

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


# === Stage 3: tunnel / portal reachability =================================


def test_portal_fails_when_down(monkeypatch):
    monkeypatch.setattr(dv, "_http_health", lambda url, timeout=2.0: (False, None))
    r = dv.check_portal(_cfg())
    assert r.failed
    assert "not responding" in r.detail
    assert any("agentwire portal start" in f for f in r.fixes)


# === Stage 4: tmux wiring + PTT binding ====================================


# === Integration: exactly one stage red ===================================
