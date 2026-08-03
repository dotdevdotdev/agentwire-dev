"""Tests for prompt_router — prompt detection, routing, markers, delivery.

Dialog fixtures are real `tmux capture-pane -p` output captured 2026-06-11
from Claude Code 2.x panes (fixture-gen session) and from the stuck worker
incident that motivated #276.
"""

from types import SimpleNamespace

import pytest

from agentwire import prompt_router
from agentwire.prompt_router import PromptInfo, detect_prompt, parse_ask_options

# =============================================================================
# Fixtures — real captures, verbatim
# =============================================================================

PERMISSION_BASH = """\
 ⚠ 1 setup issue: MCP · /doctor

 ▎ Fable 5 is here! Our newest model for complex, long-running work
 ▎ Included in your plan limits until Jun 22, then switch to usage credits to
 ▎ continue.

❯ Run this exact bash command: touch /tmp/fixture-test-file.txt

⏺ Bash(touch /tmp/fixture-test-file.txt)
  ⎿  Waiting…

────────────────────────────────────────────────────────────────────────────────
 Bash command

   touch /tmp/fixture-test-file.txt
   Create empty test file in /tmp

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to tmp/ from this project
   3. No

 Esc to cancel · Tab to amend · ctrl+e to explain
"""

PERMISSION_WRITE = """\
❯ Now use your Edit tool to add a line saying hello to /tmp/fixture-gen/test.md
  (create it with Write if needed)

⏺ Write(/tmp/fixture-gen/test.md)

────────────────────────────────────────────────────────────────────────────────
 Create file
 ../../../tmp/fixture-gen/test.md
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hello
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create test.md?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""

# Narrow-pane wrap from the 2026-06-11 stuck-worker incident (#276).
PERMISSION_WRAPPED = """\
 This command requires
 approval
 Do you want to
 proceed?
 ❯ 1. Yes
  2Yes, and    : gh
   don’t ask   issue *
   again for
   3. No
 Esc to cancel · Tab
 to amend · ctrl+e to
 explain
"""

PLAN_APPROVAL = """\

────────────────────────────────────────────────────────────────────────────────
 Ready to code?

 Here is Claude's plan:
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Plan

 Add a README.md to /private/tmp/fixture-gen briefly describing the folder's
 purpose and its current contents (test.md).
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

────────────────────────────────────────────────────────────────────────────────
 Claude has written up a plan and is ready to execute. Would you like to
 proceed?

 ❯ 1. Yes, and use auto mode
   2. Yes, manually approve edits
   3. No, refine with Ultraplan on Claude Code on the web
   4. Tell Claude what to change
      shift+tab to approve with this feedback

 ctrl+g to edit in  VS Code  · ~/.claude/plans/robust-scribbling-blum.md
"""

QUESTION_SIMPLE = """\
      1 hello

⏺ Created /tmp/fixture-gen/test.md with a "hello" line.

✻ Brewed for 9s

❯ Use the AskUserQuestion tool to ask me one question: 'Which color should the
  banner be?' with options 'Teal' (description: matches brand) and 'Orange'
  (description: high contrast).
────────────────────────────────────────────────────────────────────────────────
 ☐ Banner color

Which color should the banner be?

❯ 1. Teal
     matches brand
  2. Orange
     high contrast
  3. Type something.
────────────────────────────────────────────────────────────────────────────────
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

QUESTION_TABBED = """\
❯ Use AskUserQuestion ONCE with TWO questions in the same call: question 1
  header 'Color', question 'Which color?' options Teal/Orange; question 2
  header 'Size', question 'Which size?' options Small/Large.
────────────────────────────────────────────────────────────────────────────────
←  ☐ Color  ☐ Size  ✔ Submit  →

Which color?

❯ 1. Teal
     Teal color option
  2. Orange
     Orange color option
  3. Type something.
────────────────────────────────────────────────────────────────────────────────
  4. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

# A pane DISPLAYING a captured permission dialog: the quote sits mid-screen
# with the pane's own input box + status bar below. Must never match.
QUOTED_DIALOG = """\
  ──────────────────────────────────────────────────────────────────────────────
  ──
   Bash command

     touch /tmp/fixture-test-file.txt
     Create empty test file in /tmp

   Do you want to proceed?
   ❯ 1. Yes
     2. Yes, and always allow access to tmp/ from this project
     3. No

   Esc to cancel · Tab to amend · ctrl+e to explain

