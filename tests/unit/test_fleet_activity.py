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

import json
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


def test_announceable_events_have_a_kind():
    """Every event that may speak must have a ruled kind, in the shared table.

    The two tables are deliberately separate axes — ANNOUNCE says *whether and
    how often*, DETECTOR_KINDS says *how loudly* — so an event listed in one
    and missing from the other is a producer that looks wired and raises
    KeyError at the moment it fires.
    """
    for event in fleet_activity.ANNOUNCE:
        assert event in fleet_alerts.DETECTOR_KINDS, event


def test_no_lifecycle_event_is_ever_an_escalation():
    """The interrupt tier stays the two conditions nothing clears without a human.

    An escalation cuts across the buddy's own speech (#967). Nothing in a
    session going idle or a task finishing is burning while it waits, and an
    escalation that turns out to be ignorable retires the tier for the one that
    was not.
    """
    for event in fleet_activity.ANNOUNCE:
        assert fleet_alerts.DETECTOR_KINDS[event] != "escalation", event


def test_ordinary_churn_is_not_announceable():
    """The ledger-only events, named. Adding one here is a deliberate edit."""
    for event in ("spoke", "toast", "session_created", "session_closed", "pane_died"):
        assert event not in fleet_activity.ANNOUNCE, event


# =============================================================================
# spoke — the two-audio-surfaces case
# =============================================================================


def test_spoken_audio_is_recorded_but_never_announced(isolate):
    """The owner already heard it. Reading it back is worse than silence."""
    buddy = _subscribe()
    fleet_activity.note_spoke("the build is green", session="worker-1", sink="browser")

    assert _mail(buddy) == []
    entries = fleet_activity.recent()
    assert [e["event"] for e in entries] == ["spoke"]
    assert entries[0]["text"] == "the build is green"
    assert entries[0]["sink"] == "browser"
    assert entries[0]["announced"] is False


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


@pytest.mark.parametrize(
    "fields",
    [
        {"role": "worker"},
        {"role": "reviewer"},
        {"worktree_path": "/Users/x/worktrees/proj/auth"},
    ],
)
def test_every_delegation_axis_counts(isolate, fields):
    """#716's three axes are independent — a worker with no recorded parent is
    still somebody's delegated work, and so is a worktree checkout."""
    buddy = _subscribe()
    _record("child", **fields)
    fleet_activity.note_session_idle("child", "done")
    assert len(_mail(buddy)) == 1


def test_root_orchestrator_idle_is_ledger_only(isolate):
    """The false-accept half. An interactive session goes idle after EVERY turn;
    announcing that fires once per exchange the owner has with their own
    session, which is how a channel earns being ignored."""
    buddy = _subscribe()
    _record("orchestrator", role="orchestrator", created_by="")

    fleet_activity.note_session_idle("orchestrator", "is idle and done working")

    assert _mail(buddy) == []
    assert [e["event"] for e in fleet_activity.recent()] == ["session_idle"]


@pytest.mark.parametrize(
    "fields",
    [
        # THE SHAPE THE VERB ACTUALLY PRODUCES. `agentwire orchestrator` is
        # sugar for `worktree --kind orchestrator`, so the owner's durable
        # window carries role=orchestrator, created_by='' AND worktree_path —
        # two live sessions on this machine look exactly like this. A plain OR
        # over #716's axes let the LOCATION axis overrule the ROLE and
        # announced the window the owner was talking to, every 15 minutes.
        {"role": "orchestrator", "created_by": "",
         "worktree_path": "/Users/x/worktrees/proj/spike"},
        # Same veto through the persona axis: Briefing Mode's anchor replaces
        # the orchestrator role and is likewise who the human talks to.
        {"role": "worker", "roles": ["anchor", "contributor"],
         "worktree_path": "/Users/x/worktrees/proj/brief"},
        # And a spawned orchestrator: created_by is set, so the parent branch
        # would have fired if authority did not get to veto first.
        {"role": "orchestrator", "created_by": "boss"},
    ],
)
def test_an_interactive_role_is_never_delegated_whatever_its_location(isolate, fields):
    buddy = _subscribe()
    _record("window", **fields)

    fleet_activity.note_session_idle("window", "is idle and done working")

    assert _mail(buddy) == []
    assert [e["announced"] for e in fleet_activity.recent()] == [False]


