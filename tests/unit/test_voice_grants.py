"""Standing approvals for the voice assistant (#1198, option A).

The voice marker (``AGENTWIRE_VOICE=1``) only TIGHTENS: under bypass it keeps an
ask-tier rule as a real dialog (the spoken confirm) and names its rule; a
session without it is unchanged. Grants are keyed on the hook's own record of
the rule, scoped to agentwire-voice, always expire, and never cover an
unverifiable ask. Only the owner (not an agent) can answer or grant.
"""

import argparse
import json
import os
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from agentwire import prompt_router, voice_cli, voice_grants
from agentwire import voice_hooks as vh
from tests.conftest import HOOKS_DIR

HERMETIC_HOME = "/home/agentwire-hermetic"


def _hook(hook, payload, tmp_path, *, voice):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": HERMETIC_HOME,
           "AGENTWIRE_DIR": str(tmp_path / ".agentwire")}
    if voice:
        env["AGENTWIRE_VOICE"] = "1"
    return subprocess.run([sys.executable, str(HOOKS_DIR / hook)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)


def bash(command, tmp_path, *, voice, mode="bypassPermissions"):
    return _hook("bash-tool-damage-control.py", {"tool_name": "Bash", "tool_input": {"command": command},
                                                 "permission_mode": mode}, tmp_path, voice=voice)


def verdict(proc):
    if proc.returncode == 2:
        return "block", ""
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return "allow", ""
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


def record(tmp_path):
    path = tmp_path / ".agentwire" / "voice-approvals" / "pending-ask.json"
    return json.loads(path.read_text()) if path.exists() else None


# ── the marker: tightens only, and only where it is set ─────────────────


def test_without_the_marker_a_session_is_unchanged(tmp_path):
    """Bypass still demotes an ask-tier rule, an interactive ask names no rule,
    and nothing is recorded — the pre-#1198 behaviour, exactly."""
    assert verdict(bash("gh pr merge 5", tmp_path, voice=False)) == ("allow", "")
    decision, reason = verdict(bash("gh pr merge 5", tmp_path, voice=False, mode="default"))
    assert decision == "ask" and "[rule:" not in reason
    assert record(tmp_path) is None


def test_the_marker_keeps_an_ask_tier_rule_as_a_named_dialog(tmp_path):
    decision, reason = verdict(bash("gh pr merge 5", tmp_path, voice=True))
    rec = record(tmp_path)
    assert decision == "ask"
    assert rec["grantable"] is True and rec["rule_id"] and rec["tool"] == "Bash"
    assert reason.endswith(f"[rule: {rec['rule_id']}]")


def test_an_unverifiable_ask_is_recorded_never_grantable(tmp_path):
    decision, reason = verdict(bash('psql -c "$(cat drop.sql)"', tmp_path, voice=True))
    assert decision == "ask" and reason.endswith("; never grantable]")
    assert record(tmp_path)["grantable"] is False


def test_the_marker_never_loosens(tmp_path):
    assert verdict(bash("git push --force origin main", tmp_path, voice=True))[0] == "block"
    assert verdict(bash("ls -la", tmp_path, voice=True)) == ("allow", "")
    assert record(tmp_path) is None


def test_mcp_ask_tier_tools_stay_a_named_dialog_under_the_marker(tmp_path):
    payload = {"tool_name": "mcp__agentwire__worktree_remove", "tool_input": {"name": "x"},
               "permission_mode": "bypassPermissions"}
    decision, reason = verdict(_hook("mcp-tool-damage-control.py", payload, tmp_path, voice=True))
    assert decision == "ask" and reason.endswith("[rule: mcp.agentwire.worktree-remove]")
    assert record(tmp_path)["grantable"] is True


def test_the_marker_rides_only_the_voice_assistants_launch(tmp_path, monkeypatch):
    from agentwire.core import build_agent_command
    from agentwire.roles import RoleConfig

    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)
    voice = build_agent_command("bypass", [RoleConfig(name="voice-assistant", instructions="x")])
    other = build_agent_command("bypass", [RoleConfig(name="orchestrator", instructions="x")])
    assert '"AGENTWIRE_VOICE":"1"' in voice.command
    assert "AGENTWIRE_VOICE" not in other.command and "AGENTWIRE_VOICE" not in other.env


