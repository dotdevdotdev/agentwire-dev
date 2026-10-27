"""Fleet detectors produce typed-kind mail; `escalation` gets a producer (#982).

Two halves are tested here and they fail in opposite directions:

* **The false-REJECT half** — a detector that fires and reaches nobody. That is
  the state before this module existed: the one kind a recipient may act on out
  of turn had no fleet detector sending it, so the bell was wired to nothing.
* **The false-ACCEPT half, which is the expensive one.** A detector that
  over-produces escalations does not merely add noise — it destroys the tier,
  because a recipient who learns escalations are usually ignorable will ignore
  the one that wasn't. So the rulings in :data:`fleet_alerts.DETECTOR_KINDS`
  are pinned here as DATA: changing what may interrupt has to be a deliberate
  edit to a test that says why.

Nothing here asserts anything about *speed*, and nothing should. An alert is
ordinary inbox mail, so it waits for the drain (a 60s watchdog tick) before any
recipient sees it: `escalation` is a statement of priority, not of latency.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentwire import core, fleet_alerts, inbox, prompt_router


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """A throwaway config dir: session records, inboxes and event logs."""
    root = tmp_path / "agentwire"
    (root / "sessions").mkdir(parents=True)
    monkeypatch.setattr(core, "CONFIG_DIR", root)
    monkeypatch.setattr(inbox, "INBOX_ROOT", root / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", root / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    return root


def _subscribe(name: str = "listener") -> str:
    """A subscriber is a session that EXISTS — the record comes first.

    Not fixture ceremony: a subscription is a property of a session, and
    ``subscribe`` refuses a name with no record precisely so it can never mint
    one into the #871 store.
    """
    if not core.session_metadata_path(name).exists():
        core.store_session_metadata(name, {"role": "worker", "created_at": "x"})
    fleet_alerts.subscribe(name)
    return name


def _inbox_messages(session: str) -> list:
    return inbox.list_messages(session) + inbox.list_ingest(session)


# =============================================================================
# The ruling — which detector earns which kind
# =============================================================================


# =============================================================================
# Subscription — a lease, not a permanent flag
# =============================================================================


# =============================================================================
# emit — best-effort, never the detector's problem
# =============================================================================


class TestEmit:
    def test_enqueues_to_every_subscriber(self, isolate):
        _subscribe("listener")
        _subscribe("second")
        assert sorted(fleet_alerts.emit("hi", kind="note")) == ["listener", "second"]
        msg = _inbox_messages("listener")[0]
        assert msg.kind == "note" and msg.sender == fleet_alerts.SENDER
        assert msg.text == "hi"


# =============================================================================
# Detector: expired login (#906) — escalation, once per outage hour
# =============================================================================


# =============================================================================
# Detector: usage-limit park — a note, once per park
# =============================================================================


# =============================================================================
# Detector: dead-lettered load-bearing mail — inherits the lost kind
# =============================================================================


def _dead(kind: str, to: str = "someone", sender: str = "worker") -> inbox.Message:
    return inbox.Message(
        id="1-abc", sender=sender, to=to, kind=kind, text="report", ts=1, attempts=40
    )


class TestDeadLetters:
    def test_a_lost_done_is_a_request(self, isolate, monkeypatch):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done")], "target_gone")
        msgs = _inbox_messages("listener")
        assert len(msgs) == 1 and msgs[0].kind == "request"


# =============================================================================
# Detector: a root session blocked with nowhere to route (#905) — escalation
# =============================================================================


def _prompt_info() -> prompt_router.PromptInfo:
    return prompt_router.PromptInfo(
        kind="permission",
        question="Allow Bash(rm -rf build)?",
        options=[{"number": "1", "label": "Yes"}, {"number": "2", "label": "No"}],
        summary="",
    )


class TestCliSurface:
    """CLI is the SSOT: an API only a branch can reach is not shipped."""

    def _run(self, argv: list[str]) -> int:
        from agentwire.__main__ import build_parser

        args = build_parser().parse_args(argv)
        return args.func(args)

    @pytest.fixture(autouse=True)
    def a_real_session(self, isolate):
        """`listener` is an existing session here — the CLI refuses to invent one."""
        core.store_session_metadata("listener", {"role": "worker", "created_at": "x"})

    def test_subscribe_list_unsubscribe_round_trip(self, isolate, capsys):
        assert self._run(["alerts", "subscribe", "listener"]) == 0
        capsys.readouterr()
        assert self._run(["alerts", "list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["subscribers"] == ["listener"]
        assert self._run(["alerts", "unsubscribe", "listener"]) == 0
        capsys.readouterr()
        self._run(["alerts", "list", "--json"])
        assert json.loads(capsys.readouterr().out)["subscribers"] == []


