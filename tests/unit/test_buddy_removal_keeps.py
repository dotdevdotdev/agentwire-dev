"""What must survive the removal of the voice buddy (#1230, Phase 3).

Written BEFORE the deletion and mutation-checked against the pre-removal tree:
the notification round trip is green on both sides (it is the thing the
adapter-branch deletion must not break); the import-isolation, parser-surface
and role-prompt checks are red until the buddy is gone.
"""

import subprocess
import sys

import pytest

from agentwire import beta, inbox, prompt_router

_ROLES = ("orchestrator", "worker", "worker-worktree", "agentwire")


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    return tmp_path


def _stub_delivery(monkeypatch) -> list[str]:
    from agentwire import usage_limit

    monkeypatch.setattr(usage_limit, "_capture", lambda s, **kw: "screen")
    monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, **kw: "screen")
    monkeypatch.setattr(prompt_router, "input_box_content_sgr", lambda vis: "")
    monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p=0: True)
    monkeypatch.setattr(prompt_router, "prompt_is_empty", lambda s, p=0: True)
    sent: list[str] = []
    monkeypatch.setattr(
        prompt_router, "safe_deliver", lambda s, p, text: (sent.append(text) or (True, "delivered"))
    )
    return sent


def test_notification_kinds_round_trip_through_the_drain(isolate, monkeypatch):
    sent = _stub_delivery(monkeypatch)
    bad = []
    for kind in ("done", "idle", "escalation"):
        inbox.enqueue("orchestrator", f"body-{kind}", kind=kind, sender="worker-1")
        res = inbox.flush_session("orchestrator")
        if res["delivered"] != 1 or f"· {kind}] body-{kind}" not in sent[-1]:
            bad.append((kind, res, sent[-1:]))
        if inbox.list_messages("orchestrator"):
            bad.append((kind, "left in inbox"))
    assert not bad, bad


def test_removed_kind_is_refused(isolate):
    with pytest.raises(ValueError):
        inbox.enqueue("orchestrator", "hi", kind="voice", sender="x")


def test_hot_modules_never_import_the_buddy():
    code = (
        "import sys\n"
        "import agentwire.inbox, agentwire.msg_cli, agentwire.server, agentwire.doctor_cli\n"
        "import agentwire.__main__ as m; m.build_parser()\n"
        "bad = sorted(k for k in sys.modules if k.startswith('agentwire.voice_layer'))\n"
        "print('LEAK:' + ','.join(bad) if bad else 'CLEAN')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "CLEAN", out


def test_cli_surface_has_no_buddy_and_keeps_voice_adjacent_commands():
    from agentwire.__main__ import build_parser

    parser = build_parser()
    top = next(a for a in parser._actions if isinstance(a.choices, dict)).choices
    assert not {"buddy", "voice"} & set(top), sorted({"buddy", "voice"} & set(top))
    parsed = []
    for argv in (
        ["tts", "status"], ["stt", "status"], ["kokoro", "status"],
        ["say", "hello"], ["list", "--state"], ["new", "-s", "x", "--effort", "high"],
    ):
        try:
            parser.parse_args(argv)
        except SystemExit:
            parsed.append(argv)
    assert not parsed, f"no longer parse: {parsed}"


def test_rendered_roles_never_mention_the_buddy():
    from pathlib import Path

    from agentwire import roles
    from agentwire.config import BETA_FLAG_NAMES

    roles_dir = Path(roles.__file__).parent
    # Every flag on (plus the removed one by name, so a surviving gated block
    # would still render): nothing may mention the buddy.
    enabled = set(BETA_FLAG_NAMES) | {"voice_layer"}
    bad = [
        name for name in _ROLES
        if "buddy" in beta.render((roles_dir / f"{name}.md").read_text(), enabled=enabled).lower()
    ]
    assert not bad, bad
    assert roles.parse_role_file(roles_dir / "voice.md") is not None


def test_msg_send_tool_description_never_mentions_the_buddy():
    from agentwire import mcp_msg

    assert "buddy" not in (mcp_msg.msg_send.__doc__ or "").lower()


def test_load_bearing_kinds_are_exactly_the_report_kinds_and_doctor_sees_them(isolate, capsys):
    assert inbox.ESCALATE_KINDS == ("done", "request", "escalation")
    assert inbox.KINDS == ("note", "done", "request", "escalation", "ingest", "idle")

    from agentwire import doctor_cli

    msg = inbox.Message(
        id="1700000000000-x", sender="worker-1", to="orchestrator", kind="done",
        text="done corpse", ts=1700000000000, attempts=inbox.MAX_ATTEMPTS,
        reason="box_not_empty", dead_ts=1700000000001,
    )
    inbox._write_message(inbox.dead_dir("orchestrator") / f"{msg.id}.json", msg)
    doctor_cli._render_dead_letter_section()
    out = capsys.readouterr().out
    assert "[!!]" in out and "done" in out
