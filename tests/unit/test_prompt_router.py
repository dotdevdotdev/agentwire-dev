"""Tests for prompt_router — prompt detection, routing, markers, delivery.

Dialog fixtures are real `tmux capture-pane -p` output captured 2026-06-11
from Claude Code 2.x panes (fixture-gen session) and from the stuck worker
incident that motivated #276.
"""

from types import SimpleNamespace

import pytest

from agentwire import prompt_router
from agentwire.prompt_router import detect_prompt

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


# Narrow-pane wrap from the 2026-06-11 stuck-worker incident (#276).

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

    def test_each_real_capture_reads_as_its_kind(self):
        # Routing answers by kind; a usage-limit dialog belongs to `limits`, a
        # quoted dialog or our own routed notification must never re-route.
        want = {
            "plan": PLAN_APPROVAL, "question": QUESTION_SIMPLE,
            None: (QUOTED_DIALOG, USAGE_LIMIT_DIALOG, PLAIN_OUTPUT, ROUTED_NOTIFICATION, ""),
        }
        bad = []
        for kind, screens in want.items():
            for screen in screens if isinstance(screens, tuple) else (screens,):
                info = detect_prompt(screen)
                if (info.kind if info else None) != kind:
                    bad.append((kind, info and info.kind, screen[:40]))
        assert not bad, bad


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

    def test_creator_metadata_wins(self, monkeypatch):
        self._write_creator("child", "orch")
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: "yml-parent")
        assert prompt_router.resolve_parent("child", 0) == ("orch", 0)


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

    def test_refuses_every_unsafe_target_without_pasting(self, monkeypatch):
        # gone; parked (corrupts the resume); a shell (pasted text EXECUTES);
        # a live menu (Paste + Enter CONFIRMS the highlighted option).
        cases = {
            "target_gone": ("_session_exists", lambda s: False),
            "target_parked": ("_is_parked", lambda s: True),
            "target_not_agent": ("is_agent_pane", lambda s, p: False),
            "target_dialog": ("_capture", lambda t: PERMISSION_BASH),
        }
        bad = []
        for reason, (attr, fake) in cases.items():
            with monkeypatch.context() as m:
                m.setattr(prompt_router, attr, fake)
                got = prompt_router.safe_deliver("orch", 0, "x")
            if got != (False, reason) or self.sent:
                bad.append((reason, got, list(self.sent)))
            self.sent.clear()
        assert not bad, bad


@pytest.fixture
def router_home(tmp_path, monkeypatch):
    """Isolate marker state + events under a temp dir."""
    monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path / "prompt-router")
    monkeypatch.setattr(prompt_router, "EVENTS_FILE", tmp_path / "events.jsonl")
    return tmp_path


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

    def test_refuses_when_prompt_changed(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_capture", lambda t: PERMISSION_BASH)
        plan_hash = detect_prompt(PLAN_APPROVAL).content_hash()
        ok, msg = prompt_router.answer("child", 0, plan_hash, ["1"])
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

    def test_human_draft_never_flushed(self, monkeypatch, tmp_path):
        keys = self._patch(monkeypatch, tmp_path)
        screen = _stuck_screen("my half-typed human thought")
        for _ in range(5):
            assert not prompt_router._flush_stuck_box("s", 0, screen)
        assert keys == []

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


