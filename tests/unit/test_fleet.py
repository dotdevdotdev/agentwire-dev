"""`agentwire fleet` (#1277): one read-only view of an orchestrator's children.

The flags are the product — each test builds the real failure shape from the
real ledger + inbox on a tmp dir (only tmux, `gh` and the session store are
stubbed) and asserts the flag, with a control that must NOT flag.
"""

import json
from types import SimpleNamespace

import pytest

from agentwire import cohort, fleet, fleet_cli, inbox, session_cli, worktree_registry

PARENT = "orchestrator"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(cohort, "COHORT_ROOT", tmp_path / "cohorts")
    monkeypatch.setattr(cohort, "EVENTS_FILE", tmp_path / "cohort-events.jsonl")
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)

    state = SimpleNamespace(live={PARENT}, records={}, registry=[], prs={}, killed=[])

    def kill(session):
        state.killed.append(session)
        state.live.discard(session)
        return True

    monkeypatch.setattr(cohort, "is_agent_session", lambda s: s == PARENT)
    monkeypatch.setattr(cohort, "session_exists", lambda s: s in state.live)
    monkeypatch.setattr(cohort, "_kill", kill)
    monkeypatch.setattr("agentwire.core.load_session_metadata", lambda s: state.records.get(s, {}))
    monkeypatch.setattr(worktree_registry, "all_entries", lambda: state.registry)
    monkeypatch.setattr(session_cli, "pr_for_branch", lambda branch, project: state.prs.get(branch))
    return state


def _spawn(world, child, topology=cohort.WORKTREE, record=True):
    world.live.add(child)
    if record:
        world.records[child] = {"role": "worker", "roles": ["worker-worktree"]}
    assert cohort.enroll(PARENT, child, topology=topology)


def _child(data, name):
    return next(c for p in data["parents"] for c in p["children"] if c["session"] == name)


def _flags(data, name):
    return _child(data, name)["flags"]


def test_child_with_report_is_clean_and_timestamped(world):
    _spawn(world, "proj-a", topology=cohort.MAIN)
    inbox.enqueue(PARENT, "all done", kind="done", sender="proj-a")
    cohort.collect(PARENT)  # consumes the report, tears the main-topology child down

    row = _child(fleet.build_fleet(PARENT), "proj-a")
    assert row["state"] == "gone"
    assert row["ledger_state"] == cohort.REPORTED
    assert row["last_report_at"]
    assert row["flags"] == []


def test_idle_without_report_is_flagged(world):
    _spawn(world, "proj-b")
    inbox.enqueue(PARENT, "went idle", kind="idle", sender="proj-b")
    cohort.collect(PARENT)

    row = _child(fleet.build_fleet(PARENT), "proj-b")
    assert row["state"] == "idle"
    assert row["last_report_at"] is None
    assert fleet.IDLE_NO_REPORT in row["flags"]


def test_report_queued_to_dead_session_is_flagged_only_when_parent_is_gone(world):
    _spawn(world, "proj-c")
    inbox.enqueue(PARENT, "finished", kind="done", sender="proj-c")

    live = _child(fleet.build_fleet(PARENT), "proj-c")
    assert live["report_queued_age_s"] is not None
    assert fleet.REPORT_QUEUED_TO_DEAD not in live["flags"]

    world.live.discard(PARENT)
    assert fleet.REPORT_QUEUED_TO_DEAD in _flags(fleet.build_fleet(PARENT), "proj-c")


def test_dead_lettered_report_is_flagged_with_its_age(world):
    _spawn(world, "proj-d")
    msg = inbox.Message(id="1-abc123", sender="proj-d", to=PARENT, kind="done",
                        text="lost", ts=1, dead_ts=2)
    dead = inbox.dead_dir(PARENT)
    dead.mkdir(parents=True)
    inbox._write_message(dead / "1-abc123.json", msg)

    row = _child(fleet.build_fleet(PARENT), "proj-d")
    assert fleet.REPORT_DEAD_LETTERED in row["flags"]
    assert row["report_dead_letter_age_s"] > 0


