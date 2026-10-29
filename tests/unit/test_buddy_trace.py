"""#1191 — the buddy's durable per-run trace: writer, redaction, bridge, page, CLI.

#1190 stalled with nothing to read afterwards. These pin the properties that
make the trace usable for that diagnosis: every line is ordered and
attributable, nothing secret is ever written, the writer can never break the
live call, the page's batcher never blocks or loops, and an agent can pull the
stalls out without the owner.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agentwire import buddy_cli
from agentwire.voice_layer import client, server, trace


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

def test_every_line_is_ordered_and_attributable(tmp_path):
    log = trace.TraceLog(tmp_path, run_id="r1", clock=lambda: 12.5)
    log.record("bridge", "tool.start", {"name": "fleet_sessions"})
    log.record("client", "rt.response.done", {"status": "failed"}, ts=99)
    first, second = _lines(log.path)
    assert first == {"ts": 12500, "run": "r1", "seq": 1, "source": "bridge",
                     "kind": "tool.start", "fields": {"name": "fleet_sessions"}}
    assert (second["seq"], second["ts"], second["source"]) == (2, 99, "client")


def test_the_cap_writes_one_marker_then_drops(tmp_path):
    log = trace.TraceLog(tmp_path, run_id="r", max_bytes=400)
    results = [log.record("bridge", "x", {"n": i}) for i in range(20)]
    lines = _lines(log.path)
    assert lines[-1]["kind"] == "trace.capped"
    assert [line["kind"] for line in lines].count("trace.capped") == 1
    assert results[-1] is False and log.path.stat().st_size <= 400 + 200


def test_old_runs_are_pruned_on_open(tmp_path):
    for i in range(5):
        (tmp_path / f"2026010{i}-000000-aaaaaa.jsonl").write_text("{}\n")
    log = trace.TraceLog(tmp_path, run_id="20260201-000000-bbbbbb", keep_runs=3)
    log.record("bridge", "x")
    assert [p.stem for p in trace.list_runs(tmp_path)] == [
        "20260201-000000-bbbbbb", "20260104-000000-aaaaaa", "20260103-000000-aaaaaa",
    ]


def test_record_never_raises(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    log = trace.TraceLog(blocker, run_id="r")
    assert log.record("bridge", "x", {"bad": object()}) is False


def test_nothing_secret_reaches_the_file(tmp_path):
    secrets_ = {
        "api": "sk-proj-AAAAAAAAAAAAAAAA",
        "ephemeral": "ek_68f0BBBBBBBBBBBB",
        "bearer": "CCCCCCCCCCCCCCCC",
        "run_token": "DDDDDDDDDDDDDDDD",
        "confirm": "EEEEEEEEEEEEEEEE",
        "audio": "FFFFFFFFFFFFFFFF",
        "untrusted": "GGGGGGGGGGGGGGGG",
    }
    log = trace.TraceLog(tmp_path, run_id="r")
    log.record("client", "log", {"text": f"error body mentions {secrets_['api']}"})
    log.record("bridge", "mint", {"client_secret": secrets_["ephemeral"]})
    log.record("client", "log", {"text": f"Authorization: Bearer {secrets_['bearer']}"})
    log.record("bridge", "x", {"token": secrets_["run_token"]})
    log.record("client", "log", {
        "text": 'send_session_message {"confirm_token":"%s"}' % secrets_["confirm"]})
    log.record("client", "rt.x", {"audio": secrets_["audio"]})
    log.record("bridge", "tool.end", {
        "untrusted_content": [{"title": "x" * 200 + secrets_["untrusted"]}]})
    written = log.path.read_text()
    leaked = [name for name, value in secrets_.items() if value in written]
    assert not leaked, leaked
    assert "[untrusted]" in written


def test_long_strings_are_truncated_and_say_so(tmp_path):
    log = trace.TraceLog(tmp_path, run_id="r")
    log.record("client", "log", {"text": "y" * 5000})
    text = _lines(log.path)[0]["fields"]["text"]
    assert len(text) < 500 and "truncated 4600 chars" in text


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge_http(tmp_path):
    bridge = server.BuddyBridge(
        "buddy", "tok", roster=lambda: "", tracer=trace.TraceLog(tmp_path, run_id="r"),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._handler_factory(bridge))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    port = httpd.server_address[1]

    def post(path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    yield bridge, post
    httpd.shutdown()
    httpd.server_close()


def test_client_batches_land_in_the_run_file(bridge_http):
    bridge, post = bridge_http
    status, body = post("/trace", {"events": [
        {"ts": 5, "kind": "rt.response.done", "fields": {"status": "failed"}},
        {"kind": 7},  # malformed: skipped, not fatal
        {"ts": 6, "kind": "state", "fields": {"responseActive": True}},
    ]})
    assert (status, body["written"]) == (200, 2)
    kinds = [(e["source"], e["kind"]) for e in _lines(bridge.trace.path)]
    assert kinds == [("client", "rt.response.done"), ("client", "state")]


def test_a_raising_route_is_a_traced_500_not_a_dropped_connection(bridge_http):
    bridge, post = bridge_http
    status, body = post("/utterance", [1])  # a list body: .get() raises
    assert status == 500 and body["success"] is False
    kinds = [e["kind"] for e in _lines(bridge.trace.path)]
    assert "bridge.exception" in kinds and "http.error" in kinds


def test_tool_calls_are_timed_with_their_outcome(tmp_path, monkeypatch):
    bridge = server.BuddyBridge(
        "buddy", "tok", roster=lambda: "", tracer=trace.TraceLog(tmp_path, run_id="r"),
    )
    monkeypatch.setattr(server.tools, "dispatch", lambda name, args, buddy, spine: {
        "success": False, "reason": "pending_transcript", "must_speak": True,
        "confirm_terminal": False, "say": "hang on",
    })
    bridge.tool_call({"name": "send_session_message", "arguments": {"confirm_token": "SECRET1"}})
    start, end = _lines(bridge.trace.path)
    assert (start["kind"], end["kind"]) == ("tool.start", "tool.end")
    assert end["fields"]["reason"] == "pending_transcript"
    assert isinstance(end["fields"]["ms"], int)
    assert "SECRET1" not in bridge.trace.path.read_text()


# ---------------------------------------------------------------------------
# The page's batcher, under node
# ---------------------------------------------------------------------------

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the page JS")


def _run_tracer(script: str) -> dict:
    program = "\n".join([
        client.tracer_source(),
        "const posts = [];",
        "let timers = [];",
        "const setTimer = (fn) => { timers.push(fn); return timers.length; };",
        "const runTimers = async () => { while (timers.length) { const t = timers; timers = []; t.forEach((f) => f()); await Promise.resolve(); await Promise.resolve(); } };",
        "let mode = 'ok';",
        "let release = null;",
        "function post(batch) { posts.push(batch); if (mode === 'throw') throw new Error('boom'); if (mode === 'reject') return Promise.reject(new Error('down')); if (mode === 'hold') return new Promise((r) => { release = r; }); return Promise.resolve(); }",
        "const t = createTracer({ post, now: () => 1, setTimer, maxBatch: 3, maxQueue: 5, maxField: 50 });",
        "(async () => {",
        script,
        "console.log(JSON.stringify({ posts, pending: t.pending() }));",
        "})();",
    ])
    out = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_tracer_batches_and_bounds_each_post():
    r = _run_tracer("for (let i = 0; i < 7; i++) t.event('e', { i }); await runTimers();")
    assert [len(b) for b in r["posts"]] == [3, 3, 1]
    assert [e["fields"]["i"] for b in r["posts"] for e in b] == list(range(7))


@needs_node
def test_a_failing_post_drops_its_batch_and_the_trace_keeps_flowing():
    r = _run_tracer(
        "mode = 'throw'; t.event('a'); await runTimers();"
        "mode = 'reject'; t.event('b'); await runTimers();"
        "mode = 'ok'; t.event('c'); await runTimers();"
    )
    assert [[e["kind"] for e in b] for b in r["posts"]] == [["a"], ["b"], ["c"]]
    assert r["pending"] == 0


@needs_node
def test_overflow_is_counted_never_silent():
    # A post held in flight blocks the pipe; the queue (max 5) then fills.
    r = _run_tracer(
        "mode = 'hold'; t.event('a'); t.event('b'); t.event('c');"
        "for (let i = 0; i < 9; i++) t.event('more');"
        "mode = 'ok'; release(); await runTimers(); await runTimers();"
    )
    kinds = [e["kind"] for b in r["posts"] for e in b]
    dropped = [e for b in r["posts"] for e in b if e["kind"] == "trace.dropped"]
    assert dropped and dropped[0]["fields"]["count"] == 4, kinds


@needs_node
def test_fields_are_clipped_so_a_batch_fits_the_bridge_cap():
    r = _run_tracer("t.event('log', { text: 'z'.repeat(100000), obj: { big: 'q'.repeat(9999) } }); await runTimers();")
    fields = r["posts"][0][0]["fields"]
    assert len(fields["text"]) < 70 and fields["text"].endswith("[clipped]")
    assert isinstance(fields["obj"], str) and len(fields["obj"]) < 70


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------

def _args(**kw):
    base = dict(list=False, run=None, stalls=False, tail=False, lines=200, json=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_stalls_are_pulled_from_every_run(capsys):
    old = trace.TraceLog(run_id="20260101-000000-aaaaaa")
    old.record("client", "stall", {"why": "no response after transcript"})
    old.record("client", "state", {})
    new = trace.TraceLog(run_id="20260102-000000-bbbbbb")
    new.record("client", "dc.close", {"pc": "failed"})
    new.record("client", "log", {"text": "hi"})

    assert buddy_cli.cmd_buddy_log(_args(stalls=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(e["run"], e["kind"]) for e in out["events"]] == [
        ("20260101-000000-aaaaaa", "stall"), ("20260102-000000-bbbbbb", "dc.close"),
    ]

    assert buddy_cli.cmd_buddy_log(_args(run="latest")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["runs"] == ["20260102-000000-bbbbbb"]
    assert [e["kind"] for e in out["events"]] == ["dc.close", "log"]


def test_an_unknown_run_fails_loudly(capsys):
    assert buddy_cli.cmd_buddy_log(_args(run="nope")) == 1
    assert json.loads(capsys.readouterr().out)["success"] is False


# ---------------------------------------------------------------------------
# The page's wiring — the real served script, under tests/page_harness.py
# ---------------------------------------------------------------------------

def _page(driver: str) -> dict:
    from tests.page_harness import run_page
    return run_page(driver)


@needs_node
def test_the_page_traces_server_events_closes_and_create_decisions():
    r = _page("""
    await connect();
    await modelResponds({ text: "Hey, I'm listening. What's on your mind?" });
    toolHandler = () => ({ success: true, sessions: [] });
    await modelResponds({ calls: [{ name: "fleet_sessions" }] });
    serverEvent({ type: "response.output_audio.delta", delta: "QUFBQUFBQUFBQUFB" });
    serverEvent({ type: "response.done", response: { id: "rf", status: "failed",
      status_details: { type: "failed", error: { code: "insufficient_quota" } }, output: [] } });
    await settle();
    __dc.close();
    await drainTrace();
    result = { traces: bridge.traces };
    """)
    by_kind = {}
    for e in r["traces"]:
        by_kind.setdefault(e["kind"], []).append(e["fields"])
    failed = [f for f in by_kind["rt.response.done"] if f.get("status") == "failed"]
    assert failed and failed[0]["status_details"]["error"]["code"] == "insufficient_quota"
    assert "rt.response.output_audio.delta" not in by_kind
    assert "QUFBQUFBQUFBQUFB" not in json.dumps(r["traces"])
    assert by_kind["dc.close"]
    assert {"by": "announcer"}.items() <= by_kind["response.create"][0].items()
    batch = [f for f in by_kind["response.create"] if f["by"] == "batch"]
    assert batch and batch[0]["sent"] is True and batch[0]["why"]
    assert [f["decision"] for f in by_kind["batch.decision"]] == ["create"]
    end = by_kind["tool.end"][0]
    assert end["name"] == "fleet_sessions" and isinstance(end["ms"], int)


@needs_node
def test_a_declined_create_says_why():
    r = _page("""
    await connect();
    await modelResponds({ text: "Hey, I'm listening. What's on your mind?" });
    // A VAD response is already in flight when the batch's outputs land.
    serverEvent({ type: "response.created", response: { id: "vad" } });
    serverEvent({ type: "response.done", response: { id: "rc", status: "completed",
      output: [{ type: "function_call", name: "fleet_sessions", call_id: "c1", arguments: "{}" }] } });
    serverEvent({ type: "response.created", response: { id: "vad2" } });
    await settle();
    await drainTrace();
    result = { creates: bridge.traces.filter((e) => e.kind === "response.create").map((e) => e.fields) };
    """)
    batch = [c for c in r["creates"] if c["by"] == "batch"]
    assert batch == [{**batch[0], "sent": False, "declined": "responseActive"}]


@needs_node
def test_state_snapshots_are_periodic_and_carry_the_state_machine():
    r = _page("""
    await connect();
    await advance(16000);
    await drainTrace();
    result = { periodic: bridge.traces.filter((e) => e.kind === "state" && e.fields.why === "periodic").map((e) => e.fields) };
    """)
    assert r["periodic"], "no periodic snapshot within one interval"
    snap = r["periodic"][0]
    want = {"responseActive", "ownerSpeaking", "confirmOutstanding", "confirmRemainingMs",
            "announcerPending", "anchorPending", "dc", "pc"}
    assert want <= snap.keys(), want - snap.keys()


@needs_node
def test_window_errors_and_rejections_are_traced_and_shown():
    r = _page("""
    __winHandlers.error.forEach((f) => f({ message: "boom", filename: "x.js", lineno: 3, colno: 4, error: new Error("boom") }));
    __winHandlers.unhandledrejection.forEach((f) => f({ reason: new Error("lost") }));
    await drainTrace();
    result = { kinds: bridge.traces.map((e) => e.kind), rows: __els.log.children.length };
    """)
    assert {"window.error", "unhandledrejection"} <= set(r["kinds"])
    assert r["rows"] == 2
