"""Watchdog stage isolation (agentwire/limits_cli.py, #490).

The 60s watchdog runs five ``*.tick()`` stages sequentially. A raise in one
must be logged and skipped, not propagate out and starve the rest of the cycle.
"""

import argparse
import json

import pytest

from agentwire import limits_cli


class _Boom(RuntimeError):  # noqa: N818  # test-only stage-failure sentinel, not a public error type
    pass


@pytest.fixture
def stub_stages(monkeypatch):
    """Stub all four stage entry points with recording fakes.

    Returns a dict mapping stage name -> list that gets a sentinel appended
    when that stage actually runs, so the test can assert which stages ran.
    """
    ran: dict[str, list] = {k: [] for k in
                            ("usage_limit", "prompt_router", "inbox", "session_context",
                             "scheduler_zombie")}

    def make(name, result):
        def _tick():
            ran[name].append(True)
            return result
        return _tick

    import agentwire.inbox as inbox_mod
    import agentwire.prompt_router as pr_mod
    import agentwire.session_context as sc_mod
    from agentwire.scheduler import zombie as zombie_mod

    monkeypatch.setattr(limits_cli.usage_limit, "tick",
                        make("usage_limit",
                             {"skipped": None, "parked": [], "resumed": [], "waiting": []}))
    monkeypatch.setattr(pr_mod, "tick", make("prompt_router", {"routed": [], "deferred": []}))
    monkeypatch.setattr(inbox_mod, "tick", make("inbox", {"flushed": [], "deferred": []}))
    monkeypatch.setattr(sc_mod, "tick", make("session_context", {"acted": [], "deferred": []}))
    monkeypatch.setattr(zombie_mod, "tick", make("scheduler_zombie", {"killed": []}))
    return ran


def test_failing_first_stage_does_not_block_the_rest(stub_stages, monkeypatch, capsys, tmp_path):
    """usage_limit raises — prompt routing, inbox drain, context all still run."""
    monkeypatch.setattr(limits_cli, "WATCHDOG_EVENTS_FILE", tmp_path / "watchdog-events.jsonl")

    def boom():
        stub_stages["usage_limit"].append(True)
        raise _Boom("usage-limit stage exploded")

    monkeypatch.setattr(limits_cli.usage_limit, "tick", boom)

    rc = limits_cli.cmd_limits_tick(argparse.Namespace(json=True))

    assert rc == 0
    # Every downstream stage ran despite the first one raising.
    assert stub_stages["prompt_router"], "prompt routing was starved"
    assert stub_stages["inbox"], "inbox drain was starved"
    assert stub_stages["session_context"], "context auto-management was starved"

    # The failure is logged, not swallowed: stderr + jsonl event.
    err = capsys.readouterr().err
    assert "usage_limit" in err and "exploded" in err

    events = (tmp_path / "watchdog-events.jsonl").read_text().strip().splitlines()
    assert len(events) == 1
    rec = json.loads(events[0])
    assert rec["event"] == "stage_failed"
    assert rec["stage"] == "usage_limit"
    assert "_Boom" in rec["error"]


def test_failing_middle_stage_isolated(stub_stages, monkeypatch, capsys, tmp_path):
    """inbox raises — earlier and later stages are unaffected."""
    monkeypatch.setattr(limits_cli, "WATCHDOG_EVENTS_FILE", tmp_path / "watchdog-events.jsonl")

    import agentwire.inbox as inbox_mod

    def boom():
        raise _Boom("inbox stage exploded")

    monkeypatch.setattr(inbox_mod, "tick", boom)

    rc = limits_cli.cmd_limits_tick(argparse.Namespace(json=False))

    assert rc == 0
    assert stub_stages["usage_limit"]
    assert stub_stages["prompt_router"]
    assert stub_stages["session_context"], "context stage starved by inbox failure"

    rec = json.loads((tmp_path / "watchdog-events.jsonl").read_text().strip())
    assert rec["stage"] == "inbox"


def test_all_stages_clean_logs_nothing(stub_stages, monkeypatch, tmp_path):
    """No failures → no watchdog event file written."""
    events_file = tmp_path / "watchdog-events.jsonl"
    monkeypatch.setattr(limits_cli, "WATCHDOG_EVENTS_FILE", events_file)

    rc = limits_cli.cmd_limits_tick(argparse.Namespace(json=True))

    assert rc == 0
    assert all(stub_stages[k] for k in stub_stages)
    assert not events_file.exists()
