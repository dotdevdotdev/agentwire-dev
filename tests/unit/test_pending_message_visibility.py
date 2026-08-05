"""Long-pending report-backs are surfaced by doctor (#879).

A penalty-free defer never dead-letters, so it never reaches doctor's
dead-letter section OR the owner email that section's existence justifies.
#872 made that gap load-bearing: ``target_parked`` legitimately defers for
hours, so a worker's ``done`` can now sit in a parked parent's queue with
nothing announcing it in either direction.

Covers ``inbox.stale_pending`` (which messages qualify) and
``doctor_cli._render_pending_messages_section`` (how they're reported).
"""

import pytest

from agentwire import doctor_cli, inbox, prompt_router

HOUR_MS = 3_600_000


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    monkeypatch.setattr(inbox, "INBOX_ROOT", root)
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    return root


def _age(session: str, hours: float, reason: str = "target_parked", **kw):
    """Enqueue a message and backdate it *hours* into the past."""
    msg = inbox.enqueue(session, kw.pop("text", "PR done"),
                        kind=kw.pop("kind", "done"), sender=kw.pop("sender", "worker"))[0]
    msg.ts = inbox._now_ms() - int(hours * HOUR_MS)
    msg.reason = reason
    inbox._write_message(msg.path, msg)
    return msg


class TestStalePending:
    def test_empty_inbox_is_quiet(self, isolate):
        assert inbox.stale_pending() == []

    def test_fresh_message_is_not_stale(self, isolate):
        _age("s", hours=0.1)
        assert inbox.stale_pending() == []

    def test_message_older_than_threshold_is_reported(self, isolate):
        _age("s", hours=5)
        stale = inbox.stale_pending()
        assert len(stale) == 1
        session, msg = stale[0]
        assert session == "s"
        assert msg.kind == "done" and msg.reason == "target_parked"

    def test_only_load_bearing_kinds_qualify(self, isolate):
        """Same rule as the dead-letter email: a lost `note` is fire-and-forget
        and `ingest` is pull-only BY DESIGN, so reporting either is pure noise."""
        _age("s", hours=5, kind="note")
        _age("s", hours=5, kind="ingest")
        assert inbox.stale_pending() == []
        for kind in inbox.ESCALATE_KINDS:
            _age(f"s-{kind}", hours=5, kind=kind)
        assert {k for _, m in inbox.stale_pending() for k in [m.kind]} == set(
            inbox.ESCALATE_KINDS
        )

    def test_threshold_is_the_boundary(self, isolate):
        _age("under", hours=inbox.STALE_PENDING_MS / HOUR_MS - 0.5)
        _age("over", hours=inbox.STALE_PENDING_MS / HOUR_MS + 0.5)
        assert [s for s, _ in inbox.stale_pending()] == ["over"]

    def test_custom_threshold_is_honored(self, isolate):
        _age("s", hours=1)
        assert inbox.stale_pending() == []                      # default 2h
        assert len(inbox.stale_pending(older_than_ms=30 * 60_000)) == 1

    def test_worktree_recipients_resolve_their_nested_name(self, isolate):
        """Worktree session names contain `/` and nest a directory level — the
        recipient must come back whole, or the `msg inbox -s` hint is useless."""
        _age("proj/feature-x", hours=5)
        assert [s for s, _ in inbox.stale_pending()] == ["proj/feature-x"]

    def test_oldest_first(self, isolate):
        _age("newer", hours=3)
        _age("older", hours=9)
        assert [s for s, _ in inbox.stale_pending()] == ["older", "newer"]

    def test_dead_lettered_messages_are_not_counted(self, isolate, monkeypatch):
        """Those belong to the dead-letter section; double-reporting one
        situation in two places is how a doctor run stops being read."""
        msgs = inbox.enqueue("s", "burned out", kind="done", sender="worker")
        msgs[0].ts = inbox._now_ms() - 9 * HOUR_MS
        inbox._write_message(msgs[0].path, msgs[0])
        monkeypatch.setattr(inbox, "live_sessions", lambda: None)
        monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "x")
        monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda v: None)
        # Drive it to dead-letter with a PENALIZED reason.
        monkeypatch.setattr(inbox, "_NO_PENALTY_REASONS", frozenset())
        for _ in range(inbox.MAX_ATTEMPTS):
            inbox.flush_session("s")
        assert inbox.list_dead("s")          # it really did dead-letter
        assert inbox.stale_pending() == []   # and is not re-reported here

    def test_unreadable_inbox_yields_nothing_rather_than_raising(self, isolate, monkeypatch):
        """doctor must never fail because one section's data is broken."""
        monkeypatch.setattr(
            inbox, "_iter_pending_sessions",
            lambda: (_ for _ in ()).throw(OSError("permission denied")),
        )
        assert inbox.stale_pending() == []


class TestDoctorSection:
    def test_quiet_when_nothing_pending(self, isolate, capsys):
        assert doctor_cli._render_pending_messages_section() == 0
        assert "[ok]" in capsys.readouterr().out

    def test_reports_a_stale_report_back(self, isolate, capsys):
        _age("orchestrator", hours=6, sender="worker-a")
        rc = doctor_cli._render_pending_messages_section()
        out = capsys.readouterr().out
        assert rc == 1
        assert "[!!]" in out
        assert "orchestrator" in out and "worker-a" in out
        assert "6.0h" in out                       # how long it has waited
        assert "target_parked" in out              # why it hasn't landed
        assert "msg inbox -s" in out               # what to do about it

    def test_parked_recipients_get_the_it_resolves_itself_hint(self, isolate, capsys):
        _age("parked-parent", hours=6, reason="target_parked")
        doctor_cli._render_pending_messages_section()
        out = capsys.readouterr().out
        assert "limits status" in out
        assert "FYI, not a failure" in out

    def test_non_parked_stall_omits_the_parked_hint(self, isolate, capsys):
        _age("busy-parent", hours=6, reason="target_busy")
        doctor_cli._render_pending_messages_section()
        out = capsys.readouterr().out
        assert "target_busy" in out
        assert "limits status" not in out

    def test_counts_one_issue_for_a_whole_stranded_cohort(self, isolate, capsys):
        """A parked parent strands every child at once — that's ONE situation,
        and inflating issues_found by the cohort size misrepresents severity."""
        for i in range(4):
            _age("parent", hours=6, sender=f"worker-{i}")
        rc = doctor_cli._render_pending_messages_section()
        assert rc == 1
        assert capsys.readouterr().out.count("worker-") == 4  # all still listed
