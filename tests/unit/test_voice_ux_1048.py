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


def _owner_confirms(ring, proposal, seq_start=100):
    item = f"item_{seq_start}"
    ring.speech_started(item, seq_start)
    ring.commit(item, seq_start + 1)
    ring.transcribe(item, f"confirm {confirm.spoken_nonce(proposal.nonce)}")


# =============================================================================
# 2. The announcement-gating livelock — server half
# =============================================================================


class TestNotAnnouncedIsCappedAtOneRefusal:
    def test_the_second_confirm_on_an_unannounced_proposal_is_judged(self):
        """One "hang on" is a correction; a second is the livelock. After the
        cap the judge runs — and the nonce, which no refusal ever utters, is
        still what approves."""
        spine, ring, runner, _ = _spine_and_ring()
        proposal = _propose(spine)
        _owner_confirms(ring, proposal)

        first = spine.confirm(proposal.token)
        assert first.approved is False
        assert first.reason == "not_announced"
        assert runner.calls == []

        second = spine.confirm(proposal.token)
        assert second.approved is True
        assert len(runner.calls) == 1

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

    def test_the_cap_is_per_proposal(self):
        """A second proposal gets its own single "hang on" — the cap is state
        on the proposal, not on the spine."""
        spine, ring, runner, _ = _spine_and_ring()
        first = _propose(spine)
        _owner_confirms(ring, first, seq_start=100)
        assert spine.confirm(first.token).reason == "not_announced"
        assert spine.confirm(first.token).approved is True

        second = _propose(spine)
        _owner_confirms(ring, second, seq_start=200)
        assert spine.confirm(second.token).reason == "not_announced"
        assert spine.confirm(second.token).approved is True

    def test_the_deny_side_grammar_is_untouched(self):
        """#976's contract: a denial on an ANNOUNCED proposal still refuses,
        cap or no cap."""
        spine, ring, runner, _ = _spine_and_ring()
        proposal = _propose(spine)
        spine.announce(proposal.id, 50)
        item = "item_1"
        ring.speech_started(item, 100)
        ring.commit(item, 101)
        ring.transcribe(
            item, f"no wait don't confirm {confirm.spoken_nonce(proposal.nonce)}"
        )
        verdict = spine.confirm(proposal.token)
        assert verdict.approved is False
        assert runner.calls == []


# =============================================================================
# 2. The page anchors at RENDER — client half
# =============================================================================


