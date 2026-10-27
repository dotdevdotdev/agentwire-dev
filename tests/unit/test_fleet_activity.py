"""The fleet's own signals reach the buddy — and the ruling on which ones SPEAK (#1016).

Same two-directional shape as ``test_fleet_alerts``, because this producer has
the same two ways to be wrong and they are not equally cheap:

* **The false-REJECT half** — the state before this module. The idle handler,
  the notify family and the scheduler all knew a job had finished, and none of
  that reached a listener without a parent link, so the buddy could not check
  in on work the owner had delegated.
* **The false-ACCEPT half, which is the expensive one.** Everything in the
  buddy's spool is eventually SPOKEN — the notifier volunteers unread mail at a
  gap (#962). So a producer that spools the fleet's ordinary churn does not add
  a feature, it turns the buddy into a narrator, and the owner's move against a
  narrator is to stop listening. That is why the ruling
  (:data:`fleet_activity.ANNOUNCE` plus the kinds in
  ``fleet_alerts.DETECTOR_KINDS``) is pinned here as DATA: widening what may
  speak has to be a deliberate edit to a test that says why.

The sharpest single case is ``spoke``. A session that speaks through fleet TTS
is heard by the owner in the room; a buddy that reads it back is the two-surface
problem made worse, not solved. It is recorded and never announced, and that is
asserted directly rather than left to follow from a table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentwire import core, fleet_activity, fleet_alerts, inbox


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """A throwaway config dir: session records, inboxes, ledger and event logs."""
    root = tmp_path / "agentwire"
    (root / "sessions").mkdir(parents=True)
    monkeypatch.setattr(core, "CONFIG_DIR", root)
    monkeypatch.setattr(inbox, "INBOX_ROOT", root / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", root / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    return root


def _record(name: str, **fields) -> None:
    core.store_session_metadata(name, {"created_at": "x", **fields})


def _subscribe(name: str = "buddy", **fields) -> str:
    _record(name, **fields)
    fleet_alerts.subscribe(name)
    return name


def _mail(session: str) -> list:
    return inbox.list_messages(session) + inbox.list_ingest(session)


def _at(module, monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(module, "_now", lambda: when)


# =============================================================================
# The ruling, pinned as data
# =============================================================================


# =============================================================================
# spoke — the two-audio-surfaces case
# =============================================================================


# =============================================================================
# session_idle — delegated work only
# =============================================================================


def test_delegated_session_going_idle_is_announced(isolate):
    buddy = _subscribe()
    _record("auth-fix", created_by="orchestrator")

    fleet_activity.note_session_idle("auth-fix", "is idle and done working")

    messages = _mail(buddy)
    assert len(messages) == 1
    assert messages[0].kind == "done"
    assert messages[0].sender == fleet_activity.SENDER
    # The name PREFIXES the caller's predicate — this is a sentence the owner
    # hears out loud, and the obvious concatenation says "is idle" twice.
    assert messages[0].text == "auth-fix is idle and done working"


# =============================================================================
# task_completed — the kind carries the fleet's verdict
# =============================================================================


# =============================================================================
# toasts
# =============================================================================


# =============================================================================
# Throttling
# =============================================================================


def test_a_flapping_session_buys_one_utterance_not_many(isolate, monkeypatch):
    buddy = _subscribe()
    _record("flapper", created_by="boss")
    start = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    _at(fleet_activity, monkeypatch, start)
    fleet_activity.note_session_idle("flapper", "done")
    _at(fleet_activity, monkeypatch, start + timedelta(minutes=1))
    result = fleet_activity.note_session_idle("flapper", "done again")

    assert result["throttled"] is True
    assert result["announced"] == []
    assert len(_mail(buddy)) == 1
    # Recorded both times: the ledger is the awareness tier, and suppressing
    # the RECORD would lose the thing the buddy is meant to be able to look up.
    assert len(fleet_activity.recent(event="session_idle")) == 2


# =============================================================================
# The ledger itself
# =============================================================================


# =============================================================================
# Sender discipline — the recursion guard
# =============================================================================


# =============================================================================
# The WIRING — a producer nobody calls is a feature that does not exist
# =============================================================================
#
# The module above can be perfect and the fleet still blind: what makes this
# feature real is that four live surfaces call it. So each producer is driven
# through the function the fleet actually reaches (the CLI handler, the
# scheduler's own event log), never through a re-implementation of it — the
# "deployment is the executing path" lesson, applied to a producer.


def _ns(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def test_an_idle_session_records_through_the_notify_cli(isolate, monkeypatch):
    from agentwire import notify_cli, pane_manager, prompt_router, services
    from agentwire.notify_cli import cmd_notify_parent

    _subscribe()
    _record("child", created_by="orch")
    monkeypatch.setattr(pane_manager, "get_current_session", lambda: "child")
    monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda: 0)
    monkeypatch.setattr(prompt_router, "resolve_parent", lambda s, p: ("orch", 0))
    monkeypatch.setattr(services, "is_service_session", lambda s: False)
    monkeypatch.setattr(notify_cli, "_output_result", lambda *a, **kw: 0)

    cmd_notify_parent(_ns(text=["is", "idle"], to=None, json=False, quiet=True,
                          raw=False, on_idle=True, queued=True))

    assert [e["event"] for e in fleet_activity.recent()] == ["session_idle"]
    assert len(_mail("buddy")) == 1


@pytest.fixture
def portal(monkeypatch):
    """A portal that accepts toasts, patched at core's ONE HTTP call.

    Deliberately not patched at `_post_desktop_notification`: that is the seam
    under test. A test that stubs it proves the producer called *something*,
    which is exactly the assurance that let the MCP producer ship unrecorded.
    """
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "id": "n1", "clients": 1}

    calls = []
    monkeypatch.setattr(
        "agentwire.core.portal_request",
        lambda method, url, **kw: calls.append((method, url, kw.get("json"))) or _Response(),
    )
    return calls


def _say_env(channels_cli, monkeypatch, *, rc: int = 0, browser: bool = True):
    """Everything `cmd_say` reaches outside itself, stubbed."""
    monkeypatch.setattr(channels_cli, "load_config", lambda: {"tts": {}})
    monkeypatch.setattr(channels_cli, "get_voice_from_config", lambda: None)
    monkeypatch.setattr(channels_cli, "_get_current_tmux_session", lambda: "worker-1")
    monkeypatch.setattr(channels_cli, "_infer_session_from_path", lambda: None)
    monkeypatch.setattr(channels_cli, "_handle_voice_notifications", lambda *a, **kw: None)
    monkeypatch.setattr(channels_cli, "_get_portal_url", lambda: "https://portal")
    monkeypatch.setattr(channels_cli, "_check_portal_connections",
                        lambda s, url: (browser, s, 2))
    monkeypatch.setattr(channels_cli, "_remote_say", lambda *a, **kw: rc)


