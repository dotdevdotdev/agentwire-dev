"""How the #884 sweep is wired: watchdog stage, stamp throttle, doctor section.

Same safety rule as ``test_role_prompt_retention.py`` — every store here is a
fixture directory. The one test that exercises ``role_prompts.tick`` (which
DOES resolve real paths) first redirects ``core.CONFIG_DIR``, and asserts on
that redirected tree.
"""

import argparse
import json
import os
import time
import uuid

import pytest

from agentwire import core, doctor_cli, limits_cli, role_prompts
from agentwire.role_prompts import tick as real_tick  # bound before conftest's stub

DAY = 86400


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A complete fake ~/.agentwire, wired in through the one seam."""
    home = tmp_path / "agentwire-home"
    (home / "role-prompts").mkdir(parents=True, mode=0o700)
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(core, "CONFIG_DIR", home)
    return home


def _aged_prompt(home, age_days, *, cid=None):
    cid = cid or str(uuid.uuid4())
    path = home / "role-prompts" / f"{cid}.txt"
    path.write_text("you are a worker")
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return cid


def _session_record(home, name, conversation_ids):
    d = home / "sessions" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps({"conversation_ids": conversation_ids}))


class TestTick:
    """``tick`` is the ONLY function that resolves the real store — so it must
    resolve it through ``core.CONFIG_DIR``, and it must not run every minute."""

    def test_sweeps_through_the_patched_config_dir(self, fake_home, monkeypatch):
        stale = _aged_prompt(fake_home, 45)
        live = _aged_prompt(fake_home, 400)
        _session_record(fake_home, "orch", [live])

        result = real_tick()

        assert result["deleted"] == [f"{stale}.txt"]
        assert (fake_home / "role-prompts" / f"{live}.txt").exists()

    def test_a_worktree_sessions_record_is_found_through_tick(self, fake_home):
        """End to end through the production entry point, with a REAL name.

        ``tick`` is what the watchdog calls unattended, so the nested
        ``project/branch`` record — what every worktree session and scheduler
        dispatch writes — has to be reachable from here, not just from a
        hand-called ``sweep``.
        """
        live = _aged_prompt(fake_home, 200)
        stale = _aged_prompt(fake_home, 200)
        _session_record(fake_home, "documentscribe/fix-942-importer-types", [live])

        result = real_tick()

        assert result["deleted"] == [f"{stale}.txt"]
        assert (fake_home / "role-prompts" / f"{live}.txt").exists()

    def test_second_run_is_throttled_by_the_stamp(self, fake_home, monkeypatch):
        real_tick()
        assert (fake_home / "role-prompt-sweep.json").exists()

        _aged_prompt(fake_home, 45)
        assert real_tick()["skipped"] == "recent"
        assert len(list((fake_home / "role-prompts").iterdir())) == 1

    def test_an_expired_stamp_lets_the_sweep_run_again(self, fake_home, monkeypatch):
        real_tick()
        stamp = fake_home / "role-prompt-sweep.json"
        old = time.time() - (role_prompts.DEFAULT_INTERVAL_HOURS + 1) * 3600
        os.utime(stamp, (old, old))

        cid = _aged_prompt(fake_home, 45)
        assert real_tick()["deleted"] == [f"{cid}.txt"]


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

    monkeypatch.setattr(limits_cli.usage_limit, "tick", lambda: {
        "skipped": None, "parked": [], "resumed": [], "waiting": []})
    monkeypatch.setattr(pr_mod, "tick", lambda: {"routed": [], "deferred": []})
    monkeypatch.setattr(inbox_mod, "tick", lambda: {"flushed": [], "deferred": []})
    monkeypatch.setattr(sc_mod, "tick", lambda: {"acted": [], "deferred": []})
    monkeypatch.setattr(zombie_mod, "tick", lambda: {"killed": []})
    monkeypatch.setattr(cohort_mod, "tick", lambda: {"reaped": [], "swept": []})


class TestWatchdogStage:
    def test_tick_runs_the_role_prompt_stage(
            self, quiet_watchdog, monkeypatch, capsys, tmp_path):
        """The GC rides the 60s watchdog, and its result reaches --json."""
        monkeypatch.setattr(
            limits_cli, "WATCHDOG_EVENTS_FILE", tmp_path / "watchdog-events.jsonl")
        monkeypatch.setattr(
            role_prompts, "tick",
            lambda: {"deleted": ["a.txt", "b.txt"], "bytes_freed": 2048})

        assert limits_cli.cmd_limits_tick(argparse.Namespace(json=True)) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["role_prompts"]["deleted"] == ["a.txt", "b.txt"]

    def test_a_raising_stage_never_starves_the_cycle(
            self, quiet_watchdog, monkeypatch, tmp_path):
        """Housekeeping is last and isolated — it can't break the watchdog."""
        events = tmp_path / "watchdog-events.jsonl"
        monkeypatch.setattr(limits_cli, "WATCHDOG_EVENTS_FILE", events)

        def boom():
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(role_prompts, "tick", boom)
        assert limits_cli.cmd_limits_tick(argparse.Namespace(json=True)) == 0
        assert json.loads(events.read_text().strip())["stage"] == "role_prompts"


class TestDoctorSection:
    def test_reports_size_and_stays_quiet_when_nothing_has_aged_out(
            self, fake_home, capsys):
        live = _aged_prompt(fake_home, 300)
        _aged_prompt(fake_home, 2)  # a live pane's prompt
        _session_record(fake_home, "orch", [live])

        found, fixed = doctor_cli._render_role_prompt_store_section()
        out = capsys.readouterr().out
        assert (found, fixed) == (0, 0)
        assert "2 file(s)" in out
        assert "1 reachable, 1 unreferenced" in out

    def test_flags_the_aged_out_tail(self, fake_home, capsys, monkeypatch):
        monkeypatch.setattr(doctor_cli, "_confirm", lambda prompt: False)
        _aged_prompt(fake_home, 45)
        found, fixed = doctor_cli._render_role_prompt_store_section()
        out = capsys.readouterr().out
        assert (found, fixed) == (1, 0)
        assert "older than 30d" in out
        assert "agentwire limits install" in out
        assert len(list((fake_home / "role-prompts").iterdir())) == 1

    def test_auto_confirm_sweeps(self, fake_home, capsys):
        cid = _aged_prompt(fake_home, 45)
        live = _aged_prompt(fake_home, 400)
        _session_record(fake_home, "orch", [live])

        found, fixed = doctor_cli._render_role_prompt_store_section(auto_confirm=True)
        assert (found, fixed) == (1, 1)
        assert not (fake_home / "role-prompts" / f"{cid}.txt").exists()
        assert (fake_home / "role-prompts" / f"{live}.txt").exists()

    def test_dry_run_reports_without_sweeping(self, fake_home):
        cid = _aged_prompt(fake_home, 45)
        found, fixed = doctor_cli._render_role_prompt_store_section(
            auto_confirm=True, dry_run=True)
        assert (found, fixed) == (1, 0)
        assert (fake_home / "role-prompts" / f"{cid}.txt").exists()

    def test_absent_store_is_not_an_issue(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path / "never-used")
        assert doctor_cli._render_role_prompt_store_section() == (0, 0)
        assert "not created yet" in capsys.readouterr().out

    def test_unrecognized_entries_are_reported_never_swept(self, fake_home, capsys):
        (fake_home / "role-prompts" / "README").write_text("hand-written note")
        _aged_prompt(fake_home, 45)
        doctor_cli._render_role_prompt_store_section(auto_confirm=True)
        assert "unrecognized" in capsys.readouterr().out
        assert (fake_home / "role-prompts" / "README").exists()