def test_unknown_session_idle_is_ledger_only(isolate):
    """No record is not evidence of delegation, and the failure direction is quiet."""
    buddy = _subscribe()
    fleet_activity.note_session_idle("never-registered", "done")
    assert _mail(buddy) == []
    assert len(fleet_activity.recent()) == 1


def test_the_parent_is_excluded_from_the_announcement(isolate):
    """The parent hears this by paste from notify-parent. The same news twice is
    exactly what makes a channel skippable."""
    _record("boss")
    fleet_alerts.subscribe("boss")
    buddy = _subscribe("buddy")
    _record("child", created_by="boss")

    fleet_activity.note_session_idle("child", "done", parent="boss")

    assert _mail("boss") == []
    assert len(_mail(buddy)) == 1


def test_a_session_never_hears_about_itself(isolate):
    _subscribe("child", created_by="boss")
    fleet_activity.note_session_idle("child", "done")
    assert _mail("child") == []


# =============================================================================
# task_completed — the kind carries the fleet's verdict
# =============================================================================


def test_completed_task_is_news(isolate):
    buddy = _subscribe()
    fleet_activity.note_task_completed(
        task="weekly-stars", session="s", status="complete", duration=42,
        summary="7 new stars")
    messages = _mail(buddy)
    assert [m.kind for m in messages] == ["done"]
    assert "weekly-stars" in messages[0].text and "7 new stars" in messages[0].text


@pytest.mark.parametrize("status", ["failed", "incomplete", "timeout"])
def test_a_run_that_ended_badly_asks_for_a_person(isolate, status):
    """`request`, not `done`: the fleet already judged this as needing somebody,
    and flattening that to news throws the judgment away."""
    buddy = _subscribe()
    fleet_activity.note_task_completed(
        task="t", session="s", status=status, duration=1, summary="")
    assert [m.kind for m in _mail(buddy)] == ["request"]


@pytest.mark.parametrize("status", ["usage_limit", "auth_expired"])
def test_detector_owned_failures_are_not_announced_twice(isolate, status):
    """Both conditions are machine-wide and have their own detector, which says
    it once. This producer would say it again per task."""
    buddy = _subscribe()
    fleet_activity.note_task_completed(
        task="t", session="s", status=status, duration=1, summary="")
    assert _mail(buddy) == []
    assert [e["status"] for e in fleet_activity.recent()] == [status]


# =============================================================================
# toasts
# =============================================================================


def test_high_priority_toast_speaks_and_an_ordinary_one_does_not(isolate):
    buddy = _subscribe()
    fleet_activity.note_toast("build is red", session="ci", priority="high")
    fleet_activity.note_toast("build is green", session="ci", priority="normal")

    messages = _mail(buddy)
    assert [m.kind for m in messages] == ["request"]
    assert "red" in messages[0].text
    assert {e["event"] for e in fleet_activity.recent()} == {"toast_high", "toast"}


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


def test_the_cooldown_expires(isolate, monkeypatch):
    buddy = _subscribe()
    _record("flapper", created_by="boss")
    start = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    _at(fleet_activity, monkeypatch, start)
    fleet_activity.note_session_idle("flapper", "done")
    _at(fleet_activity, monkeypatch,
        start + fleet_activity.ANNOUNCE["session_idle"] + timedelta(seconds=1))
    fleet_activity.note_session_idle("flapper", "done again")

    assert len(_mail(buddy)) == 2


def test_the_throttle_is_per_subject(isolate):
    """Two different tasks finishing together are two pieces of news."""
    buddy = _subscribe()
    fleet_activity.note_task_completed(task="a", session="s", status="complete",
                                       duration=1, summary="")
    fleet_activity.note_task_completed(task="b", session="s", status="complete",
                                       duration=1, summary="")
    assert len(_mail(buddy)) == 2


# =============================================================================
# The ledger itself
# =============================================================================


def test_recent_is_newest_first_and_filterable(isolate):
    fleet_activity.record("spoke", session="a", text="one")
    fleet_activity.record("session_idle", session="b", text="two")
    fleet_activity.record("spoke", session="b", text="three")

    assert [e["text"] for e in fleet_activity.recent()] == ["three", "two", "one"]
    assert [e["text"] for e in fleet_activity.recent(event="spoke")] == ["three", "one"]
    assert [e["text"] for e in fleet_activity.recent(session="b")] == ["three", "two"]
    assert [e["text"] for e in fleet_activity.recent(limit=1)] == ["three"]