def test_gone_child_holding_an_open_pr_is_flagged_via_the_shared_probe(world):
    _spawn(world, "proj-e")
    world.registry = [{"session": "proj-e", "branch": "feat-e", "project": "/repo",
                       "topology": "worktree"}]
    world.prs = {"feat-e": {"state": "OPEN", "url": "https://x/pull/9"}}
    world.live.discard("proj-e")

    row = _child(fleet.build_fleet(PARENT), "proj-e")
    assert row["state"] == "gone"
    assert row["pr"] == {"state": "OPEN", "url": "https://x/pull/9"}
    assert fleet.GONE_WITH_OPEN_PR in row["flags"]

    world.prs["feat-e"] = {"state": "MERGED", "url": "https://x/pull/9"}
    assert fleet.GONE_WITH_OPEN_PR not in _flags(fleet.build_fleet(PARENT), "proj-e")


def test_open_pr_on_a_live_child_is_not_a_gone_flag(world):
    _spawn(world, "proj-f")
    world.registry = [{"session": "proj-f", "branch": "feat-f", "project": "/repo",
                       "topology": "worktree"}]
    world.prs = {"feat-f": {"state": "OPEN", "url": "u"}}
    assert _flags(fleet.build_fleet(PARENT), "proj-f") == []


def test_no_pr_lookup_when_disabled(world):
    _spawn(world, "proj-g")
    world.registry = [{"session": "proj-g", "branch": "feat-g", "project": "/repo",
                       "topology": "worktree"}]
    world.prs = {"feat-g": {"state": "OPEN", "url": "u"}}
    assert _child(fleet.build_fleet(PARENT, with_pr=False), "proj-g")["pr"] is None


def test_zero_children_and_zero_ledgers_are_valid(world):
    assert fleet.build_fleet() == {"success": True, "parents": [], "flagged": [],
                                   "broken_ledgers": []}
    missing = fleet.build_fleet(PARENT)["parents"][0]
    assert (missing["ledger"], missing["children"]) == ("missing", [])


def test_corrupt_ledger_and_missing_session_record_are_reported_not_raised(world):
    _spawn(world, "proj-h", record=False)  # live child, no launch record
    cohort.ledger_path("broken").write_text("{not json")

    data = fleet.build_fleet()
    assert [b["parent"] for b in data["broken_ledgers"]] == ["broken"]
    assert data["broken_ledgers"][0]["ledger"].startswith("corrupt")
    row = _child(data, "proj-h")
    assert row["session_record"] is False
    assert fleet.NO_SESSION_RECORD in row["flags"]


def test_building_the_fleet_changes_nothing_on_disk(world, tmp_path):
    _spawn(world, "proj-i")
    inbox.enqueue(PARENT, "x", kind="done", sender="proj-i")
    inbox.enqueue("proj-i", "ping", kind="note", sender=PARENT)

    def snapshot():
        return {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    before = snapshot()
    fleet.build_fleet()
    assert snapshot() == before


def test_cli_json_is_build_fleet_verbatim(world, capsys):
    _spawn(world, "proj-j")
    args = SimpleNamespace(all=True, session=None, no_pr=False, json=True)
    assert fleet_cli.cmd_fleet(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["parents"][0]["children"][0]["session"] == "proj-j"
    assert out["parents"][0]["children"][0]["role"] == "worker"


def test_text_render_names_the_flag(world, capsys):
    _spawn(world, "proj-k")
    inbox.enqueue(PARENT, "x", kind="idle", sender="proj-k")
    cohort.collect(PARENT)
    fleet_cli.cmd_fleet(SimpleNamespace(all=True, session=None, no_pr=True, json=False))
    assert fleet.IDLE_NO_REPORT in capsys.readouterr().out