# ── the store is the owner's: no tool call writes it ────────────────────


def test_no_tool_call_can_write_a_grant_or_fake_a_pending_ask():
    """Writing grants.json or pending-ask.json IS approving a dialog by hand.
    Every agent write path is refused; reads, listing and revoking still work."""
    from agentwire.safety._core import check_command, check_path
    from agentwire.safety_commands import load_patterns

    cfg = load_patterns()
    cfg["safety"] = {"enabled": True, "disabled_rules": []}
    store = os.path.expanduser("~/.agentwire/voice-approvals")
    writes = [
        "echo '[]' > ~/.agentwire/voice-approvals/grants.json",
        "cp /tmp/x ~/.agentwire/voice-approvals/grants.json",
        "mv /tmp/x ~/.agentwire/voice-approvals/pending-ask.json",
        "tee ~/.agentwire/voice-approvals/grants.json < /tmp/x",
        "python3 -c \"open('$HOME/.agentwire/voice-approvals/grants.json','w').write('[]')\"",
        "rm -r ~/.agentwire/voice-approvals",
        "mv ~/.agentwire/voice-approvals /tmp/v",     # move the real store away…
        "mv /tmp/fake ~/.agentwire/voice-approvals",  # …and a fake one into its place
        "cp -r /tmp/fake ~/.agentwire/voice-approvals",
        "ln -sfn /tmp/fake ~/.agentwire/voice-approvals",
    ]
    allowed = ["cat ~/.agentwire/voice-approvals/grants.json", "ls ~/.agentwire/voice-approvals",
               "agentwire voice grants", "agentwire voice grants --clear",
               # siblings the prefix must not swallow: voice samples, the speech-hook state
               "cp sample.wav ~/.agentwire/voices/anna.wav",
               "echo '{}' > ~/.agentwire/voice/turn.json"]
    not_blocked = [c for c in writes if check_command(c, cfg)["decision"] != "block"]
    tool_writes_open = [f for f in ("grants.json", "pending-ask.json")
                        if not check_path(f"{store}/{f}", cfg)[0]]
    wrongly_blocked = [c for c in allowed if check_command(c, cfg)["decision"] == "block"]
    assert not not_blocked and not tool_writes_open and not wrongly_blocked, (
        not_blocked, tool_writes_open, wrongly_blocked)


def test_the_cli_store_ignores_env_redirects(monkeypatch, tmp_path):
    """An agent runs this CLI too: `HOME=/tmp/x agentwire voice approval` with a
    fake grant there must not reach a real dialog."""
    import pwd

    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".agentwire" / "voice-approvals"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTWIRE_DIR", str(tmp_path / ".agentwire"))
    assert voice_grants._dir() == real
    assert voice_grants.grants_path().parent == real


# ── the store ───────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_grants, "_dir", lambda: tmp_path / ".agentwire" / "voice-approvals")
    launch = {"at": "run-1"}
    monkeypatch.setattr(voice_grants, "_session_launch", lambda: launch["at"])
    return launch


NOON = datetime(2026, 10, 30, 12, 0).timestamp()


def test_a_grant_covers_its_rule_only_and_always_expires(store):
    voice_grants.add("git.pr-merge", "hour", now=NOON)
    assert voice_grants.covering("git.pr-merge", now=NOON + 60)["scope"] == "hour"
    assert voice_grants.covering("git.push", now=NOON + 60) is None
    assert voice_grants.covering("git.pr-merge", now=NOON + 3601) is None

    voice_grants.add("git.pr-merge", "today", now=NOON)
    assert voice_grants.covering("git.pr-merge", now=NOON + 11 * 3600)
    assert voice_grants.covering("git.pr-merge", now=NOON + 12 * 3600) is None  # past midnight


