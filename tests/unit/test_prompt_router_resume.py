"""The resume dialog: detection, routing, root escalation, doctor (#905).

THE FIXTURE IS THE POINT OF THIS FILE. ``RESUME_DIALOG`` below is real
``tmux capture-pane -p`` output from a pane genuinely sitting at this dialog,
reproduced on 2026-08-05 by resuming a cloned 134k-token conversation with the
gating feature flag enabled in an isolated ``HOME``. The option labels and the
body sentence were cross-checked against the string literals in the shipped
Claude Code binary (2.1.222), so they are the product's text and not a
transcription of a screenshot.

That matters because #905 shipped past every existing test: the sweep had
detectors for three dialog shapes and none for this one, and no fixture in the
suite contained a fourth shape to notice with. The lesson of #901/#898/#902 —
the fixture decides what the suite can see — applies to the thing being
detected, not just the thing being asserted.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agentwire import prompt_router
from agentwire.prompt_router import PromptInfo, detect_prompt

# =============================================================================
# Fixtures — real captures, verbatim
# =============================================================================

# Real capture. The trailing blank lines are tmux padding the pane height and
# are kept: the liveness check anchors on the END of the screen, so stripping
# them would test a screen that never occurs.
RESUME_DIALOG = """\
⏺ Read 2 files, ran 4 shell commands

────────────────────────────────────────────────────────────────────────────────
  This session is 2m old and 134.1k tokens.

  Resuming the full session will consume a substantial portion of your usage limits. We recommend resuming from a summary.

  ❯ 1. Resume from summary (recommended)
    2. Resume full session as-is
    3. Don't ask me again

  Enter to confirm · Esc to cancel

"""

# The same dialog after four hours on a big conversation — the two numbers in
# the title are the ONLY difference, and they are formatted at render time.
RESUME_DIALOG_OLDER = RESUME_DIALOG.replace(
    "This session is 2m old and 134.1k tokens.",
    "This session is 2h 47m old and 233.6k tokens.",
)

# A pane DISPLAYING the dialog text rather than showing it: a session that was
# told about the incident, or one reading this very issue. It has its own input
# box and status bar below, so the screen does not end at the dialog footer.
# This is not hypothetical — the pane writing this test had the dialog quoted
# on screen from the task description.
QUOTED_RESUME_DIALOG = RESUME_DIALOG.rstrip() + """

  Enter to confirm · Esc to cancel  <- quoted from the issue, not live

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ~/worktrees/agentwire-dev/fix-905                  claude-opus-5  $1.20  9m
"""


@pytest.fixture(autouse=True)
def isolated_router(tmp_path, monkeypatch):
    """Markers, events and email all land in the fixture, never the real home."""
    monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path / "prompt-router")
    monkeypatch.setattr(
        prompt_router, "EVENTS_FILE", tmp_path / "prompt-router-events.jsonl")
    prompt_router.STATE_DIR.mkdir(parents=True)


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture owner escalations instead of sending them."""
    sent = []

    def fake_send_email(**kw):
        sent.append(kw)
        return SimpleNamespace(success=True, id="test-stub")

    monkeypatch.setattr("agentwire.channels.email.send_email", fake_send_email)
    return sent


# =============================================================================
# Detection
# =============================================================================


