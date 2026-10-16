"""Per-stage watchdog switches, doctor/msg liveness warnings (#1088)."""

import argparse
from types import SimpleNamespace

from agentwire import doctor_cli, inbox, limits_cli, msg_cli
from agentwire.config import _dict_to_config


def _stub_all_stages(monkeypatch):
    import agentwire.background_tasks as bt
    import agentwire.cohort as cohort
    import agentwire.inbox as inbox_mod
    import agentwire.prompt_router as pr
    import agentwire.role_prompts as rp
    import agentwire.safety_notify as sn
    import agentwire.session_context as sc
    from agentwire.scheduler import zombie

    order: list[str] = []

    def make(name, result):
        def _tick():
            order.append(name)
            return result
        return _tick

    monkeypatch.setattr(limits_cli.usage_limit, "tick", make(
        "usage_limit", {"skipped": None, "parked": [], "resumed": [], "waiting": []}))
    monkeypatch.setattr(pr, "tick", make("prompt_router", {"routed": [], "deferred": []}))
    monkeypatch.setattr(inbox_mod, "tick", make("inbox", {"flushed": [], "deferred": []}))
    monkeypatch.setattr(sc, "tick", make("session_context", {"acted": [], "deferred": []}))
    monkeypatch.setattr(zombie, "tick", make("scheduler_zombie", {"killed": []}))
    monkeypatch.setattr(cohort, "tick", make("cohort", {"reaped": [], "swept": []}))
    monkeypatch.setattr(bt, "tick", make("idle_deferred", {"qualified": [], "cleared": []}))
    monkeypatch.setattr(sn, "tick", make("safety_notify", {"emailed": False, "pending": 0}))
    monkeypatch.setattr(rp, "tick", make("role_prompts", {"deleted": []}))
    return order


def test_tick_runs_every_stage_in_declared_order_and_skips_disabled(monkeypatch):
    order = _stub_all_stages(monkeypatch)
    monkeypatch.setattr(limits_cli, "disabled_stages", lambda: [])
    limits_cli.cmd_limits_tick(argparse.Namespace(json=True))
    assert order == list(limits_cli.STAGE_NAMES)

    order.clear()
    monkeypatch.setattr(limits_cli, "disabled_stages", lambda: ["cohort", "inbox"])
    limits_cli.cmd_limits_tick(argparse.Namespace(json=True))
    # Disabled stages vanish; survivors keep their relative (invariant) order.
    assert order == [n for n in limits_cli.STAGE_NAMES if n not in ("cohort", "inbox")]


def test_config_only_explicit_false_disables_a_stage(monkeypatch):
    cfg = _dict_to_config({"watchdog": {"stages": {"cohort": False, "inbox": True}}})
    monkeypatch.setattr("agentwire.config.get_config", lambda: cfg)
    assert limits_cli.disabled_stages() == ["cohort"]
    assert _dict_to_config({"watchdog": "garbage"}).watchdog.stages == {}


def test_doctor_names_what_is_offline_when_watchdog_unloaded(monkeypatch, capsys):
    monkeypatch.setattr(limits_cli, "watchdog_loaded", lambda: False)
    monkeypatch.setattr(limits_cli, "disabled_stages", lambda: [])
    monkeypatch.setattr(limits_cli, "unknown_stage_keys", lambda: [])
    issues = doctor_cli._render_watchdog_section()
    out = capsys.readouterr().out
    assert issues == 1
    assert "message delivery" in out and "prompt routing" in out


def test_doctor_reports_disabled_stage_and_loaded_clean(monkeypatch, capsys):
    monkeypatch.setattr(limits_cli, "watchdog_loaded", lambda: True)
    monkeypatch.setattr(limits_cli, "disabled_stages", lambda: ["cohort"])
    monkeypatch.setattr(limits_cli, "unknown_stage_keys", lambda: [])
    assert doctor_cli._render_watchdog_section() == 0
    assert "cohort" in capsys.readouterr().out


def _send(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "ev.jsonl")
    monkeypatch.setattr(msg_cli, "_current_session", lambda: None)
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    ns = SimpleNamespace(json=True, to="somebody", kind="note", from_session="o",
                         text=["hi"], body_file=None, ref="")
    assert msg_cli.cmd_msg_send(ns) == 0
    import json
    return json.loads(capsys.readouterr().out)["warnings"]


def test_msg_send_warns_when_no_drainer(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(limits_cli, "disabled_stages", lambda: [])
    monkeypatch.setattr(limits_cli, "watchdog_loaded", lambda: False)
    assert any("not loaded" in w for w in _send(monkeypatch, tmp_path, capsys))

    monkeypatch.setattr(limits_cli, "watchdog_loaded", lambda: True)
    assert _send(monkeypatch, tmp_path, capsys) == []

    monkeypatch.setattr(limits_cli, "disabled_stages", lambda: ["inbox"])
    assert any("inbox stage is disabled" in w for w in _send(monkeypatch, tmp_path, capsys))


def test_unit_files_and_help_list_every_stage():
    missing = [
        (where, desc)
        for where, text in (
            ("plist", limits_cli._plist_content("/x/agentwire")),
            ("systemd", limits_cli._systemd_service_content("/x/agentwire")),
        )
        for _, desc, _ in limits_cli.WATCHDOG_STAGES
        if desc not in text
    ]
    assert not missing, missing


def test_only_real_bools_switch_a_stage_off(monkeypatch, capsys):
    raw = {"cohort": None, "inbox": 0, "prompt_router": "false",
           "session_context": "off", "role_prompts": False, "safety_notify": True}
    cfg = _dict_to_config({"watchdog": {"stages": raw}})
    monkeypatch.setattr("agentwire.config.get_config", lambda: cfg)
    # Only the genuine `false` disables; None is absent, the rest are ignored.
    assert limits_cli.disabled_stages() == ["role_prompts"]
    assert limits_cli.invalid_stage_values() == {
        "inbox": 0, "prompt_router": "false", "session_context": "off"}

    monkeypatch.setattr(limits_cli, "watchdog_loaded", lambda: True)
    doctor_cli._render_watchdog_section()
    out = capsys.readouterr().out
    assert all(k in out for k in ("inbox", "prompt_router", "session_context"))
    assert "cohort" not in out.split("IGNORED")[1].split("\n")[0]


def test_doctor_flags_disabled_usage_limit_with_parked_sessions(monkeypatch, capsys):
    from agentwire import usage_limit

    monkeypatch.setattr(limits_cli, "watchdog_loaded", lambda: True)
    monkeypatch.setattr(limits_cli, "disabled_stages", lambda: ["usage_limit"])
    monkeypatch.setattr(limits_cli, "invalid_stage_values", lambda: {})
    monkeypatch.setattr(limits_cli, "unknown_stage_keys", lambda: [])
    monkeypatch.setattr(usage_limit, "list_parked", lambda: [])
    assert doctor_cli._render_watchdog_section() == 0
    monkeypatch.setattr(usage_limit, "list_parked", lambda: [{"session": "proj-x"}])
    assert doctor_cli._render_watchdog_section() == 1
    assert "proj-x" in capsys.readouterr().out
