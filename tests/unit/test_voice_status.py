"""Tests for the active-tier voice status resolver (#441).

The resolver is the SSOT every status surface shares: resolve the active tier,
report the path that tier uses, only probe a server when the tier has one, and
flag an orphaned engine server (running but unused).
"""

from types import SimpleNamespace

import pytest

import agentwire.voice_status as vs


@pytest.fixture(autouse=True)
def _no_live_portal(monkeypatch):
    """Keep the resolver off the network: no running portal unless a test
    overrides this (the #679 effective-backend query)."""
    monkeypatch.setattr(vs, "_portal_effective_stt_backend", lambda: None)


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


def test_tts_default_no_orphan_when_port_empty(monkeypatch):
    monkeypatch.setattr(vs, "_tts_service_url", lambda: "http://localhost:8100")
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (False, "refused"))
    st = vs.resolve_tts_status(_cfg(tts_backend="default"))
    assert st.ready is True
    assert st.warnings == []


def test_tts_default_skips_probe_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(vs, "_probe", lambda *a, **k: calls.append(1) or (True, None))
    st = vs.resolve_tts_status(_cfg(tts_backend="default"), probe=False)
    assert st.ready is True
    assert calls == []  # no orphan probe when probing is off


def test_tts_custom_probes_shim(monkeypatch):
    monkeypatch.setattr(vs, "_tts_service_url", lambda: "http://localhost:8100")
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (True, None))
    st = vs.resolve_tts_status(_cfg(tts_backend="custom", tts_url="http://localhost:8100"))
    assert st.tier == "custom"
    assert st.ready is True
    assert st.server_url == "http://localhost:8100"


def test_tts_custom_down(monkeypatch):
    monkeypatch.setattr(vs, "_tts_service_url", lambda: "http://localhost:8100")
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (False, "connection refused"))
    st = vs.resolve_tts_status(_cfg(tts_backend="custom", tts_url="http://localhost:8100"))
    assert st.ready is False
    assert "not responding" in st.detail


# === STT ===================================================================


def test_stt_cloud_ready_with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    st = vs.resolve_stt_status(_cfg(stt_backend="cloud"))
    assert st.tier == "cloud"
    assert st.ready is True
    assert st.server_url is None  # cloud has no shim to probe


def test_stt_cloud_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    st = vs.resolve_stt_status(_cfg(stt_backend="cloud"))
    assert st.ready is False
    assert "OPENAI_API_KEY" in st.detail


def test_stt_default_browser_fallback(monkeypatch):
    import agentwire.stt as stt_mod
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: False)
    st = vs.resolve_stt_status(_cfg(stt_backend="default"))
    assert st.ready is True
    assert st.server_url is None  # browser fallback — no host shim
    assert "browser" in st.path.lower()


def test_stt_default_probes_shim_when_moonshine_present(monkeypatch):
    import agentwire.stt as stt_mod
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: True)
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (True, None))
    st = vs.resolve_stt_status(_cfg(stt_backend="default"))
    assert st.ready is True
    assert st.server_url is not None
    assert "8101" in st.server_url


# === STT disabled (none tier, #679) ========================================


def test_stt_none_reports_disabled(monkeypatch):
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (False, "refused"))
    st = vs.resolve_stt_status(_cfg(stt_backend="none"))
    assert st.tier == "none"
    assert st.ready is True
    assert st.server_url is None
    assert "disabled" in st.detail.lower()
    assert st.warnings == []


def test_stt_none_flags_orphan_shim(monkeypatch):
    """A shim still answering on :8101 while STT is disabled is an orphan."""
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (True, None))
    st = vs.resolve_stt_status(_cfg(stt_backend="none"))
    assert st.tier == "none"
    assert any("unused" in w for w in st.warnings)


def test_stt_none_skips_probe_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(vs, "_probe", lambda *a, **k: calls.append(1) or (True, None))
    st = vs.resolve_stt_status(_cfg(stt_backend="none"), probe=False)
    assert st.tier == "none"
    assert calls == []


def test_stt_portal_runtime_no_stt_wins_over_config(monkeypatch):
    """`portal start --no-stt` flips the RUNNING portal's backend to none
    without touching config.yaml — the live answer must win (#679)."""
    monkeypatch.setattr(vs, "_portal_effective_stt_backend", lambda: "none")
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (False, "refused"))
    st = vs.resolve_stt_status(_cfg(stt_backend="default"))
    assert st.tier == "none"
    assert "disabled" in st.detail.lower()


def test_stt_portal_unreachable_falls_back_to_config(monkeypatch):
    monkeypatch.setattr(vs, "_portal_effective_stt_backend", lambda: None)
    import agentwire.stt as stt_mod
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: True)
    monkeypatch.setattr(vs, "_probe", lambda url, endpoint="/health", timeout=2.0: (True, None))
    st = vs.resolve_stt_status(_cfg(stt_backend="default"))
    assert st.tier == "default"


def test_stt_no_portal_query_when_probe_off(monkeypatch):
    """probe=False must not hit the portal either (used by fast paths)."""
    def boom():
        raise AssertionError("portal queried with probe=False")
    monkeypatch.setattr(vs, "_portal_effective_stt_backend", boom)
    import agentwire.stt as stt_mod
    monkeypatch.setattr(stt_mod, "moonshine_importable", lambda: True)
    st = vs.resolve_stt_status(_cfg(stt_backend="default"), probe=False)
    assert st.tier == "default"