class TestTheProposalAnchorsAtRender:
    def test_announce_anchors_before_the_announcer_can_fail(self):
        page = client.page("buddy", "tok")
        body = page.split("function announce(text, meta, fallbackText) {", 1)[1]
        body = body.split("\n}", 1)[0]
        assert body.index('log("buddy", text, "buddy");') \
            < body.index("confirmGate.anchored();") \
            < body.index("announcer.announce(")
        assert 'forward("/anchor", { proposal_id: meta.anchor, seq: nextSeq() });' in body

    def test_the_anchor_forward_no_longer_waits_on_speech(self):
        """Exactly one /anchor forward on the page, and it lives in announce()
        — the spoken/not-spoken verdicts can no longer starve it."""
        page = client.page("buddy", "tok")
        assert page.count('forward("/anchor"') == 1
        onspoken = page.split("function onSpoken(meta, how)", 1)[1]
        onspoken = onspoken.split("function onNotSpoken", 1)[0]
        assert 'forward("/anchor"' not in onspoken
        assert "confirmGate.anchored" not in onspoken


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

    def test_a_dead_prompt_alert_is_verified_against_the_marker(
        self, isolate, monkeypatch
    ):
        """The re-raised no-parent alert carries its prompt identity; at speak
        time the MARKER decides — answered or changed means dropped, live and
        unchanged means spoken."""
        from agentwire import prompt_router

        monkeypatch.setattr(inbox, "live_sessions", lambda: {"root-sess"})
        markers = {("root-sess", 0): {"hash": "abc123"}}
        monkeypatch.setattr(
            prompt_router, "read_marker", lambda s, p: markers.get((s, p))
        )
        _seed_spool([
            _msg("m1", sender="fleet-alerts", kind="escalation",
                 ref="prompt:root-sess:0:abc123", text="blocked prompt"),
            _msg("m2", sender="fleet-alerts", kind="escalation",
                 ref="prompt:root-sess:1:def456", text="already answered"),
        ])
        res = tools._buddy_inbox({"_buddy": "buddy", "ack": False})
        assert [m["id"] for m in res["messages"]] == ["m1"]
        assert res["dropped"][0]["id"] == "m2"
        assert res["dropped"][0]["reason"] == "prompt no longer live"

    def test_idle_news_about_a_torn_down_session_is_not_news(
        self, isolate, monkeypatch
    ):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"alive-sess"})
        _seed_spool([
            _msg("m1", sender="fleet-activity",
                 ref="activity:session_idle:dead-sess",
                 text="dead-sess is idle and done working"),
            _msg("m2", sender="fleet-activity",
                 ref="activity:session_idle:alive-sess",
                 text="alive-sess is idle and done working"),
        ])
        res = tools._buddy_inbox({"_buddy": "buddy", "ack": False})
        assert [m["id"] for m in res["messages"]] == ["m2"]
        assert res["dropped"][0]["reason"] == "subject session gone"

    def test_a_long_gone_sender_is_dropped_but_a_just_reaped_one_speaks(
        self, isolate, monkeypatch
    ):
        """Both halves priced: a worker reaped seconds after reporting still
        deserves its report spoken; one gone for minutes is history."""
        monkeypatch.setattr(inbox, "live_sessions", lambda: set())
        _seed_spool([
            _msg("m1", sender="old-worker", age_s=600),
            _msg("m2", sender="new-worker", age_s=30),
        ])
        res = tools._buddy_inbox({"_buddy": "buddy", "ack": False})
        assert [m["id"] for m in res["messages"]] == ["m2"]
        assert res["dropped"][0]["reason"] == "sender session gone"

    def test_re_raises_in_one_batch_collapse_to_the_newest(self, isolate, monkeypatch):
        from agentwire import prompt_router

        monkeypatch.setattr(inbox, "live_sessions", lambda: {"root-sess"})
        monkeypatch.setattr(
            prompt_router, "read_marker", lambda s, p: {"hash": "abc123"}
        )
        _seed_spool([
            _msg("m1", sender="fleet-alerts", ref="prompt:root-sess:0:abc123"),
            _msg("m2", sender="fleet-alerts", ref="prompt:root-sess:0:abc123"),
            _msg("m3", sender="fleet-alerts", ref="prompt:root-sess:0:abc123"),
        ])
        res = tools._buddy_inbox({"_buddy": "buddy", "ack": False})
        assert [m["id"] for m in res["messages"]] == ["m3"]
        assert sorted(d["id"] for d in res["dropped"]) == ["m1", "m2"]
        assert all(d["reason"] == "superseded by a newer re-raise"
                   for d in res["dropped"])

    def test_a_tmux_outage_is_not_a_verdict(self, isolate, monkeypatch):
        """live_sessions() None means tmux was unreachable — gone-checks
        abstain rather than declaring every sender dead."""
        monkeypatch.setattr(inbox, "live_sessions", lambda: None)
        _seed_spool([
            _msg("m1", sender="worker-1", age_s=600),
            _msg("m2", sender="fleet-activity",
                 ref="activity:session_idle:whoever", age_s=30),
        ])
        res = tools._buddy_inbox({"_buddy": "buddy", "ack": False})
        assert [m["id"] for m in res["messages"]] == ["m1", "m2"]
        assert res["dropped"] == []

    def test_the_full_history_read_is_unfiltered(self, isolate, monkeypatch):
        """unread_only=false is the owner asking for the record, not the
        volunteering path — it gets everything."""
        monkeypatch.setattr(inbox, "live_sessions", lambda: set())
        _seed_spool([_msg("m1", age_s=4 * 3600)])
        res = tools._buddy_inbox({"_buddy": "buddy", "ack": False,
                                  "unread_only": False})
        assert [m["id"] for m in res["messages"]] == ["m1"]
        assert res["dropped"] == []

    def test_producers_stamp_the_stable_identity(self):
        """The refs the gate keys on are the ones the producers actually write
        — pinned at the source so neither side can drift alone."""
        import inspect

        from agentwire import fleet_activity, prompt_router

        alert = inspect.getsource(prompt_router._alert_no_parent)
        assert 'ref=f"prompt:{session}:{pane_index}:{info.content_hash()}"' in alert
        note = inspect.getsource(fleet_activity.note)
        assert 'ref=f"activity:{event}:{subject}"' in note


# =============================================================================
# 3. The fallback voice — volume-matched, and the 6s-miss names its cause
# =============================================================================


class TestTheFallbackVoiceIsNormalized:
    def test_the_constants_are_pinned_and_wired(self):
        page = client.page("buddy", "tok")
        assert "const FALLBACK_VOICE_VOLUME = 0.4;" in page
        assert "const FALLBACK_VOICE_RATE = 1.0;" in page
        assert "utterance.volume = FALLBACK_VOICE_VOLUME;" in page
        assert "utterance.rate = FALLBACK_VOICE_RATE;" in page
        # BOTH speechSynthesis paths route through the normalized constructor:
        # the announcer's speak dep and announce()'s no-announcer fallback.
        assert page.count("fallbackUtterance(") >= 3  # def + 2 call sites
        assert page.count("new SpeechSynthesisUtterance(") == 1  # inside it


