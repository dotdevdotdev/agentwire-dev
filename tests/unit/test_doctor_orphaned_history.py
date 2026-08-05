"""Doctor's orphaned-conversation-history check (#871 item 5).

Calibration matters more than coverage here. Measured against the real store
while building this: 466 session records, 7 with ``conversation_ids``, 28
recorded ids with no transcript anywhere — and all 28 belong to ONE record the
test suite itself polluted (#893). A check that scored every missing
transcript would therefore have reported dozens of "failures" that are either
test noise or Claude's own cache behaviour. So the tests below pin the
*silences* as hard as the reports.
"""

import types

import pytest

from agentwire import doctor_cli
from agentwire.history import encode_project_path

#: The minimum that makes a transcript a CONVERSATION rather than a metadata
#: stub. A stub is a DEAD id — measured: `claude --resume` answers "No
#: conversation found" while `--session-id` still says "already in use", so it
#: can be neither resumed nor reclaimed (see history.holds_a_conversation).
TURN = '{"type":"user","message":{"role":"user","content":"hi"}}\n'
STUB = '{"type":"ai-title"}\n{"type":"mode","mode":"normal"}\n'


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    monkeypatch.setattr("agentwire.history.PROJECTS_DIR", tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    # Default: nothing is live and nothing has moved. Each test opts in.
    monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda n: False)
    monkeypatch.setattr("agentwire.core.tmux_session_cwd", lambda n: None)
    return types.SimpleNamespace(root=tmp_path, projects=tmp_path / "projects")


def write_history(store, cwd, cid):
    d = store.projects / encode_project_path(str(cwd))
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.jsonl").write_text(TURN)


def record(session, *, cwd, ids, **extra):
    from agentwire.core import store_session_metadata

    store_session_metadata(session, {
        "cwd_at_launch": str(cwd),
        "posture": "bypass",
        "conversation_ids": list(ids),
        **extra,
    })


class TestScanOrphanedHistory:
    def test_healthy_session_is_silent(self, store):
        cwd = store.root / "wt"
        write_history(store, cwd, "cid")
        record("sess", cwd=cwd, ids=["cid"])
        assert doctor_cli.scan_orphaned_history() == []

    def test_history_under_another_key_is_orphaned(self, store):
        """The literal check: recorded cwd's key != where the transcript is."""
        write_history(store, store.root / "old", "cid")
        record("sess", cwd=store.root / "new", ids=["cid"])

        [f] = doctor_cli.scan_orphaned_history()
        assert f["session"] == "sess"
        assert f["status"] == "orphaned"
        assert f["found_at"].endswith("cid.jsonl")
        assert f["expected_dir"].endswith(encode_project_path(str(store.root / "new")))
        assert f["moved"] is False  # not running, so nothing contradicts the record

    def test_moved_live_session_is_checked_against_where_it_runs(self, store, monkeypatch):
        """The case that actually happens: the record still agrees with the
        transcript, and both are stale — the session moved underneath them."""
        old, new = store.root / "old", store.root / "new"
        write_history(store, old, "cid")
        record("sess", cwd=old, ids=["cid"])
        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda n: True)
        monkeypatch.setattr("agentwire.core.tmux_session_cwd", lambda n: str(new))

        [f] = doctor_cli.scan_orphaned_history()
        assert f["status"] == "orphaned"
        assert f["moved"] is True
        assert f["running_in"] == str(new)
        assert f["recorded_cwd"] == str(old)

    def test_resumable_anywhere_in_the_chain_is_silent(self, store):
        """A newest-id-never-prompted session is fine — restart walks back."""
        cwd = store.root / "wt"
        write_history(store, cwd, "older")
        record("sess", cwd=cwd, ids=["older", "never-prompted"])
        assert doctor_cli.scan_orphaned_history() == []

    def test_a_restart_does_not_silence_an_orphan(self, store):
        """The bug this survey exists for. Sequence: orphan present -> doctor
        reports it -> `restart` correctly degrades to a fresh conversation ->
        that conversation takes a turn. A scan that stops at the first
        resumable id goes quiet here, with the orphaned transcript still on
        disk — silence for exactly the user who did the natural thing."""
        cwd = store.root / "wt"
        write_history(store, store.root / "old", "stranded")   # orphaned
        write_history(store, cwd, "after-restart")             # the fresh one
        record("sess", cwd=cwd, ids=["stranded", "after-restart"])

        [f] = doctor_cli.scan_orphaned_history()
        assert f["status"] == "orphaned"
        assert f["orphaned_ids"] == ["stranded"]
        # The session itself works — this is stranded history beside it, not a
        # session that can't come back. The render says which.
        assert f["current"] is False
        assert doctor_cli._render_orphaned_history_section() == 1

    def test_every_orphaned_link_is_counted_once_per_session(self, store):
        """A session restarted twice in a moved directory strands more than
        one transcript; it is still ONE thing to fix."""
        cwd = store.root / "new"
        write_history(store, store.root / "old", "first")
        write_history(store, store.root / "old", "second")
        record("sess", cwd=cwd, ids=["first", "second"])

        [f] = doctor_cli.scan_orphaned_history()
        assert f["orphaned_ids"] == ["second", "first"]   # newest first
        assert f["current"] is True                        # nothing resumes
        assert doctor_cli._render_orphaned_history_section() == 1

    def test_dead_session_with_no_history_is_silent(self, store):
        """#893's polluted record shape — 36 fabricated ids, none live."""
        record("resumed", cwd=store.root, ids=[f"fake-{i}" for i in range(36)])
        assert doctor_cli.scan_orphaned_history() == []

    def test_live_session_with_no_history_is_reported_but_not_scored(self, store, monkeypatch):
        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda n: True)
        record("sess", cwd=store.root, ids=["cid"])

        [f] = doctor_cli.scan_orphaned_history()
        assert f["status"] == "gone"
        assert doctor_cli._render_orphaned_history_section() == 0  # stated, not counted

    def test_pre_871_records_are_skipped(self, store):
        from agentwire.core import store_session_metadata

        store_session_metadata("old-shape", {"created_by": "orch"})
        store_session_metadata("no-ids", {"cwd_at_launch": str(store.root)})
        assert doctor_cli.scan_orphaned_history() == []

    def test_explicit_session_list_bypasses_the_sweep(self, store):
        write_history(store, store.root / "old", "cid")
        record("sess", cwd=store.root / "new", ids=["cid"])
        assert doctor_cli.scan_orphaned_history(sessions=[]) == []
        assert len(doctor_cli.scan_orphaned_history(sessions=["sess"])) == 1


class TestRenderSection:
    def test_clean_store(self, store, capsys):
        assert doctor_cli._render_orphaned_history_section() == 0
        assert "[ok]" in capsys.readouterr().out

    def test_orphan_is_counted_and_explained(self, store, capsys):
        write_history(store, store.root / "old", "cid")
        record("sess", cwd=store.root / "new", ids=["cid"])

        assert doctor_cli._render_orphaned_history_section() == 1
        out = capsys.readouterr().out
        assert "[!!]" in out and "sess" in out
        # Must name both keys — the fix is moving one to the other.
        assert "runs in:" in out and "history:" in out and "expected:" in out