def test_a_session_grant_ends_when_the_assistant_relaunches(store):
    voice_grants.add("git.pr-merge", "session", now=NOON)
    assert voice_grants.covering("git.pr-merge", now=NOON + 60)
    store["at"] = "run-2"
    assert voice_grants.covering("git.pr-merge", now=NOON + 60) is None


def test_clear_and_every_change_is_logged(store, tmp_path):
    voice_grants.add("a", "today", now=NOON)
    voice_grants.add("b", "today", now=NOON)
    assert voice_grants.clear("a") == 1
    assert [g["rule_id"] for g in voice_grants.active(now=NOON)] == ["b"]
    assert voice_grants.clear() == 1 and voice_grants.active(now=NOON) == []
    events = [json.loads(line)["event"] for line in voice_grants.log_path().read_text().splitlines()]
    assert events == ["created", "created", "cleared", "cleared"]


def test_a_stale_pending_ask_covers_nothing(store, tmp_path):
    path = tmp_path / ".agentwire" / "voice-approvals" / "pending-ask.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rule_id": "git.pr-merge", "grantable": True, "ts": NOON}))
    assert voice_grants.pending_ask(now=NOON + 60)["rule_id"] == "git.pr-merge"
    assert voice_grants.pending_ask(now=NOON + voice_grants.PENDING_MAX_AGE_S + 1) is None


# ── the voice layer: applying and creating grants ───────────────────────

SCREEN = """ Bash command
 gh pr merge 5
 │ Hook PreToolUse:Bash requires confirmation for this command:
 │ merging a PR [rule: git.pr-merge]
 Do you want to proceed?
 ❯ 1. Yes
   2. No, and tell Claude what to do differently (esc)

 Esc to cancel · Tab to amend
"""


@pytest.fixture
def voice(store, tmp_path, monkeypatch):
    calls = types.SimpleNamespace(pressed=[], spoken=[], signals=[], queued=[], screen=SCREEN,
                                  verdict=("approve", None), judged=0)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, escapes=False: calls.screen)
    monkeypatch.setattr(prompt_router, "answer",
                        lambda s, p, h, keys: calls.pressed.append(keys) or (True, "sent"))
    monkeypatch.setattr(vh, "speak", lambda text, kind="say", display=None: calls.spoken.append(text))
    monkeypatch.setattr(vh, "signal", calls.signals.append)
    monkeypatch.setattr(vh, "APPROVAL_FILE", tmp_path / "approval.json")
    monkeypatch.setattr(voice_cli, "_service", lambda: types.SimpleNamespace(name="agentwire-voice"))
    monkeypatch.setattr(voice_cli, "REMINDED_FILE", tmp_path / "reminded")
    monkeypatch.setattr("agentwire.services.set_disabled", lambda *a: None)
    monkeypatch.setattr("agentwire.services.start_service", lambda s: (True, "already running"))
    monkeypatch.setattr("agentwire.inbox.enqueue", lambda to, text, kind, sender: calls.queued.append(text))

    def judge(intent, text):
        calls.judged += 1
        return calls.verdict

    monkeypatch.setattr(voice_cli, "classify_reply", judge)

    def pending(rule_id="git.pr-merge", grantable=True):
        path = tmp_path / ".agentwire" / "voice-approvals" / "pending-ask.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"rule_id": rule_id, "grantable": grantable,
                                    "tool": "Bash", "ts": __import__("time").time()}))

    calls.pending = pending
    calls.approval = lambda: voice_cli.cmd_voice_approval(
        argparse.Namespace(announce="Merge PR 5. Yes or no?", display=None, json=True))
    calls.ask = lambda text: voice_cli.cmd_voice_ask(argparse.Namespace(text=[text], json=True))
    return calls


def test_a_covered_dialog_gets_the_one_time_yes_and_says_so(voice):
    voice.pending()
    voice_grants.add("git.pr-merge", "today")
    voice.approval()
    assert voice.pressed == [["1"]]
    assert voice.spoken == ["Approved, standing OK."] and voice.signals == ["working"]
    events = [json.loads(line)["event"] for line in voice_grants.log_path().read_text().splitlines()]
    assert events[-1] == "used"


