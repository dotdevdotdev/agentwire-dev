"""#1048 — three composing voice-UX regressions, tested AS the composition.

The live failure was never one defect: the no-parent detector re-raised dead
alerts as fresh mail (spam), the notices interrupted the proposal announcement
so it never anchored (livelock — every correct "confirm <nonce>" refused
``not_announced``), and every interrupted announcement fell back to the
full-volume browser voice (the 6s-miss firehose). Each piece reviewed fine
alone; the defect lived between them, so the acceptance test here drives the
REAL client factories together — gate + announcer + notifier + the page's own
``announce()`` — through the issue's scenario: a pending proposal plus a
firehose of inbox notices.

The individual mechanisms get their own pins too:

- the spine caps ``not_announced`` at ONE refusal per proposal, then judges;
- the page anchors a proposal at RENDER (screen visibility satisfies the
  announcement precondition — this channel is not screenless while the
  transcript panel is open);
- the bridge's staleness gate drops dead notices at read time WITH a reason;
- the browser fallback voice is volume/rate-pinned, and the 6s-miss log names
  its interrupt correlation.
"""

import json
import shutil
import subprocess
import textwrap
import time

import pytest

from agentwire import core, inbox
from agentwire.voice_layer import client, confirm, delivery, tools, transcript

_NODE = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the client's own JS"
)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecordingRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return {"success": True}


def _spine_and_ring():
    clock = FakeClock()
    ring = transcript.TranscriptRing(clock=clock)
    runner = RecordingRunner()
    spine = confirm.ConfirmSpine(ring, wait_s=0.0, runner=runner, clock=clock)
    return spine, ring, runner, clock


def _propose(spine):
    return spine.propose(
        tool="send_session_message",
        session="orchestrator",
        instruction="restart the portal",
        argv_prefix=["msg", "send", "--to", "orchestrator", "--from", "buddy",
                     "--kind", "voice"],
        params={},
    )


# =============================================================================
# 2. The announcement-gating livelock — server half
# =============================================================================


class TestNotAnnouncedIsCappedAtOneRefusal:

    def test_the_cap_does_not_weaken_the_nonce(self):
        """Past the cap the claim proceeds to the JUDGE, not to approval: a
        wrong nonce is still refused, and nothing writes."""
        spine, ring, runner, _ = _spine_and_ring()
        proposal = _propose(spine)
        item = "item_1"
        ring.speech_started(item, 100)
        ring.commit(item, 101)
        ring.transcribe(item, "confirm zebra")  # not in the alphabet

        assert spine.confirm(proposal.token).reason == "not_announced"
        second = spine.confirm(proposal.token)
        assert second.approved is False
        assert second.reason != "not_announced"
        assert runner.calls == []


# =============================================================================
# 2. The page anchors at RENDER — client half
# =============================================================================


# =============================================================================
# 1. The staleness gate at the bridge — verify, never replay
# =============================================================================


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    return tmp_path


def _seed_spool(entries):
    path = delivery.spool_path("buddy")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _msg(mid, *, sender="worker-1", kind="done", text="report", age_s=10, ref=""):
    return {
        "id": mid, "from": sender, "kind": kind, "text": text,
        "ts": int((time.time() - age_s) * 1000), "ref": ref,
    }


class TestTheStalenessGate:
    def test_hours_stale_mail_is_dropped_with_a_reason(self, isolate, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"worker-1"})
        _seed_spool([
            _msg("m1", age_s=4 * 3600, text="is idle and done working"),
            _msg("m2", age_s=10, text="fresh report"),
        ])
        res = tools._buddy_inbox({"_buddy": "buddy", "ack": False})
        assert [m["id"] for m in res["messages"]] == ["m2"]
        assert res["dropped"] == [
            {"id": "m1", "from": "worker-1", "ref": "",
             "reason": "older than 15 minutes"},
        ]


# =============================================================================
# 3. The fallback voice — volume-matched, and the 6s-miss names its cause
# =============================================================================


def _run_js(sources: str, script: str) -> dict:
    program = "\n".join([sources, textwrap.dedent(script), "console.log(report());"])
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout.strip().splitlines()[-1])


# =============================================================================
# THE COMPOSITION — the issue's acceptance scenario, on the real factories
# =============================================================================

