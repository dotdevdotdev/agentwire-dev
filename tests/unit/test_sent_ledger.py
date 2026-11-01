"""Sender-side delivery status — ``agentwire msg sent`` / ``msg_sent`` (#1231).

The ledger outlives the inbox file, so every test here asserts the state AFTER
the file is gone — the thing the drain used to make unanswerable.
"""

import json
import time
from types import SimpleNamespace

import pytest

from agentwire import inbox, msg_cli, prompt_router, sent_ledger, session_ready


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    # Never email the owner from a test.
    monkeypatch.setattr(inbox, "_escalate_dead_letters", lambda *a, **k: None)
    return tmp_path


def _patch_drain(monkeypatch, *, empty=True, deliver=(True, "delivered"), scrollback=""):
    from agentwire import usage_limit

    monkeypatch.setattr(usage_limit, "_capture", lambda s, **kw: "screen")
    monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "screen")
    monkeypatch.setattr(
        prompt_router, "input_box_content_sgr", lambda vis: "" if empty else "a draft"
    )
    monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
    monkeypatch.setattr(prompt_router, "safe_deliver", lambda s, p, text: deliver)
    monkeypatch.setattr(session_ready, "scrollback", lambda s, p=0: scrollback)


def _send(to="recv", text="hello", kind="note", sender="me"):
    return inbox.enqueue(to, text, kind=kind, sender=sender)[0]


def _only(sender="me", **kw):
    rows = sent_ledger.list_sent(sender, **kw)["sent"]
    assert len(rows) == 1, rows
    return rows[0]


class TestLifecycle:
    def test_queued_then_delivered_survives_the_file_being_unlinked(self, isolate, monkeypatch):
        m = _send()
        assert _only()["state"] == "queued"

        _patch_drain(monkeypatch)
        assert inbox.flush_session("recv")["delivered"] == 1
        assert not m.path.exists()  # the drain unlinked it ...
        row = _only()
        assert row["state"] == "delivered"  # ... and the sender can still tell
        assert row["how"] == "pasted"

    def test_queued_carries_the_live_deferral_reason_and_attempts(self, isolate, monkeypatch):
        _send()
        _patch_drain(monkeypatch, empty=False)
        inbox.flush_session("recv")
        row = _only()
        assert row["state"] == "queued"
        assert row["reason"] == "box_not_empty" and row["attempts"] == 1

    def test_dedup_delivery_is_recorded(self, isolate, monkeypatch):
        m = _send()
        rule = "─" * 60
        _patch_drain(
            monkeypatch, scrollback=f"❯ {m.render()}\n{rule}\n❯\n{rule}\n  status line\n"
        )
        inbox.flush_session("recv")
        row = _only()
        assert row["state"] == "delivered" and row["how"] == "scrollback"

    def test_late_delivery_names_its_evidence(self, isolate):
        m = _send()
        m.path.unlink()
        inbox._note_delivered("recv", [m], how="late")
        row = _only()
        assert row["state"] == "delivered" and row["how"] == "late" and "verified" not in row
        assert "late" in sent_ledger.describe(row)

    def test_dead_letter_records_reason_and_time(self, isolate, monkeypatch):
        _send()
        _patch_drain(monkeypatch)
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"other"})
        for _ in range(inbox.GONE_MAX_ATTEMPTS):
            inbox.flush_session("recv")
        assert inbox.purge_dead() == 1  # the corpse is gone; the verdict is not
        row = _only()
        assert row["state"] == "dead_lettered"
        assert row["reason"] == "target_gone" and row["state_ts"] > 0

    def test_purge_pending_is_distinguishable_from_delivery(self, isolate):
        _send()
        inbox.purge_pending("recv")
        row = _only()
        assert row["state"] == "purged" and row["reason"] == "msg purge"

    def test_gc_sender_dead_letters_load_bearing_and_purges_the_rest(self, isolate):
        _send(kind="done", text="report")
        _send(kind="note", text="chatter")
        inbox.gc_sender("me")
        inbox.purge_dead()
        by_kind = {r["kind"]: r for r in sent_ledger.list_sent("me")["sent"]}
        assert by_kind["done"]["state"] == "dead_lettered"
        assert by_kind["done"]["reason"] == "sender_exited"
        assert by_kind["note"]["state"] == "purged"

    def test_passive_message_waits_then_is_pulled(self, isolate):
        _send(kind="ingest", text="pointer")
        row = _only()
        assert row["state"] == "queued" and "msg pull" in row["reason"]
        inbox.pull_ingest("recv")
        assert _only()["state"] == "pulled"

    def test_a_terminal_state_is_never_rewritten(self, isolate, monkeypatch):
        m = _send()
        _patch_drain(monkeypatch)
        inbox.flush_session("recv")
        sent_ledger.record_purged([m], "late sweep")
        assert _only()["state"] == "delivered"