def test_entries_older_than_the_window_are_history_not_awareness(isolate, monkeypatch):
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    _at(fleet_activity, monkeypatch, now - timedelta(hours=20))
    fleet_activity.record("spoke", session="a", text="ancient")
    _at(fleet_activity, monkeypatch, now)
    fleet_activity.record("spoke", session="a", text="fresh")

    assert [e["text"] for e in fleet_activity.recent()] == ["fresh"]
    assert len(fleet_activity.recent(window=timedelta(hours=48))) == 2


def test_an_unknown_event_is_refused_not_recorded(isolate):
    """A closed vocabulary: a typo must not become a category nothing queries."""
    assert fleet_activity.record("session_exploded", session="a") == {}
    assert fleet_activity.recent() == []


def test_a_corrupt_line_costs_one_entry_not_the_file(isolate):
    fleet_activity.record("spoke", session="a", text="one")
    with open(fleet_activity.ledger_path(), "a") as fh:
        fh.write("{not json\n")
    fleet_activity.record("spoke", session="a", text="two")

    assert [e["text"] for e in fleet_activity.recent()] == ["two", "one"]


@pytest.mark.parametrize("ts", ["2026-08-11T12:00:00", "not-a-date", 7, None])
def test_an_unusable_timestamp_never_raises(isolate, ts):
    """The naive one is the sharp case: it PARSES, then raises on the comparison
    — outside the guard that wrapped only the parse. That tracebacked
    `agentwire activity list` on a single hand-edited line, and falsified the
    "never raises" contract every producer here is written against."""
    fleet_activity.record("spoke", session="a", text="fine")
    with open(fleet_activity.ledger_path(), "a") as fh:
        fh.write(json.dumps({"event": "spoke", "ts": ts, "text": "odd",
                             "session": "a", "subject": "a"}) + "\n")

    assert [e["text"] for e in fleet_activity.recent()] == ["fine"]
    assert fleet_activity.note_spoke("still works", session="a")["announced"] == []


def test_the_ledger_stays_bounded(isolate):
    for i in range(fleet_activity.TRIM_AT + 20):
        fleet_activity.record("spoke", session="a", text=str(i))
    lines = fleet_activity.ledger_path().read_text().splitlines()
    # Amortized, not exact: the trim fires only above TRIM_AT, so the file lives
    # between the two bounds. Asserting equality with MAX_ENTRIES would be
    # asserting a whole-file rewrite per append, which is the cost the slack
    # exists to avoid.
    assert fleet_activity.MAX_ENTRIES <= len(lines) <= fleet_activity.TRIM_AT
    # The TAIL is what survives — trimming from the wrong end would keep the
    # oldest entries and answer "what just happened" with last week.
    assert json.loads(lines[-1])["text"] == str(fleet_activity.TRIM_AT + 19)


def test_an_unwritable_ledger_never_breaks_a_producer(isolate, monkeypatch):
    """Speaking, toasting and dispatching are the jobs; awareness is the bonus."""
    monkeypatch.setattr(fleet_activity, "ledger_path",
                        lambda: isolate / "nope" / "\0" / "ledger.jsonl")
    assert fleet_activity.note_spoke("hello")["announced"] == []


def test_an_unreachable_inbox_never_breaks_a_producer(isolate, monkeypatch):
    _subscribe()
    _record("child", created_by="boss")

    def boom(*a, **kw):
        raise OSError("inbox on fire")

    monkeypatch.setattr(fleet_alerts, "emit_for", boom)
    result = fleet_activity.note_session_idle("child", "done")
    assert result["announced"] == []
    assert len(fleet_activity.recent()) == 1


# =============================================================================
# Sender discipline — the recursion guard
# =============================================================================


def test_activity_mail_carries_its_own_sender(isolate):
    buddy = _subscribe()
    fleet_activity.note_task_completed(task="t", session="s", status="complete",
                                       duration=1, summary="")
    assert _mail(buddy)[0].sender == fleet_activity.SENDER
    assert fleet_activity.SENDER != fleet_alerts.SENDER


def test_a_lost_activity_notice_does_not_alert_about_itself(isolate):
    """The dead-letter alert path drops our own stranded mail BY SENDER. Activity
    joins that set: it is news by construction, and an alert about a lost
    'session went idle' would land exactly when the fleet is already noisy
    enough to be stranding mail."""
    assert fleet_activity.SENDER in fleet_alerts.MACHINE_SENDERS