@pytest.mark.parametrize("case", ["no grant", "other rule", "never grantable", "no dialog"])
def test_anything_not_covered_is_announced_not_approved(voice, case):
    voice.pending(grantable=case != "never grantable")
    if case != "no grant":
        voice_grants.add("git.push" if case == "other rule" else "git.pr-merge", "today")
    if case == "no dialog":
        voice.screen = "❯ \n"
    voice.approval()
    assert voice.pressed == []
    assert voice.spoken == ["Merge PR 5. Yes or no?"] and voice.signals == ["approval"]


def test_yes_for_today_grants_the_pending_rule_and_approves(voice):
    voice.pending()
    voice.verdict = ("approve", "today")
    assert voice.ask("yes, approve those for today") == 0
    assert voice.pressed == [["1"]]
    assert [g["rule_id"] for g in voice_grants.active()] == ["git.pr-merge"]
    assert voice.spoken == ["Approved. Standing OK for the rest of today."]


def test_a_never_grantable_ask_is_approved_once_not_standing(voice):
    voice.pending(rule_id="core.ambiguous-command", grantable=False)
    voice.verdict = ("approve", "today")
    voice.ask("yes, for today")
    assert voice.pressed == [["1"]] and voice_grants.active() == []
    assert voice.spoken == ["Approved this once. That kind can't be a standing OK."]


def test_an_agent_can_neither_answer_nor_grant(voice, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    voice.pending()
    voice.verdict = ("approve", "today")
    assert voice.ask("yes, approve those for today") == 1
    assert voice.pressed == [] and voice_grants.active() == [] and voice.judged == 0
    assert "only the owner answers it" in capsys.readouterr().out


def test_the_verdict_is_derived_from_three_strict_fields():
    """The classifier answers yes/no/unclear, also_asks and lasts separately;
    the verdict comes from code. Anything off-format fails closed."""
    from agentwire.voice_cli import parse_reply_fields as parse

    def f(answer, also, lasts):
        return f"answer: {answer}\nalso_asks: {also}\nlasts: {lasts}\n"

    cases = {
        f("yes", "no", "once"): ("approve", None),
        f("yes", "no", "today"): ("approve", "today"),
        f("yes", "yes", "hour"): ("approve_and_more", "hour"),
        f("no", "yes", "today"): ("deny", None),       # a no never carries a grant
        f("unclear", "no", "once"): ("other", None),
        f("Yes.", "No.", "Session."): ("approve", "session"),
        f("yes", "no", "forever"): ("other", None),     # an unbounded grant is off-format
        f("maybe", "no", "once"): ("other", None),
        "answer: yes\nlasts: today\n": ("other", None),  # a missing field
        "approve": ("other", None),
        "": ("other", None),
    }
    wrong = {t: (want, parse(t)) for t, want in cases.items() if parse(t) != want}
    assert not wrong, wrong


async def test_the_chip_hears_the_grant_count(tmp_path):
    from unittest.mock import AsyncMock

    from agentwire.config import load_config
    from agentwire.server import AgentWireServer

    srv = AgentWireServer(load_config(tmp_path / "none.yaml"))
    srv.broadcast_dashboard = AsyncMock()

    def req(body):
        return types.SimpleNamespace(json=AsyncMock(return_value=body))

    await srv.api_voice_state(req({"grants": 2}))
    await srv.api_voice_state(req({"state": "done", "grants": 0}))
    statuses = [(await srv.api_voice_state(req(b))).status for b in ({}, {"grants": -1}, {"grants": True})]
    sent = [c.args[1] for c in srv.broadcast_dashboard.await_args_list]
    assert sent == [{"session": "agentwire-voice", "grants": 2},
                    {"session": "agentwire-voice", "state": "done", "grants": 0}]
    assert statuses == [400, 400, 400]