class TestDetection:
    def test_the_real_dialog_is_detected(self):
        """The bug: this returned None, so nothing downstream ever ran."""
        info = detect_prompt(RESUME_DIALOG)
        assert info is not None
        assert info.kind == "resume"

    def test_all_three_options_are_parsed(self):
        info = detect_prompt(RESUME_DIALOG)
        assert [(o["number"], o["label"]) for o in info.options] == [
            (1, "Resume from summary (recommended)"),
            (2, "Resume full session as-is"),
            (3, "Don't ask me again"),
        ]

    def test_age_and_tokens_are_reported_as_context(self):
        assert detect_prompt(RESUME_DIALOG).summary == (
            "session is 2m old, 134.1k tokens")
        assert detect_prompt(RESUME_DIALOG_OLDER).summary == (
            "session is 2h 47m old, 233.6k tokens")

    def test_the_hash_ignores_the_ticking_age(self):
        """Otherwise every sweep sees a NEW prompt and re-notifies the parent.

        The title interpolates age and token count. If those reached the
        content hash, a dialog that redrew would defeat the presence-marker
        dedupe and paste into the parent every 60 seconds.
        """
        assert (detect_prompt(RESUME_DIALOG).content_hash()
                == detect_prompt(RESUME_DIALOG_OLDER).content_hash())

    def test_a_quoted_dialog_is_not_detected(self):
        """A pane merely SHOWING this text must never be routed as blocked."""
        assert detect_prompt(QUOTED_RESUME_DIALOG) is None

    def test_a_delivered_notification_is_not_re_detected(self):
        """The loop guard: our own message quotes the option labels."""
        info = detect_prompt(RESUME_DIALOG)
        message = prompt_router.build_message("worker", 0, info)
        assert detect_prompt(message + "\n\n Enter to confirm · Esc to cancel") is None

    def test_it_does_not_shadow_the_other_dialog_kinds(self):
        """_detect_resume runs first; the existing three must still classify."""
        from tests.unit.test_prompt_router import (
            PERMISSION_BASH,
            PLAN_APPROVAL,
            QUESTION_SIMPLE,
            QUESTION_TABBED,
        )

        assert detect_prompt(PERMISSION_BASH).kind == "permission"
        assert detect_prompt(PLAN_APPROVAL).kind == "plan"
        assert detect_prompt(QUESTION_SIMPLE).kind == "question"
        assert detect_prompt(QUESTION_TABBED).kind == "question"

    def test_the_footer_alone_is_not_enough(self):
        """The usage-limit dialog shares this exact footer."""
        from tests.unit.test_prompt_router import USAGE_LIMIT_DIALOG

        assert detect_prompt(USAGE_LIMIT_DIALOG) is None

    def test_the_anchor_alone_is_not_enough(self):
        """Body text with no live menu under it is prose, not a dialog."""
        prose = (
            "I hit the resume dialog. We recommend resuming from a summary.\n"
            "Anyway, moving on.\n"
        )
        assert detect_prompt(prose) is None


# =============================================================================
# Routing to a parent
# =============================================================================


class TestRouting:
    def test_a_blocked_worker_reaches_its_parent(self, monkeypatch):
        """End to end: the exact state that stranded four sessions."""
        delivered = []
        monkeypatch.setattr(
            prompt_router, "resolve_parent", lambda *a, **k: ("orch", 0))
        monkeypatch.setattr(
            prompt_router, "safe_deliver",
            lambda s, p, text: (delivered.append((s, p, text)), (True, "delivered"))[1])

        info = detect_prompt(RESUME_DIALOG)
        assert prompt_router.route_prompt("worker", 0, info) == "orch"

        target_session, target_pane, text = delivered[0]
        assert (target_session, target_pane) == ("orch", 0)
        assert "kind=resume" in text
        assert "134.1k tokens" in text
        assert "agentwire prompts answer" in text
        assert info.content_hash() in text

    def test_the_notification_is_answerable_as_captured(self, monkeypatch):
        """The hash in the message must match what `prompts answer` re-derives."""
        monkeypatch.setattr(prompt_router, "_capture", lambda t, **k: RESUME_DIALOG)
        sent = []
        monkeypatch.setattr(prompt_router, "_tmux", lambda args, **k: sent.append(args)
                            or SimpleNamespace(returncode=0, stdout=""))

        info = detect_prompt(RESUME_DIALOG)
        ok, reason = prompt_router.answer(
            "worker", 0, info.content_hash(), ["1"])
        assert ok, reason

    def test_a_stale_hash_refuses_to_answer(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_capture", lambda t, **k: RESUME_DIALOG)
        ok, reason = prompt_router.answer("worker", 0, "0000000000000000", ["1"])
        assert not ok


# =============================================================================
# The root case — no parent to route to
# =============================================================================


class TestRootEscalation:
    def test_a_root_session_emails_the_owner(self, monkeypatch, sent_emails):
        """no_parent used to be terminal: marker written, nobody told."""
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)

        info = detect_prompt(RESUME_DIALOG)
        assert prompt_router.route_prompt("documentscribe", 0, info) is None

        assert len(sent_emails) == 1
        assert "documentscribe" in sent_emails[0]["subject"]
        body = sent_emails[0]["body"]
        assert "no parent" in body
        assert "agentwire prompts answer -s 'documentscribe'" in body
        assert info.content_hash() in body

    def test_the_marker_records_the_escalation(self, monkeypatch, sent_emails):
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        prompt_router.route_prompt("root", 0, detect_prompt(RESUME_DIALOG))
        marker = prompt_router.read_marker("root", 0)
        assert marker["status"] == "no_parent"
        assert marker["escalated_at"]

    def test_the_sweeps_60s_cadence_does_not_become_60_emails_an_hour(
            self, monkeypatch, sent_emails):
        """A no-parent prompt is re-routed EVERY tick — nothing sets
        notified_at, so the sweep never short-circuits on it."""
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        info = detect_prompt(RESUME_DIALOG)
        for _ in range(10):
            prompt_router.route_prompt("root", 0, info)
        assert len(sent_emails) == 1

    def test_it_nags_again_once_the_ttl_expires(self, monkeypatch, sent_emails):
        """Four hours of silence is the failure; one email then quiet is not
        much better."""
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        info = detect_prompt(RESUME_DIALOG)
        prompt_router.route_prompt("root", 0, info)

        marker = prompt_router.read_marker("root", 0)
        old = datetime.fromisoformat(marker["escalated_at"]) - timedelta(hours=2)
        marker["escalated_at"] = old.isoformat()
        marker["detected_at"] = old.isoformat()
        prompt_router.write_marker("root", 0, **{
            k: v for k, v in marker.items() if k not in ("session", "pane")})

        prompt_router.route_prompt("root", 0, info)
        assert len(sent_emails) == 2
        assert "waiting 120 minutes" in sent_emails[1]["body"]

    def test_how_long_it_has_been_blocked_survives_the_rewrite(
            self, monkeypatch, sent_emails):
        """detected_at used to be refreshed on every tick, so a pane blocked
        for four hours read as seconds old to anything measuring the wait."""
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        info = detect_prompt(RESUME_DIALOG)
        prompt_router.route_prompt("root", 0, info)
        first = prompt_router.read_marker("root", 0)["detected_at"]

        for _ in range(5):
            prompt_router.route_prompt("root", 0, info)
        assert prompt_router.read_marker("root", 0)["detected_at"] == first

    def test_a_different_prompt_restarts_the_clock(self, monkeypatch, sent_emails):
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        prompt_router.route_prompt("root", 0, detect_prompt(RESUME_DIALOG))
        first = prompt_router.read_marker("root", 0)

        other = PromptInfo(kind="question", question="Ship it?",
                           options=[{"number": 1, "label": "Yes"}])
        prompt_router.route_prompt("root", 0, other)
        assert prompt_router.read_marker("root", 0)["detected_at"] != first["detected_at"]
        assert len(sent_emails) == 2

    def test_a_failing_mailer_never_breaks_the_sweep(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("resend is down")

        monkeypatch.setattr("agentwire.channels.email.send_email", boom)
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)

        assert prompt_router.route_prompt("root", 0, detect_prompt(RESUME_DIALOG)) is None
        assert prompt_router.read_marker("root", 0)["status"] == "no_parent"

    def test_a_parented_session_does_not_email(self, monkeypatch, sent_emails):
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: ("orch", 0))
        monkeypatch.setattr(prompt_router, "safe_deliver", lambda *a: (True, "delivered"))
        prompt_router.route_prompt("worker", 0, detect_prompt(RESUME_DIALOG))
        assert sent_emails == []