def test_a_foreign_sender_is_refused(isolate):
    """It can only be a coding bug, and swallowing it would re-open the loop the
    guard above closes."""
    _subscribe()
    with pytest.raises(ValueError):
        fleet_alerts.emit("x", kind="note", sender="some-agent")


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


def test_a_service_session_going_idle_is_not_even_recorded(isolate, monkeypatch):
    """Services cycle idle constantly and are nobody's delegated work. The
    existing skip runs BEFORE this producer, and that ordering is the whole
    reason the ledger isn't dominated by the portal."""
    from agentwire import pane_manager, services
    from agentwire.notify_cli import cmd_notify_parent

    _subscribe()
    _record("agentwire-portal", created_by="orch")
    monkeypatch.setattr(pane_manager, "get_current_session", lambda: "agentwire-portal")
    monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda: 0)
    monkeypatch.setattr(services, "is_service_session", lambda s: True)

    cmd_notify_parent(_ns(text=["is", "idle"], to=None, json=True, quiet=True,
                          raw=False, on_idle=True, queued=True))

    assert fleet_activity.recent() == []


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


def test_a_toast_records_through_the_notify_cli(isolate, portal, monkeypatch):
    from agentwire import notify_cli
    from agentwire.notify_cli import cmd_notify_user

    _subscribe()
    monkeypatch.setattr(notify_cli, "_output_result", lambda *a, **kw: 0)

    cmd_notify_user(_ns(text=["build", "is", "red"], session="ci",
                        priority="high", json=False))

    assert [e["event"] for e in fleet_activity.recent()] == ["toast_high"]
    assert [m.kind for m in _mail("buddy")] == ["request"]


def test_a_toast_records_through_the_mcp_tool(isolate, portal):
    """The producer agents actually reach. CLAUDE.md's rule is MCP for agents
    and CLI for humans, so a CLI-side hook sees exactly the toasts a human
    posted — and none of the ones the fleet posts. It POSTed on its own
    transport; now every toast producer goes through one seam."""
    _subscribe()
    from agentwire.mcp_notify import notify_user

    notify_user("deploy needs a decision", session="ci", priority="high")

    assert [e["event"] for e in fleet_activity.recent()] == ["toast_high"]
    assert [m.kind for m in _mail("buddy")] == ["request"]


def test_the_briefing_display_card_records_too(isolate, portal, monkeypatch):
    """`say(display=...)` posts its own toast — the third producer."""
    from agentwire import channels_cli

    _say_env(channels_cli, monkeypatch, rc=0)
    channels_cli.cmd_say(_ns(text=["spoken", "headline"], json=True, voice=None,
                             exaggeration=None, cfg=None, session=None,
                             display="**the richer card**", backend=None,
                             instructions=None, language="English", stream=False))

    assert {e["event"] for e in fleet_activity.recent()} == {"toast", "spoke"}