✻ Crunched for 8s

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  /private/tmp/fixture-gen                           claude-fable-5  $2.02  3m
  [██████████████████████████████████████████████████████████████████░░░░] 95%
  ⏸ plan mode on (shift+tab to cycle) · ← for agents
"""

USAGE_LIMIT_DIALOG = """\
 You've hit your session limit · resets 11:40pm (America/Toronto)

 What do you want to do?
 ❯ 1. Stop and wait for limit to reset
   2. Switch to usage credits
   3. Switch to Team plan

 Enter to confirm · Esc to cancel
"""

PLAIN_OUTPUT = """\
⏺ Running tests...
  ⎿  142 passed in 3.2s

✻ Baked for 12s

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ~/projects/demo                                    claude-fable-5  $0.10  1m
"""

ROUTED_NOTIFICATION = """\
❯ working on the task

[PROMPT from worker-1 pane 0] kind=permission Claude wants to run: git push
Options: Yes, Yes always, No. Deadline: 300s.

 Do you want to proceed?
 ❯ 1. Yes
   2. No

 Esc to cancel · Tab to amend
"""


# =============================================================================
# Detection
# =============================================================================


class TestDetectPrompt:
    def test_permission_bash(self):
        info = detect_prompt(PERMISSION_BASH)
        assert info is not None
        assert info.kind == "permission"
        assert info.question == "Do you want to proceed?"
        assert [o["number"] for o in info.options] == [1, 2, 3]
        assert info.options[0]["label"] == "Yes"
        assert "touch /tmp/fixture-test-file.txt" in info.summary

    def test_permission_write(self):
        info = detect_prompt(PERMISSION_WRITE)
        assert info is not None
        assert info.kind == "permission"
        assert info.question == "Do you want to create test.md?"
        assert len(info.options) == 3

    def test_permission_wrapped_narrow_pane(self):
        info = detect_prompt(PERMISSION_WRAPPED)
        assert info is not None
        assert info.kind == "permission"
        assert "Do you want to" in info.question

    def test_plan_approval(self):
        info = detect_prompt(PLAN_APPROVAL)
        assert info is not None
        assert info.kind == "plan"
        assert [o["number"] for o in info.options] == [1, 2, 3, 4]
        assert info.options[0]["label"] == "Yes, and use auto mode"

    def test_plan_not_misdetected_as_question(self):
        # ASK_PATTERN_SIMPLE would happily match "proceed?\n\n❯ 1." —
        # ordering must let the plan detector claim it first.
        info = detect_prompt(PLAN_APPROVAL)
        assert info.kind == "plan"

    def test_question_simple(self):
        info = detect_prompt(QUESTION_SIMPLE)
        assert info is not None
        assert info.kind == "question"
        assert info.question == "Which color should the banner be?"
        labels = [o["label"] for o in info.options]
        assert "Teal" in labels and "Orange" in labels
        # Option rendered after the separator rule is still captured.
        assert "Chat about this" in labels

    def test_question_tabbed(self):
        info = detect_prompt(QUESTION_TABBED)
        assert info is not None
        assert info.kind == "question"
        assert info.question == "Which color?"

    def test_quoted_dialog_not_live(self):
        assert detect_prompt(QUOTED_DIALOG) is None

    def test_usage_limit_dialog_excluded(self):
        assert detect_prompt(USAGE_LIMIT_DIALOG) is None

    def test_plain_output(self):
        assert detect_prompt(PLAIN_OUTPUT) is None

    def test_empty_screen(self):
        assert detect_prompt("") is None
        assert detect_prompt("\n\n  \n") is None

    def test_routed_notification_is_poison(self):
        # A screen containing our own message prefix must never be detected,
        # even if a dialog is also visible — prevents notification loops.
        assert detect_prompt(ROUTED_NOTIFICATION) is None


class TestParseAskOptions:
    def test_labels_and_descriptions(self):
        block = "❯ 1. Teal\n     matches brand\n  2. Orange\n     high contrast\n"
        options = parse_ask_options(block)
        assert options == [
            {"number": 1, "label": "Teal", "description": "matches brand"},
            {"number": 2, "label": "Orange", "description": "high contrast"},
        ]

    def test_strips_ansi(self):
        block = "\x1b[1m❯ 1. Yes\x1b[0m\n  2. No\n"
        options = parse_ask_options(block)
        assert [o["label"] for o in options] == ["Yes", "No"]


class TestResolveParent:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
        monkeypatch.setattr(prompt_router, "_session_exists", lambda s: True)
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: None)
        self.tmp_path = tmp_path

    def _write_creator(self, session, creator):
        d = self.tmp_path / "sessions" / session
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(
            '{"created_by": "%s", "created_via": "new"}' % creator
        )

    def test_worker_pane_routes_to_pane_zero(self):
        assert prompt_router.resolve_parent("orch", 3) == ("orch", 0)

    def test_creator_metadata_wins(self, monkeypatch):
        self._write_creator("child", "orch")
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: "yml-parent")
        assert prompt_router.resolve_parent("child", 0) == ("orch", 0)

    def test_yml_parent_fallback(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: "yml-parent")
        assert prompt_router.resolve_parent("child", 0) == ("yml-parent", 0)

    def test_no_parent(self):
        assert prompt_router.resolve_parent("standalone", 0) is None

    def test_self_creator_skipped(self):
        self._write_creator("child", "child")
        assert prompt_router.resolve_parent("child", 0) is None

    def test_dead_creator_falls_through(self, monkeypatch):
        self._write_creator("child", "gone")
        monkeypatch.setattr(
            prompt_router, "_session_exists", lambda s: s != "gone"
        )
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: "yml-parent")
        assert prompt_router.resolve_parent("child", 0) == ("yml-parent", 0)

    def test_self_yml_parent_skipped(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: "child")
        assert prompt_router.resolve_parent("child", 0) is None

    def test_machine_suffix_stripped(self):
        self._write_creator("child", "orch")
        assert prompt_router.resolve_parent("child@studio", 0) == ("orch", 0)


class TestIsAgentPane:
    def test_command_classification(self, monkeypatch):
        cases = {
            "node": True,
            "claude": True,
            "2.1.170": True,
            "zsh": False,
            "bash": False,
            "vim": False,
            "less": False,
            "python3": False,
            "": False,
        }
        for command, expected in cases.items():
            monkeypatch.setattr(
                prompt_router, "pane_command", lambda s, p, c=command: c
            )
            assert prompt_router.is_agent_pane("s", 0) is expected, command


class TestScreenShowsLiveMenu:
    def test_dialog_screens(self):
        for screen in (
            PERMISSION_BASH,
            PERMISSION_WRITE,
            PLAN_APPROVAL,
            QUESTION_SIMPLE,
            USAGE_LIMIT_DIALOG,
        ):
            assert prompt_router.screen_shows_live_menu(screen) is True

    def test_safe_screens(self):
        assert prompt_router.screen_shows_live_menu(PLAIN_OUTPUT) is False
        assert prompt_router.screen_shows_live_menu("") is False

    def test_routed_notification_with_dialog_below_is_still_unsafe(self):
        # The detector's poison-marker rule must NOT weaken the pre-paste
        # check: a screen with both our prefix AND a live menu stays unsafe.
        assert prompt_router.screen_shows_live_menu(ROUTED_NOTIFICATION) is True


class TestSafeDeliver:
    @pytest.fixture(autouse=True)
    def _safe_world(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_session_exists", lambda s: True)
        monkeypatch.setattr(prompt_router, "_is_parked", lambda s: False)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PLAIN_OUTPUT)
        self.sent = []
        import agentwire.session_ready as session_ready

        monkeypatch.setattr(
            session_ready,
            "send_verified",
            lambda session, message, marker=None, **kw: self.sent.append(
                (session, message)
            )
            or True,
        )

    def test_delivers_to_safe_target(self):
        ok, reason = prompt_router.safe_deliver("orch", 0, "[PROMPT from w pane 1] hi")
        assert ok and reason == "delivered"
        assert self.sent == [("orch", "[PROMPT from w pane 1] hi")]

    def test_refuses_dead_session(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_session_exists", lambda s: False)
        assert prompt_router.safe_deliver("orch", 0, "x") == (False, "target_gone")
        assert self.sent == []

    def test_refuses_parked_session(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_is_parked", lambda s: True)
        assert prompt_router.safe_deliver("orch", 0, "x") == (False, "target_parked")
        assert self.sent == []

    def test_refuses_shell_pane(self, monkeypatch):
        # Pasting into a shell would EXECUTE the message text.
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: False)
        assert prompt_router.safe_deliver("orch", 0, "x") == (False, "target_not_agent")
        assert self.sent == []

    def test_refuses_target_showing_dialog(self, monkeypatch):
        # Paste + Enter into a live menu would CONFIRM the highlighted option.
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PERMISSION_BASH)
        assert prompt_router.safe_deliver("orch", 0, "x") == (False, "target_dialog")
        assert self.sent == []

    def test_unverified_send_reported(self, monkeypatch):
        import agentwire.session_ready as session_ready

        monkeypatch.setattr(
            session_ready, "send_verified", lambda *a, **kw: False
        )
        ok, reason = prompt_router.safe_deliver("orch", 0, "x")
        assert not ok and reason == "delivery_unverified"


class TestHookScriptsUseRealCommands:
    HOOKS_DIR = (
        __import__("pathlib").Path(__file__).resolve().parent.parent.parent
        / "agentwire"
        / "hooks"
    )

    def test_no_nonexistent_alert_command(self):
        # `agentwire alert` never existed — every delivery through it was
        # silently dropped (#276). Guard against reintroduction.
        for script in ("idle-handler.sh", "queue-processor.sh"):
            source = (self.HOOKS_DIR / script).read_text()
            assert " alert " not in source.replace("alert-activity", ""), script

    def test_queue_processor_uses_notify_parent_raw(self):
        source = (self.HOOKS_DIR / "queue-processor.sh").read_text()
        assert "notify-parent -q --raw --to" in source

    def test_idle_handler_has_prompt_router_guard(self):
        source = (self.HOOKS_DIR / "idle-handler.sh").read_text()
        assert ".agentwire/prompt-router/" in source


@pytest.fixture
def router_home(tmp_path, monkeypatch):
    """Isolate marker state + events under a temp dir."""
    monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path / "prompt-router")
    monkeypatch.setattr(prompt_router, "EVENTS_FILE", tmp_path / "events.jsonl")
    return tmp_path


def _events(tmp_path):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    import json

    return [json.loads(line) for line in path.read_text().splitlines()]


class TestMarkers:
    def test_roundtrip(self, router_home):
        prompt_router.write_marker("sess", 1, kind="plan", hash="abc")
        marker = prompt_router.read_marker("sess", 1)
        assert marker["kind"] == "plan" and marker["hash"] == "abc"
        prompt_router.clear_marker("sess", 1)
        assert prompt_router.read_marker("sess", 1) is None

    def test_worktree_session_names_nest(self, router_home):
        prompt_router.write_marker("proj/branch", 0, kind="question", hash="x")
        assert prompt_router.read_marker("proj/branch", 0)["hash"] == "x"
        assert (prompt_router.STATE_DIR / "proj" / "branch.0.json").exists()

    def test_list_markers_skips_dotfiles(self, router_home):
        prompt_router.write_marker("a", 0, kind="plan", hash="h1")
        prompt_router.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (prompt_router.STATE_DIR / ".tick.lock").write_text("")
        assert len(prompt_router.list_markers()) == 1


class TestBuildMessage:
    def test_message_is_not_detectable_as_dialog(self, router_home):
        info = detect_prompt(PERMISSION_BASH)
        message = prompt_router.build_message("worker", 0, info)
        # The notification must never look like a live dialog to the sweep
        # (self-trigger loop) or to the pre-paste safety check.
        assert "❯" not in message
        assert "Esc to cancel" not in message
        assert "Enter to confirm" not in message
        assert detect_prompt(message) is None
        assert message.startswith("[PROMPT from worker pane 0] kind=permission")

    def test_message_carries_answer_contract(self, router_home):
        info = detect_prompt(PLAN_APPROVAL)
        message = prompt_router.build_message("child", 0, info)
        assert f"--expect {info.content_hash()}" in message
        assert "agentwire prompts answer -s 'child' --pane 0" in message
        assert "1=Yes, and use auto mode" in message


class TestRoutePrompt:
    @pytest.fixture(autouse=True)
    def _wire(self, router_home, monkeypatch):
        self.home = router_home
        self.delivered = []
        monkeypatch.setattr(
            prompt_router, "resolve_parent", lambda s, p, pp=None: ("orch", 0)
        )
        monkeypatch.setattr(
            prompt_router,
            "safe_deliver",
            lambda ts, tp, text: self.delivered.append((ts, text)) or (True, "delivered"),
        )

    def test_routes_and_marks(self):
        info = detect_prompt(PLAN_APPROVAL)
        parent = prompt_router.route_prompt("child", 0, info)
        assert parent == "orch"
        assert self.delivered[0][0] == "orch"
        marker = prompt_router.read_marker("child", 0)
        assert marker["status"] == "delivered" and marker["notified_at"]
        assert _events(self.home)[0]["event"] == "prompt_routed"

    def test_no_parent_marks_without_delivery(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        info = detect_prompt(PLAN_APPROVAL)
        assert prompt_router.route_prompt("solo", 0, info) is None
        assert self.delivered == []
        assert prompt_router.read_marker("solo", 0)["status"] == "no_parent"
        assert _events(self.home)[0]["event"] == "no_parent"

    def test_deferred_delivery_marks_unnotified(self, monkeypatch):
        monkeypatch.setattr(
            prompt_router, "safe_deliver", lambda *a: (False, "target_dialog")
        )
        info = detect_prompt(PLAN_APPROVAL)
        assert prompt_router.route_prompt("child", 0, info) is None
        marker = prompt_router.read_marker("child", 0)
        assert marker["status"] == "target_dialog" and marker["notified_at"] is None

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            prompt_router, "resolve_parent",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        info = detect_prompt(PLAN_APPROVAL)
        assert prompt_router.route_prompt("child", 0, info) is None
        assert _events(self.home)[0]["event"] == "route_failed"


class TestAnswer:
    @pytest.fixture(autouse=True)
    def _wire(self, router_home, monkeypatch):
        self.sent_keys = []

        def fake_tmux(args, timeout=5):
            self.sent_keys.append(args)

            class R:
                returncode = 0
                stdout = ""

            return R()

        monkeypatch.setattr(prompt_router, "_tmux", fake_tmux)

    def test_answers_live_matching_prompt(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PLAN_APPROVAL)
        expected = detect_prompt(PLAN_APPROVAL).content_hash()
        ok, msg = prompt_router.answer("child", 0, expected, ["2"])
        assert ok
        assert self.sent_keys == [["send-keys", "-t", "child.0", "2"]]

    def test_refuses_when_prompt_gone(self, monkeypatch):
        # Human answered first via portal → no-op, no stray keystroke.
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PLAIN_OUTPUT)
        ok, msg = prompt_router.answer("child", 0, "whatever", ["2"])
        assert not ok and "no live prompt" in msg
        assert self.sent_keys == []

    def test_refuses_when_prompt_changed(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PERMISSION_BASH)
        plan_hash = detect_prompt(PLAN_APPROVAL).content_hash()
        ok, msg = prompt_router.answer("child", 0, plan_hash, ["1"])
        assert not ok and "DIFFERENT prompt" in msg
        assert self.sent_keys == []

    def test_clears_marker_on_answer(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PLAN_APPROVAL)
        prompt_router.write_marker("child", 0, kind="plan", hash="h")
        expected = detect_prompt(PLAN_APPROVAL).content_hash()
        prompt_router.answer("child", 0, expected, ["2"])
        assert prompt_router.read_marker("child", 0) is None

    def test_marker_bridges_hook_payload_hash(self, monkeypatch):
        # Hook-routed permission: --expect carries a payload-derived hash the
        # screen can't reproduce; the marker (same hash, same kind) bridges
        # it (live-verified failure mode: unanswerable prompt, 2026-06-11).
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PERMISSION_BASH)
        prompt_router.write_marker(
            "child", 0, kind="permission", hash="payload-hash", source="hook"
        )
        ok, msg = prompt_router.answer("child", 0, "payload-hash", ["1"])
        assert ok
        assert self.sent_keys == [["send-keys", "-t", "child.0", "1"]]

    def test_marker_does_not_bridge_kind_mismatch(self, monkeypatch):
        # A plan dialog live + a stale permission marker must still refuse.
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PLAN_APPROVAL)
        prompt_router.write_marker(
            "child", 0, kind="permission", hash="payload-hash", source="hook"
        )
        ok, msg = prompt_router.answer("child", 0, "payload-hash", ["1"])
        assert not ok and "DIFFERENT prompt" in msg
        assert self.sent_keys == []


class TestSweep:
    PANES = "orch\t0\t2.1.170\t/p/orch\nchild\t0\t2.1.170\t/p/child\nshelly\t0\tzsh\t/p/x"

    @pytest.fixture(autouse=True)
    def _wire(self, router_home, monkeypatch):
        self.home = router_home
        self.screens = {"orch.0": PLAIN_OUTPUT, "child.0": PLAN_APPROVAL,
                        "shelly.0": PLAN_APPROVAL}
        self.routed = []

        def fake_tmux(args, timeout=5):
            class R:
                returncode = 0
                stdout = self.PANES

            return R()

        monkeypatch.setattr(prompt_router, "_tmux", fake_tmux)
        monkeypatch.setattr(prompt_router, "_capture", lambda t: self.screens.get(t, ""))
        monkeypatch.setattr(prompt_router, "_router_config", lambda: (True, set()))
        monkeypatch.setattr(prompt_router, "_is_parked", lambda s: False)
        monkeypatch.setattr(
            prompt_router, "resolve_parent", lambda s, p, pp=None: ("orch", 0)
        )
        monkeypatch.setattr(
            prompt_router,
            "safe_deliver",
            lambda ts, tp, text: self.routed.append(ts) or (True, "delivered"),
        )

    def test_routes_dialog_and_skips_shell_pane(self):
        result = prompt_router.sweep()
        assert [e["session"] for e in result["routed"]] == ["child"]
        # zsh pane shows the same dialog text but is not an agent pane.
        assert all(e["session"] != "shelly" for e in result["routed"])

    def test_second_sweep_is_silent(self):
        prompt_router.sweep()
        result = prompt_router.sweep()
        assert result["routed"] == []
        assert [e["session"] for e in result["active"]] == ["child"]
        assert len(self.routed) == 1

    def test_marker_cleared_when_prompt_gone(self):
        prompt_router.sweep()
        assert prompt_router.read_marker("child", 0)
        self.screens["child.0"] = PLAIN_OUTPUT
        prompt_router.sweep()
        assert prompt_router.read_marker("child", 0) is None

    def test_renotifies_after_ttl(self, monkeypatch):
        prompt_router.sweep()
        marker = prompt_router.read_marker("child", 0)
        # Backdate the notification past the TTL.
        from datetime import timedelta as td

        old = (prompt_router._now() - prompt_router.RENOTIFY_TTL - td(minutes=1)).isoformat()
        marker["notified_at"] = old
        prompt_router.write_marker("child", 0, **{k: v for k, v in marker.items()
                                                  if k not in ("session", "pane")})
        result = prompt_router.sweep()
        assert [e["session"] for e in result["routed"]] == ["child"]
        assert len(self.routed) == 2

    def test_hook_marker_suppresses_permission_sweep(self):
        # The hook's hash derives from the tool payload and NEVER equals the
        # sweep's screen-derived hash — suppression must not be hash-gated
        # (live-verified failure mode: double notification, 2026-06-11).
        self.screens["child.0"] = PERMISSION_BASH
        prompt_router.write_marker(
            "child", 0, kind="permission", hash="payload-derived-hash",
            source="hook", detected_at=prompt_router._now().isoformat(),
            notified_at=None, parent="orch", status="delivered",
        )
        result = prompt_router.sweep()
        assert result["routed"] == []
        assert self.routed == []

    def test_excluded_session_skipped(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_router_config", lambda: (True, {"child"}))
        result = prompt_router.sweep()
        assert result["routed"] == []

    def test_disabled_config(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_router_config", lambda: (False, set()))
        assert prompt_router.sweep() == {"routed": [], "deferred": [], "active": []}

    def test_parked_session_skipped(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_is_parked", lambda s: s == "child")
        result = prompt_router.sweep()
        assert result["routed"] == []

    def test_gc_drops_orphaned_old_markers(self):
        from datetime import timedelta as td

        old = (prompt_router._now() - prompt_router.MARKER_GC_TTL - td(minutes=1)).isoformat()
        prompt_router.write_marker("dead-sess", 2, kind="plan", hash="h", detected_at=old)
        prompt_router.sweep()
        assert prompt_router.read_marker("dead-sess", 2) is None


class TestContentHash:
    def test_stable_across_calls(self):
        a = detect_prompt(PERMISSION_BASH).content_hash()
        b = detect_prompt(PERMISSION_BASH).content_hash()
        assert a == b

    def test_differs_by_prompt(self):
        a = detect_prompt(PERMISSION_BASH).content_hash()
        b = detect_prompt(PERMISSION_WRITE).content_hash()
        assert a != b

    def test_wrap_invariant(self):
        # Same logical prompt re-wrapped at a different width hashes equal.
        wide = PromptInfo(kind="plan", question="Would you like to proceed?")
        wrapped = PromptInfo(kind="plan", question="Would you like\nto   proceed?")
        assert wide.content_hash() == wrapped.content_hash()


class TestRecordSessionCreator:
    def test_records_and_merges(self, tmp_path, monkeypatch):
        import agentwire.__main__ as cli

        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
        cli.store_session_metadata("child", {"existing": "kept"})
        cli._record_session_creator("child", "orch", via="new")
        meta = cli.load_session_metadata("child")
        assert meta["created_by"] == "orch"
        assert meta["created_via"] == "new"
        assert meta["existing"] == "kept"

    def test_self_and_none_creator_skipped(self, tmp_path, monkeypatch):
        import agentwire.__main__ as cli

        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
        cli._record_session_creator("child", "child", via="new")
        cli._record_session_creator("child", None, via="new")
        assert cli.load_session_metadata("child") == {}

    def test_empty_creator_recorded_as_explicitly_rootless(self, tmp_path, monkeypatch):
        """`--created-by ''` means "no parent" and must be written (#848).

        Skipping it left a stale parent from a prior creation in place, so
        `resolve_parent` kept routing to a creator the caller had explicitly
        disowned. Only `None` — no signal at all — is a skip.
        """
        import agentwire.__main__ as cli

        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
        cli.store_session_metadata("child", {"created_by": "stale-orch"})
        cli._record_session_creator("child", "", via="new")
        assert cli.load_session_metadata("child")["created_by"] == ""


class TestNotifyPermissionRequest:
    @pytest.fixture(autouse=True)
    def _wire(self, router_home, monkeypatch):
        self.routed = []
        monkeypatch.setattr(
            prompt_router,
            "route_prompt",
            lambda s, p, info, source="hook", project_path=None: self.routed.append(
                (s, p, info, source)
            )
            or "orch",
        )

    def test_bash_summary(self):
        prompt_router.notify_permission_request(
            "child", 0, {"tool_name": "Bash", "tool_input": {"command": "git push"}}
        )
        _, _, info, source = self.routed[0]
        assert info.kind == "permission" and source == "hook"
        assert "git push" in info.question

    def test_exit_plan_mode_maps_to_plan_kind(self):
        # ExitPlanMode fires the hook but renders the plan dialog — the
        # notification must carry the plan dialog's kind + options or the
        # sweep/answer kinds disagree (live-verified 2026-06-11).
        prompt_router.notify_permission_request(
            "child", 0,
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": "Do the thing"}},
        )
        _, _, info, _ = self.routed[0]
        assert info.kind == "plan"
        assert info.summary == "Do the thing"
        assert [o["number"] for o in info.options] == [1, 2, 3, 4]
        assert info.options[0]["label"] == "Yes, and use auto mode"

    def test_ask_user_question_maps_to_question_kind(self):
        # AskUserQuestion fires the hook in prompted sessions (drill-verified
        # 2026-06-11); the payload carries the question + options.
        prompt_router.notify_permission_request(
            "child", 0,
            {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{
                "question": "Ship it?",
                "header": "Ship",
                "options": [
                    {"label": "Yes", "description": "ship now"},
                    {"label": "No", "description": "hold"},
                ],
            }]}},
        )
        _, _, info, source = self.routed[0]
        assert info.kind == "question" and source == "hook"
        assert info.question == "Ship it?"
        assert [o["label"] for o in info.options] == ["Yes", "No"]


# =============================================================================
# #689 — stuck-box sweeper backstop
# =============================================================================

STUCK_RULE = "─" * 60


def _stuck_screen(box_text, footer=""):
    return f"output above\n{STUCK_RULE}\n❯ {box_text}\n{STUCK_RULE}\n{footer}"


class TestFlushStuckBox:
    """The last-resort healer: machine-injected text stuck in an idle pane's
    input box across consecutive sweeps gets a bare Enter — never a paste."""

    def _patch(self, monkeypatch, tmp_path):
        monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path)
        keys = []

        def fake_tmux(args, timeout=5):
            keys.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(prompt_router, "_tmux", fake_tmux)
        monkeypatch.setattr(
            prompt_router, "EVENTS_FILE", tmp_path / "events.jsonl")
        return keys

    def test_machine_header_flushes_on_second_sweep(self, monkeypatch, tmp_path):
        keys = self._patch(monkeypatch, tmp_path)
        screen = _stuck_screen("[MSG from worker · done] finished  ⟨#abc123⟩")
        assert not prompt_router._flush_stuck_box("s", 0, screen)  # sweep 1: arm
        assert keys == []
        assert prompt_router._flush_stuck_box("s", 0, screen)  # sweep 2: flush
        assert keys == [["send-keys", "-t", "s.0", "Enter"]]

    def test_pasted_text_placeholder_flushes(self, monkeypatch, tmp_path):
        keys = self._patch(monkeypatch, tmp_path)
        screen = _stuck_screen("[Pasted text #1 +12 lines]")
        prompt_router._flush_stuck_box("s", 0, screen)
        assert prompt_router._flush_stuck_box("s", 0, screen)
        assert len(keys) == 1

    def test_human_draft_never_flushed(self, monkeypatch, tmp_path):
        keys = self._patch(monkeypatch, tmp_path)
        screen = _stuck_screen("my half-typed human thought")
        for _ in range(5):
            assert not prompt_router._flush_stuck_box("s", 0, screen)
        assert keys == []

    def test_changed_content_resets_counter(self, monkeypatch, tmp_path):
        keys = self._patch(monkeypatch, tmp_path)
        a = _stuck_screen("[MSG from w · done] one  ⟨#aaa111⟩")
        b = _stuck_screen("[MSG from w · done] two  ⟨#bbb222⟩")
        assert not prompt_router._flush_stuck_box("s", 0, a)
        assert not prompt_router._flush_stuck_box("s", 0, b)  # reset, re-armed
        assert keys == []

    def test_generating_pane_still_flushes(self, monkeypatch, tmp_path):
        # #698 regression (the 12:40 incident): Enter on a generating pane
        # QUEUES the draft — it never interrupts — so a stuck machine message
        # on a busy orchestrator must be rescued on the normal two-sweep
        # cadence, not starved (and counter-reset) until the turn ends.
        keys = self._patch(monkeypatch, tmp_path)
        screen = _stuck_screen(
            "[MSG from w · done] finished  ⟨#abc123⟩",
            footer="✶ Working… (esc to interrupt)",
        )
        assert not prompt_router._flush_stuck_box("s", 0, screen)  # arm
        assert prompt_router._flush_stuck_box("s", 0, screen)  # rescue
        assert keys == [["send-keys", "-t", "s.0", "Enter"]]

    def test_queued_placeholder_skipped(self, monkeypatch, tmp_path):
        keys = self._patch(monkeypatch, tmp_path)
        screen = _stuck_screen("[MSG queue] Press up to edit queued messages")
        prompt_router._flush_stuck_box("s", 0, screen)
        assert not prompt_router._flush_stuck_box("s", 0, screen)
        assert keys == []

    def test_empty_box_clears_state(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path)
        stuck = _stuck_screen("[MSG from w · done] hi  ⟨#abc123⟩")
        assert not prompt_router._flush_stuck_box("s", 0, stuck)  # armed
        prompt_router._flush_stuck_box("s", 0, _stuck_screen(""))  # cleared
        # Re-stuck: must take TWO sweeps again, not fire immediately.
        assert not prompt_router._flush_stuck_box("s", 0, stuck)

    def test_unparseable_frame_holds_counter(self, monkeypatch, tmp_path):
        # #698 — a transient mid-redraw frame (no box parses) must not reset
        # the stuck counter; the next stuck sighting completes the pair and
        # rescues within that one sweep.
        keys = self._patch(monkeypatch, tmp_path)
        stuck = _stuck_screen("[MSG from w · done] hi  ⟨#abc123⟩")
        assert not prompt_router._flush_stuck_box("s", 0, stuck)  # armed
        assert not prompt_router._flush_stuck_box("s", 0, "garbled, no rules")
        assert prompt_router._flush_stuck_box("s", 0, stuck)  # rescued
        assert len(keys) == 1

    def test_live_menu_holds_and_never_enters(self, monkeypatch, tmp_path):
        # A live dialog owns Enter — never flush into it, but keep the counter
        # so the rescue fires once the dialog is gone.
        keys = self._patch(monkeypatch, tmp_path)
        stuck = _stuck_screen("[MSG from w · done] hi  ⟨#abc123⟩")
        menu = _stuck_screen(
            "[MSG from w · done] hi  ⟨#abc123⟩", footer="Esc to cancel"
        )
        assert not prompt_router._flush_stuck_box("s", 0, stuck)  # armed
        assert not prompt_router._flush_stuck_box("s", 0, menu)  # held
        assert keys == []
        assert prompt_router._flush_stuck_box("s", 0, stuck)  # rescued
        assert len(keys) == 1


class TestSafeDeliverPane:
    def test_pane_index_threads_to_send_verified(self, monkeypatch):
        from agentwire import session_ready
        monkeypatch.setattr(prompt_router, "_session_exists", lambda s: True)
        monkeypatch.setattr(prompt_router, "_is_parked", lambda s: False)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)
        monkeypatch.setattr(
            prompt_router, "screen_shows_live_menu", lambda v: False)
        monkeypatch.setattr(prompt_router, "_capture", lambda t: "")
        seen = {}

        def fake_send(session, text, marker=None, retries=1, settle=2.0,
                      pane_index=0):
            seen["pane"] = pane_index
            return True

        monkeypatch.setattr(session_ready, "send_verified", fake_send)
        ok, reason = prompt_router.safe_deliver("s", 3, "hello")
        assert ok and seen["pane"] == 3
