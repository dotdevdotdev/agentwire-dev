"""Tests for session context-bar parsing (issue #442, Phase 0 — observe only).

Locks in the verified semantic: the Claude Code footer bar percentage is
context REMAINING (headroom before auto-compact), not usage — so a LOW number
means a bloated session. Parsing is pure string work on captured pane text;
these tests cover the real bar shapes seen on live sessions plus the daemon /
no-bar skip paths.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agentwire import session_context as sc
from agentwire.config import CustomServiceConfig

# Real captures from live sessions (2026-06-22): the bar width tracks terminal
# width, so the same 92% renders with different glyph counts.
WIDE_BAR = "  [████████████████████████████████████████████████████████████████████████████████░░░░░░░░] 92%"
NARROW_BAR = "  [████████████████████████████████████████████████████████████████░░░░░░] 92%"
LOW_BAR = "  [█████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 12%"
META_LINE = "  ~/projects/agentwire-dev  main                       opus  $1805.19  7988m"


def test_parse_bar_wide_and_narrow_same_pct():
    assert sc.parse_context_bar(WIDE_BAR) == 92
    assert sc.parse_context_bar(NARROW_BAR) == 92


def test_parse_bar_low():
    assert sc.parse_context_bar(LOW_BAR) == 12


def test_parse_bar_with_ansi():
    noisy = "\x1b[2m" + WIDE_BAR + "\x1b[0m"
    assert sc.parse_context_bar(noisy) == 92


def test_no_bar_returns_none():
    assert sc.parse_context_bar("INFO: 127.0.0.1 - GET /health 200 OK") is None
    assert sc.parse_context_bar("[2026-06-22 07:43:41] Nothing due. Sleeping 0s...") is None
    assert sc.parse_context_bar("") is None


def test_out_of_range_rejected():
    assert sc.parse_context_bar("[██░░] 150%") is None


def test_parse_model():
    assert sc.parse_model(META_LINE) == "opus"
    assert sc.parse_model("  main  sonnet  $1.00") == "sonnet"
    assert sc.parse_model("no model here") is None


def _ctx(command: str, pane_text: str):
    """Build a SessionContext with pane command + capture mocked."""
    with patch.object(sc, "_pane_command", return_value=command), patch.object(
        sc, "_capture", return_value=pane_text
    ):
        return sc.session_context("s", warn_threshold=20)


def test_agent_session_healthy():
    c = _ctx("node", "\n".join([META_LINE, WIDE_BAR]))
    assert c.is_agent is True
    assert c.remaining_pct == 92
    assert c.model == "opus"
    assert c.flagged is False


def test_agent_session_flagged_when_low():
    c = _ctx("2.1.185", "\n".join([META_LINE, LOW_BAR]))
    assert c.is_agent is True
    assert c.remaining_pct == 12
    assert c.flagged is True  # 12 <= 20 warn threshold
    assert "LOW" in c.note


def test_daemon_skipped_gracefully():
    c = _ctx("python3.13", "INFO: GET /health 200 OK")
    assert c.is_agent is False
    assert c.remaining_pct is None
    assert c.flagged is False
    assert "daemon" in c.note


def test_agent_pane_no_bar_visible():
    c = _ctx("node", "...busy render, no footer yet...")
    assert c.is_agent is True
    assert c.remaining_pct is None
    assert c.flagged is False
    assert "no bar" in c.note


def test_threshold_boundary_inclusive():
    at = _ctx("node", "\n".join([META_LINE, "[██░░] 20%"]))
    assert at.flagged is True  # remaining == threshold is flagged
    above = _ctx("node", "\n".join([META_LINE, "[██░░] 21%"]))
    assert above.flagged is False


# ── Regex hardening (Phase 1, issue #442) ────────────────────────────────────
# Once auto-/clear keys off the parsed value, a stray bracketed bar must not be
# able to trigger an action. The bar must be the trailing token of its line, and
# when several bars are on screen the widest one (the real footer) wins.


def test_bar_must_be_trailing_token():
    # A bar followed by more text on the same line is NOT the Claude footer.
    assert sc.parse_context_bar("[█████░░░░░] 50% remaining, downloading...") is None
    assert sc.parse_context_bar("CPU [███░░] 50% load  user 3%") is None


def test_trailing_bar_with_label_still_parses():
    # Trailing IS allowed even with a preceding label — the trailing-token rule
    # can't distinguish a lone fake bar; the idle/empty-prompt gate is the
    # belt-and-suspenders for that (documented residual).
    assert sc.parse_context_bar("model  $1.00  [████████░░] 80%") == 80


def test_longest_glyph_run_wins():
    # A worker's short progress bar + the wide real footer on separate lines:
    # the wide one (the actual context bar) must win, not the leftmost match.
    screen = "\n".join([
        "[██░] 5%",  # short, would falsely read 5% if leftmost won
        META_LINE,
        WIDE_BAR,    # the real footer, 92%
    ])
    assert sc.parse_context_bar(screen) == 92


def test_all_spaces_bracket_rejected():
    assert sc.parse_context_bar("[     ] 80%") is None


def test_trailing_whitespace_tolerated():
    assert sc.parse_context_bar("[████░░] 30%   ") == 30


# ── Policy resolution (Phase 1) ──────────────────────────────────────────────


def _cfg(policies=None):
    """Minimal fake Config for resolve_policy (only the fields it reads)."""
    return SimpleNamespace(
        session_context=SimpleNamespace(
            warn_remaining_pct=20, auto_enabled=True, policies=policies or {},
        ),
        services=SimpleNamespace(custom=[]),
    )


def _svc(name, policy="none"):
    return CustomServiceConfig(name=name, context_policy=policy)


def test_resolve_policy_service_default_on():
    cfg = _cfg()
    with patch.object(sc, "_warn_threshold", return_value=20), patch(
        "agentwire.services.registry",
        return_value=[_svc("agentwire-notifications", "clear")],
    ):
        assert sc.resolve_policy("agentwire-notifications", cfg) == "clear"


def test_resolve_policy_config_override_wins():
    cfg = _cfg(policies={"agentwire-notifications": "compact"})
    with patch(
        "agentwire.services.registry",
        return_value=[_svc("agentwire-notifications", "clear")],
    ):
        # Explicit per-session override beats the service-registry default.
        assert sc.resolve_policy("agentwire-notifications", cfg) == "compact"


def test_resolve_policy_unknown_session_is_none():
    cfg = _cfg()
    with patch("agentwire.services.registry", return_value=[]):
        assert sc.resolve_policy("some-random-session", cfg) == "none"


def test_resolve_policy_invalid_service_value_is_none():
    cfg = _cfg()
    with patch(
        "agentwire.services.registry",
        return_value=[_svc("svc", "nonsense")],
    ):
        assert sc.resolve_policy("svc", cfg) == "none"


# ── act_on_session (Phase 1) ─────────────────────────────────────────────────


def _ctx_obj(remaining, is_agent=True, flagged=None):
    if flagged is None:
        flagged = remaining is not None and remaining <= 20
    return sc.SessionContext(
        session="s", pane=0, is_agent=is_agent, remaining_pct=remaining,
        model="opus", flagged=flagged, note="",
    )


def test_act_skips_when_above_threshold():
    with patch.object(sc, "session_context", return_value=_ctx_obj(80)):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["skipped"] == "above_threshold"


def test_act_skips_when_no_bar():
    with patch.object(sc, "session_context", return_value=_ctx_obj(None, is_agent=True)):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["skipped"] == "no_bar"


def test_act_skips_when_no_policy():
    r = sc.act_on_session("s", "none", threshold=20)
    assert r["acted"] is False
    assert r["skipped"] == "no_policy"


def test_act_defers_when_box_not_empty():
    # Collision guard: a non-empty / unparseable box defers before any paste.
    with patch.object(sc, "session_context", return_value=_ctx_obj(10)), patch(
        "agentwire.prompt_router.prompt_is_empty", return_value=False
    ), patch("agentwire.prompt_router.safe_deliver") as deliver, patch.object(
        sc, "_log_event"
    ):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["deferred"] == "box_not_empty"
    deliver.assert_not_called()  # never even attempt the paste


def test_act_defers_when_safe_deliver_refuses():
    # An empty box but safe_deliver refuses (parked / live-menu) or the paste
    # can't be verified — logged honestly as NOT acted, retried next tick.
    with patch.object(sc, "session_context", return_value=_ctx_obj(10)), patch(
        "agentwire.prompt_router.prompt_is_empty", return_value=True
    ), patch(
        "agentwire.prompt_router.safe_deliver",
        return_value=(False, "delivery_unverified"),
    ), patch.object(sc, "_log_event"):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["deferred"] == "delivery_unverified"


def test_act_sends_command_when_flagged_and_verified():
    # Happy path: empty box + verified safe_deliver => acted, via safe_deliver
    # (NOT a raw paste) so the audit log is honest.
    with patch.object(sc, "session_context", return_value=_ctx_obj(10)), patch(
        "agentwire.prompt_router.prompt_is_empty", return_value=True
    ), patch(
        "agentwire.prompt_router.safe_deliver", return_value=(True, "delivered")
    ) as deliver, patch.object(sc, "_log_event"):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is True
    assert r["command"] == "/clear"
    deliver.assert_called_once_with("s", 0, "/clear")


def test_act_compact_routes_compact_through_safe_deliver():
    with patch.object(sc, "session_context", return_value=_ctx_obj(5)), patch(
        "agentwire.prompt_router.prompt_is_empty", return_value=True
    ), patch(
        "agentwire.prompt_router.safe_deliver", return_value=(True, "delivered")
    ) as deliver, patch.object(sc, "_log_event"):
        r = sc.act_on_session("s", "compact", threshold=20)
    assert r["acted"] is True
    assert r["command"] == "/compact"
    deliver.assert_called_once_with("s", 0, "/compact")


# ── tick (Phase 1) ───────────────────────────────────────────────────────────


def test_tick_skips_when_auto_disabled():
    cfg = _cfg()
    cfg.session_context.auto_enabled = False
    with patch("agentwire.config.get_config", return_value=cfg):
        assert sc.tick() == {"skipped": "disabled"}


def test_tick_acts_only_on_opted_in_sessions():
    cfg = _cfg()
    calls = []

    def fake_act(session, policy, threshold):
        calls.append((session, policy))
        return {"session": session, "acted": True, "command": "/clear",
                "remaining_pct": 10, "policy": policy}

    def fake_resolve(session, c):
        return "clear" if session == "agentwire-notifications" else "none"

    with patch("agentwire.config.get_config", return_value=cfg), patch.object(
        sc, "_list_local_sessions",
        return_value=["agentwire-notifications", "fragmentz", "agentwire-scheduler"],
    ), patch.object(sc, "resolve_policy", side_effect=fake_resolve), patch.object(
        sc, "act_on_session", side_effect=fake_act
    ):
        result = sc.tick()

    # Only the opted-in session was evaluated/acted on — never fragmentz.
    assert calls == [("agentwire-notifications", "clear")]
    assert [e["session"] for e in result["acted"]] == ["agentwire-notifications"]