def test_a_toast_the_portal_refused_is_still_recorded(isolate, monkeypatch):
    """The case where awareness matters MOST: the screen never got it."""
    from agentwire import notify_cli
    from agentwire.notify_cli import cmd_notify_user

    def unreachable(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr("agentwire.core.portal_request", unreachable)
    monkeypatch.setattr(notify_cli, "_output_result", lambda *a, **kw: 1)

    cmd_notify_user(_ns(text=["heads", "up"], session="ci", priority="normal", json=False))

    assert [e["event"] for e in fleet_activity.recent()] == ["toast"]


def test_different_high_toasts_are_not_throttled_into_silence(isolate, portal, monkeypatch):
    """The cooldown groups by CONTENT, not by sender. Keying it on the session
    made 'build is red' swallow 'deploy rolled back' a minute later — and on the
    one surface whose caller declared the message urgent, a false-reject is
    silence with no screen behind it."""
    from agentwire import notify_cli
    from agentwire.notify_cli import cmd_notify_user

    _subscribe()
    monkeypatch.setattr(notify_cli, "_output_result", lambda *a, **kw: 0)

    cmd_notify_user(_ns(text=["build", "is", "red"], session="ci", priority="high", json=False))
    cmd_notify_user(_ns(text=["deploy", "rolled", "back"], session="ci",
                        priority="high", json=False))
    # …and the case the throttle IS for: the same toast, again.
    cmd_notify_user(_ns(text=["build", "is", "red"], session="ci", priority="high", json=False))

    spoken = [m.text for m in _mail("buddy")]
    assert len(spoken) == 2
    assert any("red" in t for t in spoken) and any("rolled back" in t for t in spoken)


def test_portal_churn_is_not_recorded(isolate, monkeypatch):
    """`notify-event` fires on every glance at a terminal. Recording all of it
    would bury the events that mean something."""
    from agentwire import notify_cli
    from agentwire.notify_cli import cmd_notify

    monkeypatch.setattr(notify_cli, "_get_portal_url", lambda: "")
    for event in ("client_attached", "pane_focused", "window_activity"):
        cmd_notify(_ns(event=event, session="s", pane=None, pane_id=None,
                       old_name=None, new_name=None, json=True))
    assert fleet_activity.recent() == []

    cmd_notify(_ns(event="session_closed", session="s", pane=None, pane_id=None,
                   old_name=None, new_name=None, json=True))
    assert [e["event"] for e in fleet_activity.recent()] == ["session_closed"]


def test_a_finished_scheduled_run_records_through_the_scheduler(isolate, monkeypatch):
    """Driven through the scheduler's own event log, which is the seam BOTH
    dispatch paths (in-place and worktree) go through exactly once per run."""
    from agentwire.scheduler import report

    _subscribe()
    monkeypatch.setattr(report, "append_event", lambda *a, **kw: None)

    report._log_event("task_completed", task="weekly-stars", session="s",
                      status="complete", duration=90, summary="7 new stars")

    entries = fleet_activity.recent()
    assert [e["event"] for e in entries] == ["task_completed"]
    assert entries[0]["task"] == "weekly-stars"
    assert [m.kind for m in _mail("buddy")] == ["done"]


def test_other_scheduler_events_are_not_activity(isolate, monkeypatch):
    from agentwire.scheduler import report

    monkeypatch.setattr(report, "append_event", lambda *a, **kw: None)
    report._log_event("task_skipped", task="t", session="s", reason="lock_conflict")
    assert fleet_activity.recent() == []


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


def test_speaking_records_the_sink_through_the_say_cli(isolate, monkeypatch):
    """The sink is part of the record, and a FAILED dispatch is never recorded
    as spoken."""
    from agentwire import channels_cli

    _subscribe()
    _say_env(channels_cli, monkeypatch, rc=0)

    channels_cli.cmd_say(_ns(text=["the", "build", "is", "green"], json=True,
                             voice=None, exaggeration=None, cfg=None, session=None,
                             display=None, backend=None, instructions=None,
                             language="English", stream=False))

    entries = fleet_activity.recent()
    assert [e["event"] for e in entries] == ["spoke"]
    assert entries[0]["sink"] == "browser"
    assert entries[0]["session"] == "worker-1"
    # And it stayed out of the spool — the owner heard it in the room.
    assert _mail("buddy") == []


def test_a_failed_say_is_not_recorded_as_spoken(isolate, monkeypatch):
    from agentwire import channels_cli

    _say_env(channels_cli, monkeypatch, rc=1)

    channels_cli.cmd_say(_ns(text=["nobody", "heard", "this"], json=True,
                             voice=None, exaggeration=None, cfg=None, session=None,
                             display=None, backend=None, instructions=None,
                             language="English", stream=False))

    assert fleet_activity.recent() == []


def test_a_partly_spoken_say_records_exactly_what_played(isolate, monkeypatch):
    """The local path CHUNKS. A failure on chunk 3 of 4 still played 1 and 2
    out loud — recording the whole string claims the owner heard a sentence
    that never played, and recording nothing lets the buddy later offer, as
    news, something they already heard."""
    from agentwire import channels_cli

    _say_env(channels_cli, monkeypatch, browser=False)
    monkeypatch.setattr(channels_cli, "chunk_text", None, raising=False)
    monkeypatch.setattr("agentwire.utils.chunker.chunk_text",
                        lambda t: ["first part.", "second part.", "third part."])
    played = []

    def dispatch(chunk, *a, **kw):
        if len(played) == 2:
            return 1, "os-voice"
        played.append(chunk)
        return 0, "os-voice"

    monkeypatch.setattr(channels_cli, "_local_say_dispatch", dispatch)

    channels_cli.cmd_say(_ns(text=["first part. second part. third part."], json=True,
                             voice=None, exaggeration=None, cfg=None, session=None,
                             display=None, backend=None, instructions=None,
                             language="English", stream=False))

    entries = fleet_activity.recent()
    assert [e["event"] for e in entries] == ["spoke"]
    assert entries[0]["text"] == "first part. second part."
    assert "third part" not in entries[0]["text"]