class TestReconciliation:
    def test_a_missed_transition_reads_unknown_with_the_reason(self, isolate):
        m = _send()
        m.path.unlink()  # gone, and nothing told the ledger
        row = _only()
        assert row["state"] == "unknown" and "no recorded outcome" in row["reason"]

    def test_a_corpse_heals_a_missed_dead_letter_write(self, isolate):
        m = _send()
        m.dead_ts, m.reason = 5, "box_not_empty"
        inbox._write_message(inbox.dead_dir("recv") / m.path.name, m)
        m.path.unlink()
        row = _only()
        assert row["state"] == "dead_lettered" and row["reason"] == "box_not_empty"

    def test_a_ledger_fault_never_breaks_the_drain(self, isolate, monkeypatch):
        m = _send()
        _patch_drain(monkeypatch)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(sent_ledger, "record_delivered", boom)
        assert inbox.flush_session("recv")["delivered"] == 1
        assert not m.path.exists()


class TestScopeAndBounds:
    def test_a_session_sees_only_its_own_sends_and_filters_work(self, isolate):
        a = _send(to="r1", sender="me")
        _send(to="r2", sender="me")
        _send(to="r1", sender="someone-else")
        assert sent_ledger.list_sent("me")["total"] == 2
        assert [r["id"] for r in sent_ledger.list_sent("me", to="r1")["sent"]] == [a.id]
        tail = a.id.rsplit("-", 1)[-1]
        assert sent_ledger.list_sent("me", message_id=tail)["sent"][0]["id"] == a.id
        assert sent_ledger.list_sent("nobody")["sent"] == []

    def test_newest_first_and_limit(self, isolate):
        ids = [_send(text=f"m{i}").id for i in range(3)]
        got = [r["id"] for r in sent_ledger.list_sent("me", limit=2)["sent"]]
        assert got == ids[::-1][:2]

    def test_worktree_style_sender_names_get_a_flat_distinct_file(self, isolate):
        _send(sender="proj/branch")
        _send(sender="proj_branch")
        assert sent_ledger.list_sent("proj/branch")["total"] == 1
        assert sent_ledger.list_sent("proj_branch")["total"] == 1

    def test_unknown_sender_gets_no_ledger(self, isolate):
        inbox.enqueue("recv", "x", sender="unknown")
        assert not sent_ledger.ledger_dir().exists()

    def test_ttl_and_cap_bound_the_file(self, isolate, monkeypatch):
        monkeypatch.setattr(sent_ledger, "MAX_ENTRIES", 3)
        old = _send(text="old")
        for i in range(4):
            _send(text=f"n{i}")
        assert sent_ledger.list_sent("me", limit=100)["total"] == 3
        monkeypatch.setattr(sent_ledger, "MAX_ENTRIES", 500)
        path = sent_ledger._path("me")
        data = json.loads(path.read_text())
        victim = next(iter(data))
        data[victim]["state_ts"] = int(time.time() * 1000) - sent_ledger.TTL_MS - 1
        path.write_text(json.dumps(data))
        _send(text="trigger prune")
        assert victim not in json.loads(path.read_text())
        assert old.id not in json.loads(path.read_text())


class TestCli:
    def _ns(self, **kw):
        base = dict(json=False, limit=20, to=None, message_id=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_outside_a_session_it_refuses_rather_than_guessing(self, isolate, monkeypatch, capsys):
        monkeypatch.setattr(msg_cli, "_current_session", lambda: None)
        assert msg_cli.cmd_msg_sent(self._ns(json=True)) == 1
        assert json.loads(capsys.readouterr().out)["success"] is False

    def test_json_has_the_full_body_table_truncates_and_never_overclaims(
        self, isolate, monkeypatch, capsys
    ):
        long = "word " * 60
        _send(text=long)
        monkeypatch.setattr(msg_cli, "_current_session", lambda: "me")
        msg_cli.cmd_msg_sent(self._ns(json=True))
        assert json.loads(capsys.readouterr().out)["sent"][0]["text"] == long
        msg_cli.cmd_msg_sent(self._ns())
        out = capsys.readouterr().out
        assert long not in out and "…" in out
        assert "not read or acted on" in out