# =============================================================================
# The sweep + the doctor check
# =============================================================================


def _fake_tmux(panes, screens):
    """A tmux stub: `list-panes -a` rows plus per-pane capture output."""
    rows = "\n".join("\t".join(str(c) for c in p) for p in panes)

    def run(args, **kwargs):
        if args and args[0] == "list-panes":
            return SimpleNamespace(returncode=0, stdout=rows)
        if args and args[0] == "capture-pane":
            target = args[args.index("-t") + 1]
            return SimpleNamespace(returncode=0, stdout=screens.get(target, ""))
        return SimpleNamespace(returncode=0, stdout="")

    return run


@pytest.fixture
def fleet(monkeypatch):
    """Two agent panes and a shell pane; only the blocked one shows a dialog.

    Pane indices are 0 and 1 on purpose — base-index ships as 0 since #903 but
    windows created before it kept 1, so both conventions are live at once and
    nothing may assume either.
    """
    panes = [
        ("blocked", 1, "node", "/repo/a"),
        ("healthy", 0, "node", "/repo/b"),
        ("a-shell", 0, "zsh", "/repo/c"),
    ]
    screens = {
        "blocked.1": RESUME_DIALOG,
        "healthy.0": "⏺ working away\n\n───\n❯\n───\n",
        # A shell pane that happens to be displaying the dialog text — `cat` of
        # a saved capture. It must never be treated as a blocked agent.
        "a-shell.0": RESUME_DIALOG,
    }
    monkeypatch.setattr(prompt_router, "_tmux", _fake_tmux(panes, screens))
    monkeypatch.setattr(prompt_router, "_capture",
                        lambda t, **k: screens.get(t, ""))
    monkeypatch.setattr(prompt_router, "_is_parked", lambda s: False)
    monkeypatch.setattr(prompt_router, "_router_config", lambda: (True, set()))
    return screens


