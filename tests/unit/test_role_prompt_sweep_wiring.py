"""How the #884 sweep is wired: watchdog stage, stamp throttle, doctor section.

Same safety rule as ``test_role_prompt_retention.py`` — every store here is a
fixture directory. The one test that exercises ``role_prompts.tick`` (which
DOES resolve real paths) first redirects ``core.CONFIG_DIR``, and asserts on
that redirected tree.
"""

import argparse
import json

import pytest

from agentwire import limits_cli, role_prompts


@pytest.fixture
def quiet_watchdog(monkeypatch):
    """Stub every OTHER watchdog stage.

    Without this, `cmd_limits_tick` runs prompt routing and the inbox drain
    against the live tmux server of whatever machine the suite is on — slow,
    and capable of actually delivering a message mid-test.
    """
    import agentwire.cohort as cohort_mod
    import agentwire.inbox as inbox_mod
    import agentwire.prompt_router as pr_mod
    import agentwire.session_context as sc_mod
    from agentwire.scheduler import zombie as zombie_mod

    monkeypatch.setattr(
        limits_cli.usage_limit,
        "tick",
        lambda: {"skipped": None, "parked": [], "resumed": [], "waiting": []},
    )
    monkeypatch.setattr(pr_mod, "tick", lambda: {"routed": [], "deferred": []})
    monkeypatch.setattr(inbox_mod, "tick", lambda: {"flushed": [], "deferred": []})
    monkeypatch.setattr(sc_mod, "tick", lambda: {"acted": [], "deferred": []})
    monkeypatch.setattr(zombie_mod, "tick", lambda: {"killed": []})
    monkeypatch.setattr(cohort_mod, "tick", lambda: {"reaped": [], "swept": []})


class TestWatchdogStage:
    def test_a_raising_stage_never_starves_the_cycle(self, quiet_watchdog, monkeypatch, tmp_path):
        """Housekeeping is last and isolated — it can't break the watchdog."""
        events = tmp_path / "watchdog-events.jsonl"
        monkeypatch.setattr(limits_cli, "WATCHDOG_EVENTS_FILE", events)

        def boom():
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(role_prompts, "tick", boom)
        assert limits_cli.cmd_limits_tick(argparse.Namespace(json=True)) == 0
        assert json.loads(events.read_text().strip())["stage"] == "role_prompts"