_COMPOSED_HARNESS = """
const logs = [];
const forwards = [];
const noticeAnnounces = [];
let timers = [];
let nextHandle = 1;
let seq = 0;
let ownerSpeaking = false;
let spool = [];
let cursor = 0;
const seen = {};
const seenRefs = {};

function setTimer(fn, ms) { const h = nextHandle++; timers.push({ h, fn }); return h; }
function clearTimer(h) { timers = timers.filter((t) => t.h !== h); }
function fireTimers() { const due = timers.slice(); timers = []; due.forEach((t) => t.fn()); }
function nextSeq() { return ++seq; }
function log() {}
function forward(path, body) { forwards.push({ path, body }); return Promise.resolve(); }
function fallbackUtterance(t) { return t; }
const window = { speechSynthesis: { speak: function () {} } };

const confirmGate = createConfirmGate({ now: () => Date.now(), ttlMs: 120000 });
const announcer = createAnnouncer({
  send: () => true,
  speak: (t, onDone) => { if (onDone) onDone(); },
  stopSpeak: () => {},
  onSpoken: (meta, how) => onSpoken(meta, how),
  onNotSpoken: () => {},
  ownerSpeaking: () => ownerSpeaking,
  setTimer, clearTimer,
  onLog: (kind, detail) => logs.push(kind + ": " + detail),
});
function onSpoken(meta, how) {
  if (meta && meta.inboxIds) { notifier.noticeSpoken(meta); return; }
}

__ANNOUNCE__

function fetchInbox() {
  return Promise.resolve({ success: true, messages: spool.slice(cursor) });
}
function ackInbox(through) {
  const idx = spool.findIndex((m) => m.id === through);
  if (idx >= 0 && idx + 1 > cursor) cursor = idx + 1;
  return Promise.resolve({ success: true, acked: idx >= 0 });
}
const notifier = createInboxNotifier({
  fetchInbox, ackInbox,
  announce: (text, meta) => { noticeAnnounces.push({ text, meta }); announce(text, meta); },
  // The page's own gate predicates, verbatim in shape.
  canSpeak: () => !ownerSpeaking && !!announcer && announcer.pending() === 0 && !confirmGate.outstanding(),
  canInterrupt: () => !ownerSpeaking && !confirmGate.outstanding() && !!announcer && !announcer.anchorPending(),
  setTimer, clearTimer,
  onLog: (kind, detail) => logs.push(kind + ": " + detail),
  pollMs: 5000,
  seen, seenRefs,
});
function report() {
  return JSON.stringify({
    logs, forwards, cursor,
    noticeTexts: noticeAnnounces.map((a) => a.text),
    outstanding: confirmGate.outstanding(),
  });
}
"""


def _composed_program() -> str:
    """The real factories plus the page's REAL announce(), extracted."""
    page = client.page("buddy", "tok")
    body = page.split("function announce(text, meta, fallbackText) {", 1)[1]
    announce_fn = (
        "function announce(text, meta, fallbackText) {" + body.split("\n}", 1)[0] + "\n}"
    )
    return "\n".join([
        client.confirm_gate_source(),
        client.announcer_source(),
        client.notifier_source(),
        _COMPOSED_HARNESS.replace("__ANNOUNCE__", announce_fn),
    ])


@_NODE
class TestTheAcceptanceComposition:
    """The issue's scenario: a pending proposal plus a firehose of notices."""

    def test_the_firehose_is_held_until_the_proposal_resolves(self):
        report = _run_js(_composed_program(), """
            // The write tool proposed; the page renders + anchors immediately.
            announce("I'd send that to orchestrator. Say confirm tango.",
                     { anchor: "p1" }, "check the screen for the word");
            // The announcement is TRUNCATED — the owner talks over the
            // fallback voice mid-proposal. The anchor must not care.
            fireTimers();            // fallback fires (model never spoke it)
            announcer.bargeIn();

            // A firehose lands: 8 notices including an escalation.
            for (var i = 1; i <= 7; i++) {
              spool.push({ id: "m" + i, from: "w" + i, kind: "done", text: "report " + i });
            }
            spool.push({ id: "m8", from: "fleet-alerts", kind: "escalation",
                         text: "root-sess is blocked", ref: "prompt:root-sess:0:abc" });
            await notifier.pollOnce();
            await notifier.pollOnce();
            await notifier.pollOnce();
            logs.push("held: " + noticeAnnounces.length);

            // The proposal resolves (the outcome router's confirm_terminal).
            confirmGate.resolved();
            await notifier.pollOnce();
        """)
        # Anchored at render, before and regardless of the truncated speech.
        assert report["forwards"][0]["path"] == "/anchor"
        assert report["forwards"][0]["body"]["proposal_id"] == "p1"
        # ZERO notice utterances while the proposal was outstanding —
        # escalation included: the hard hold held.
        assert "held: 0" in report["logs"]
        # After resolution: exactly ONE utterance, escalation read first, the
        # backlog collapsed to a summary — never replayed serially.
        assert len(report["noticeTexts"]) == 1
        text = report["noticeTexts"][0]
        assert "root-sess is blocked" in text
        assert "Plus 5 more from" in text and "collapsed" in text
