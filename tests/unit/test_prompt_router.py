"""Tests for prompt_router — prompt detection, routing, markers, delivery.

Dialog fixtures are real `tmux capture-pane -p` output captured 2026-06-11
from Claude Code 2.x panes (fixture-gen session) and from the stuck worker
incident that motivated #276.
"""

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
        monkeypatch.setattr(prompt_router, "_SESSIONS_META_DIR", tmp_path / "sessions")
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
