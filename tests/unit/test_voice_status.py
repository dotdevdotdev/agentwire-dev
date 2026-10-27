"""Tests for the active-tier voice status resolver (#441).

The resolver is the SSOT every status surface shares: resolve the active tier,
report the path that tier uses, only probe a server when the tier has one, and
flag an orphaned engine server (running but unused).
"""

from types import SimpleNamespace

import agentwire.voice_status as vs


def _cfg(tts_backend="default", tts_url=None, stt_backend="default",
         stt_url=None, stt_cloud=None):
    return SimpleNamespace(
        tts=SimpleNamespace(backend=tts_backend, url=tts_url),
        stt=SimpleNamespace(backend=stt_backend, url=stt_url, cloud=stt_cloud or {}),
    )


# === TTS ===================================================================


def test_tts_default_ready_and_flags_orphan(monkeypatch):
    """Default tier is always ready (browser/OS) and flags a shim left running
    on the custom-tier port — the exact #441 dogfooding scenario."""
    monkeypatch.setattr(vs, "_tts_service_url", lambda: "http://localhost:8100")
    # An engine server *is* up on the custom-tier port.
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (True, None))
    st = vs.resolve_tts_status(_cfg(tts_backend="default"))
    assert st.tier == "default"
    assert st.ready is True
    assert st.server_url is None  # default tier probes no server in its path
    assert any("unused" in w for w in st.warnings)


# === STT ===================================================================


def test_stt_cloud_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    st = vs.resolve_stt_status(_cfg(stt_backend="cloud"))
    assert st.ready is False
    assert "OPENAI_API_KEY" in st.detail


# === STT disabled (none tier, #679) ========================================