@_NODE
class TestTheMissLogNamesItsCause:
    def test_a_cancelled_announcement_miss_reads_interrupt_correlated(self):
        report = _run_js(
            client.announcer_source() + _MINI_ANNOUNCER_HARNESS,
            """
            announcer.announce("say confirm tango to approve");
            announcer.onResponseCreated();
            announcer.onResponseCancelled();   // a notice interrupt killed it
            fireTimers();                       // the 6s miss
            """,
        )
        miss = [line for line in report["logs"] if "no spoken confirmation" in line]
        assert len(miss) == 1
        assert "1 response(s) cancelled while this was pending" in miss[0]
        assert "interrupt-correlated" in miss[0]

    def test_an_uninterrupted_miss_carries_no_false_correlation(self):
        report = _run_js(
            client.announcer_source() + _MINI_ANNOUNCER_HARNESS,
            """
            announcer.announce("say confirm tango to approve");
            fireTimers();
            """,
        )
        miss = [line for line in report["logs"] if "no spoken confirmation" in line]
        assert len(miss) == 1
        assert "interrupt-correlated" not in miss[0]


_MINI_ANNOUNCER_HARNESS = """
const logs = [];
let timers = [];
let nextHandle = 1;
const announcer = createAnnouncer({
  send: () => true,
  speak: (t, onDone) => { if (onDone) onDone(); },
  onLog: (kind, detail) => logs.push(kind + ": " + detail),
  setTimer: (fn, ms) => { const h = nextHandle++; timers.push({ h, fn }); return h; },
  clearTimer: (h) => { timers = timers.filter((t) => t.h !== h); },
});
function fireTimers() {
  const due = timers.slice(); timers = [];
  due.forEach((t) => t.fn());
}
function report() { return JSON.stringify({ logs }); }
"""


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

    def test_a_re_raise_of_a_heard_identity_never_speaks_again(self):
        report = _run_js(_composed_program(), """
            spool.push({ id: "m1", from: "fleet-alerts", kind: "escalation",
                         text: "root-sess is blocked", ref: "prompt:root-sess:0:abc" });
            await notifier.pollOnce();
            fireTimers();                  // the fallback voice says it
            await Promise.resolve();       // let the ack settle

            // The detector re-raises the SAME prompt an hour later: new id,
            // same identity.
            spool.push({ id: "m2", from: "fleet-alerts", kind: "escalation",
                         text: "root-sess is blocked", ref: "prompt:root-sess:0:abc" });
            await notifier.pollOnce();
            await notifier.pollOnce();

            // A genuinely CHANGED prompt is a new identity and speaks.
            spool.push({ id: "m3", from: "fleet-alerts", kind: "escalation",
                         text: "root-sess asks something new", ref: "prompt:root-sess:0:def" });
            await notifier.pollOnce();
            fireTimers();
            await Promise.resolve();
        """)
        texts = report["noticeTexts"]
        assert len(texts) == 2
        assert "root-sess is blocked" in texts[0]
        assert "something new" in texts[1]
        # The deduped re-raise is on the record, not silently missing.
        assert any("re-raised notice deduped" in line for line in report["logs"])
        # And it is ACKABLE: the changed-prompt notice's ack walks past it.
        assert report["cursor"] == 3

    def test_stale_drops_are_logged_once_not_per_tick(self):
        report = _run_js(_composed_program(), """
            const dropped = [{ id: "m9", from: "fleet-activity",
                               reason: "subject session gone" }];
            let fetchInboxOverride = () => Promise.resolve(
              { success: true, messages: [], dropped: dropped });
            // Re-wire fetch through the override for this scenario.
            const n2 = createInboxNotifier({
              fetchInbox: () => fetchInboxOverride(),
              ackInbox,
              announce: (text, meta) => noticeAnnounces.push({ text, meta }),
              canSpeak: () => true, canInterrupt: () => false,
              setTimer, clearTimer,
              onLog: (kind, detail) => logs.push(kind + ": " + detail),
              pollMs: 5000, seen: {}, seenRefs: {},
            });
            await n2.pollOnce();
            await n2.pollOnce();
            await n2.pollOnce();
        """)
        drop_lines = [line for line in report["logs"] if "stale notice dropped" in line]
        assert len(drop_lines) == 1
        assert "subject session gone" in drop_lines[0]
        assert report["noticeTexts"] == []