class TestSweep:
    def test_the_sweep_routes_the_blocked_pane(self, fleet, monkeypatch):
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: ("orch", 0))
        monkeypatch.setattr(prompt_router, "safe_deliver", lambda *a: (True, "delivered"))

        result = prompt_router.sweep()
        assert [(r["session"], r["pane"], r["kind"]) for r in result["routed"]] == [
            ("blocked", 1, "resume")]

    def test_a_shell_pane_showing_the_text_is_ignored(self, fleet, monkeypatch):
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: ("orch", 0))
        monkeypatch.setattr(prompt_router, "safe_deliver", lambda *a: (True, "delivered"))
        touched = {r["session"] for r in prompt_router.sweep()["routed"]}
        assert "a-shell" not in touched


class TestBlockedPanes:
    def test_it_finds_the_pane_every_liveness_check_calls_healthy(self, fleet):
        """`pane_current_command` is `node` for this pane — alive by every
        existing measure, and doing nothing."""
        blocked = prompt_router.blocked_panes()
        assert [(b["session"], b["pane"], b["kind"]) for b in blocked] == [
            ("blocked", 1, "resume")]

    def test_no_marker_means_nobody_was_told(self, fleet):
        b = prompt_router.blocked_panes()[0]
        assert b["status"] == "unrouted"
        assert b["stuck"] is True

    def test_a_freshly_routed_prompt_is_not_stuck(self, fleet):
        info = detect_prompt(RESUME_DIALOG)
        prompt_router.write_marker(
            "blocked", 1, kind="resume", hash=info.content_hash(),
            parent="orch", status="delivered",
            detected_at=prompt_router._now().isoformat(),
            notified_at=prompt_router._now().isoformat())
        b = prompt_router.blocked_panes()[0]
        assert (b["status"], b["stuck"]) == ("waiting", False)

    def test_a_long_wait_is_stuck(self, fleet):
        old = (prompt_router._now() - timedelta(hours=4)).isoformat()
        prompt_router.write_marker(
            "blocked", 1, kind="resume", hash="x", parent="orch",
            status="delivered", detected_at=old, notified_at=old)
        b = prompt_router.blocked_panes()[0]
        assert (b["status"], b["stuck"], b["waiting_minutes"]) == ("waiting", True, 240)

    def test_a_root_session_is_reported_as_no_parent(self, fleet):
        prompt_router.write_marker(
            "blocked", 1, kind="resume", hash="x", parent=None,
            status="no_parent",
            detected_at=prompt_router._now().isoformat(), notified_at=None)
        assert prompt_router.blocked_panes()[0]["status"] == "no_parent"

    def test_it_never_writes_a_marker(self, fleet):
        prompt_router.blocked_panes()
        assert list(prompt_router.STATE_DIR.glob("*.json")) == []

    def test_unreachable_tmux_is_none_not_empty(self, monkeypatch):
        """"Couldn't look" must not collapse into "looked, all healthy"."""
        monkeypatch.setattr(
            prompt_router, "_tmux",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
        assert prompt_router.blocked_panes() is None


class TestDoctorSection:
    def test_it_flags_the_blocked_session(self, fleet, capsys):
        from agentwire import doctor_cli

        found = doctor_cli._render_blocked_prompt_section()
        out = capsys.readouterr().out
        assert found == 1
        assert "blocked on a resume prompt" in out
        assert "blocked pane 1" in out
        assert "agentwire prompts answer" in out

    def test_a_healthy_fleet_reports_nothing(self, monkeypatch, capsys):
        from agentwire import doctor_cli

        monkeypatch.setattr(prompt_router, "blocked_panes", lambda: [])
        assert doctor_cli._render_blocked_prompt_section() == 0
        assert "No session is sitting on an unanswered prompt" in capsys.readouterr().out

    def test_a_routed_prompt_is_shown_but_not_counted(self, fleet, capsys):
        from agentwire import doctor_cli

        now = prompt_router._now().isoformat()
        prompt_router.write_marker(
            "blocked", 1, kind="resume", hash="x", parent="orch",
            status="delivered", detected_at=now, notified_at=now)
        found = doctor_cli._render_blocked_prompt_section()
        assert found == 0
        assert "routed to orch" in capsys.readouterr().out

    def test_unreachable_tmux_is_not_an_issue(self, monkeypatch, capsys):
        from agentwire import doctor_cli

        monkeypatch.setattr(prompt_router, "blocked_panes", lambda: None)
        assert doctor_cli._render_blocked_prompt_section() == 0
        assert "tmux not reachable" in capsys.readouterr().out


class TestEvents:
    def test_the_escalation_is_logged(self, monkeypatch, sent_emails):
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        prompt_router.route_prompt("root", 0, detect_prompt(RESUME_DIALOG))
        events = [json.loads(ln) for ln in
                  prompt_router.EVENTS_FILE.read_text().strip().splitlines()]
        assert {"no_parent_escalated", "no_parent"} <= {e["event"] for e in events}
