"""Reviewer canaries for PR #1110 (round 2). Each asserts CORRECT behavior; FAIL = defect."""
import pytest

from agentwire import background_tasks as bt

R = "─" * 40
BOX = f"{R}\n❯ \n{R}\n"


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "aw")


def _safe(reading):
    # never "ok with 0" when the truth is a live task
    return not (reading.status == "ok" and reading.count == 0)


# K: footer truncated right after the digit, no turn-end line (interrupted / duration off)
@pytest.mark.parametrize("tail", ["⏵⏵ bypass permissions on · 1", "⏵⏵ bypass permissions on · 2 ",
                                  "⏵⏵ bypass permissions on · 1 shell, 1"])
def test_k1_footer_cut_after_digit_reads_zero(tail):
    text = "⏺ waiting\n" + BOX + "  haiku  main\n" + tail
    assert _safe(bt.parse_pane(text)), bt.parse_pane(text)


# K: pending task, multi-line draft in the input box (box not found) -> unreadable -> gate proceeds
def test_k2_taller_input_box_with_pending_task_is_unreadable_not_pending():
    text = (f"✻ Worked for 3s · done 7:37 PM · 1 shell still running\n{R}\n❯ line one\n  line two\n{R}\n"
            "  ⏵⏵ bypass permissions on · 1 shell")
    r = bt.parse_pane(text)
    assert r.status != "unreadable", r  # unreadable + no transcript ids => proceed (kill path)


# K: dialog over the box (plan-approval menu) with a pending agent task (no transcript shape)
def test_k3_dialog_over_box_is_unreadable():
    text = "✻ Worked for 3s · done 7:37 PM · 1 local agent still running\n Do you want to proceed?\n ❯ 1. Yes\n   2. No"
    assert bt.parse_pane(text).status != "unreadable"


# L: any numeric footer segment is counted as a task (finished session held)
@pytest.mark.parametrize("seg", ["2 MCP servers failed", "3 files changed", "1 update available"])
def test_l1_non_task_footer_segment_is_not_a_task(seg):
    text = ("✻ Brewed for 1s · done 7:41 PM\n" + BOX + f"  haiku  main\n  ⏵⏵ bypass permissions on · {seg}")
    r = bt.parse_pane(text)
    assert r.count == 0, r


# L: a quoted turn-end line as the last tool output of a turn with NO real turn-end line
def test_l2_quoted_turn_line_directly_above_box_no_real_turn_end():
    text = ("⏺ Bash(cat fixture)\n  ⎿  ⏺ waiting.\n     ✻ Worked for 3s · done 7:37 PM · 1 shell still running\n" + BOX
            + "  ⏵⏵ bypass permissions on (shift+tab to cycle)")
    assert bt.parse_pane(text).count in (0, None)


# state: concurrent writers share one tmp name -> replace() FileNotFoundError -> gate raises -> hook proceeds
def test_s1_marker_tmp_name_is_unique_per_writer(tmp_path):
    import inspect
    src = inspect.getsource(bt._write_marker)
    assert "pid" in src or "mkstemp" in src or "uuid" in src, "shared '.tmp' name races"


# state: tick without watchdog never refreshes last_seen; hook-only holds restart the clock after 10m
def test_s2_hook_only_hold_reaches_the_bound_without_watchdog():
    p = bt.Pending(1, "pane", None, 1, [])
    t0 = 1_000_000.0
    bt.gate("s", 0, pending=p, now=t0)
    # task wakes the session every 12 minutes (monitor events) for an hour; no watchdog ticks
    acts = [bt.gate("s", 0, pending=p, now=t0 + 720 * i)["action"] for i in range(1, 6)]
    assert "qualify" in acts, acts


# gate crash on a reap path must not mean kill: cmd_idle_gate returns proceed on exception
def test_s3_gate_exception_on_worker_pane_is_not_proceed(monkeypatch):
    import argparse
    import contextlib
    import io
    import json

    from agentwire import notify_cli, pane_manager
    monkeypatch.setattr(pane_manager, "get_current_session", lambda: "s")
    monkeypatch.setattr(pane_manager, "get_current_pane_index", lambda: 1)
    monkeypatch.setattr(bt, "gate", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("tmp")))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        notify_cli.cmd_idle_gate(argparse.Namespace(conversation_id="", cwd="", json=True))
    assert json.loads(buf.getvalue())["action"] != "proceed"
