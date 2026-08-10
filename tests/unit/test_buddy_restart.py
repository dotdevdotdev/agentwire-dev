"""Restart semantics for the supervised buddy bridge (#983).

The lifecycle-host wave puts the bridge under a supervisor, which means the
question "what does a restart mid-handshake land in?" stops being hypothetical:
the watchdog can kill and respawn the process at any point, including between a
proposal being anchored and its spoken nonce arriving.

The issue said this is clean "by construction". It is — the confirm spine and
the utterance ring are per-``BuddyBridge`` and nothing in ``confirm.py`` or
``transcript.py`` touches disk — but *by construction* is a claim about code
that is one refactor away from being false, and nothing failed when it became
false. So it is pinned two ways here: the objects a second ``serve()`` hands
out are empty, AND nothing the first run proposed exists anywhere under
``~/.agentwire`` afterwards. The second assertion is the one that survives
someone deciding proposals should be durable.

The acceptance test the issue names is the greet: after a supervised restart,
the next "Start talking" must greet, because a heard greeting is what proves
the write path can approve anything at all (#950/#963). #995 records that the
wires arming that greet — ``pc.ontrack`` among them — have no pin at all and
can be cut with the whole suite staying green. They are executed here, as
themselves, extracted from the page the server actually serves.
"""

import json
import re
import shutil
import subprocess
import urllib.request

import pytest

from agentwire.voice_layer import client, server

# ─────────────────────────────────────────────────────────────
# A real serve() → kill → serve() cycle
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def runs(monkeypatch):
    """Record every BuddyBridge a serve() builds, and clean up the servers.

    Recording the bridge is itself a pin: ``serve()`` constructing a bridge per
    call is the whole mechanism, and a module-level bridge would show up here as
    one object across two runs.
    """
    built = []
    real = server.BuddyBridge

    def recording(*args, **kwargs):
        bridge = real(*args, **kwargs)
        built.append(bridge)
        return bridge

    monkeypatch.setattr(server, "BuddyBridge", recording)
    started = []

    def start():
        httpd, url = server.serve("buddy", port=0)
        started.append(httpd)
        return httpd, url, built[-1]

    def kill(httpd):
        """What the supervisor does: the process goes away. No teardown hook
        runs on the spine, deliberately — that is the case under test."""
        httpd.shutdown()
        httpd.server_close()

    yield start, kill, built
    for httpd in started:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass


def _mid_handshake(bridge):
    """Put *bridge* where a restart is worst: a proposal announced and anchored,
    waiting on a spoken nonce that will never come."""
    bridge.ring.speech_started("item-1", 1)
    bridge.ring.commit("item-1", 2)
    bridge.ring.transcribe("item-1", "send them a note", 2)
    proposal = bridge.spine.propose(
        tool="fleet_msg_send",
        session="agentwire",
        instruction="tell them the PR is up",
        argv_prefix=("agentwire", "msg", "send"),
    )
    bridge.spine.announce(proposal.id, 3)
    assert bridge.spine.pending(), "fixture built no handshake to interrupt"
    return proposal


class TestASupervisedRestartLandsNothingPending:
    def test_the_second_run_gets_a_fresh_spine_and_ring(self, runs):
        start, kill, built = runs
        httpd1, _url1, bridge1 = start()
        proposal = _mid_handshake(bridge1)
        kill(httpd1)

        httpd2, _url2, bridge2 = start()
        assert bridge2 is not bridge1
        assert bridge2.spine is not bridge1.spine
        assert bridge2.ring is not bridge1.ring
        assert bridge2.spine.pending() == []
        assert bridge2.ring.snapshot() == []
        # And the dead run's proposal is not merely invisible — it is unusable.
        verdict = bridge2.spine.confirm(proposal.token)
        assert verdict.approved is False

    def test_the_run_token_is_fresh_so_the_old_page_cannot_keep_talking(self, runs):
        """A restart invalidates the browser tab: the token was minted per run.
        This is why the acceptance path is *reload, then Start talking* — the
        old page's POSTs 401 rather than half-working."""
        start, kill, _built = runs
        httpd1, url1, bridge1 = start()
        kill(httpd1)
        httpd2, url2, bridge2 = start()
        assert bridge2.token != bridge1.token
        assert url1.startswith("http://127.0.0.1:") and url2.startswith("http://127.0.0.1:")

    def test_nothing_from_the_handshake_is_written_to_disk(self, runs, tmp_path):
        """The construction argument, asserted as a fact about the filesystem.

        A spine that started persisting proposals would still pass the
        fresh-object test above (a new run would just LOAD them), so the pin
        that actually holds the guarantee is this one.
        """
        from pathlib import Path

        start, kill, _built = runs
        home = Path.home() / ".agentwire"
        before = _snapshot(home)
        httpd1, _url, bridge1 = start()
        proposal = _mid_handshake(bridge1)
        kill(httpd1)

        after = _snapshot(home)
        new_or_changed = {p: c for p, c in after.items() if before.get(p) != c}
        for path, content in new_or_changed.items():
            assert proposal.token not in content, f"proposal token persisted in {path}"
            assert proposal.nonce not in content, f"nonce persisted in {path}"
            assert "tell them the PR is up" not in content, f"instruction persisted in {path}"


def _snapshot(root) -> dict:
    """Text content of every file under *root* (binary files skipped)."""
    out = {}
    if not root.exists():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            out[str(path)] = path.read_text(errors="replace")
        except OSError:
            continue
    return out


class TestTheRestartedRunServesAGreetArmedPage:
    """The liveness probe end of it, at the HTTP boundary."""

    def test_the_page_served_after_a_restart_carries_the_new_token(self, runs):
        start, kill, _built = runs
        httpd1, _url1, bridge1 = start()
        kill(httpd1)
        _httpd2, url2, bridge2 = start()

        with urllib.request.urlopen(url2, timeout=5) as resp:
            page = resp.read().decode("utf-8")
        assert json.dumps(bridge2.token) in page
        assert bridge1.token not in page

    def test_the_greet_latch_is_page_lifetime_so_a_restart_re_arms_it(self, runs):
        """``greeted`` is never reset by ``stop()`` on purpose — a reconnect must
        not re-greet. A supervisor restart is not a reconnect: it serves a new
        page, and that page starts unlatched. If the latch ever moved server-side
        the buddy would come back from a restart silent, and silence is exactly
        what the greet exists to distinguish from health."""
        start, kill, _built = runs
        httpd1, _url1, _b1 = start()
        kill(httpd1)
        _httpd2, url2, _b2 = start()

        with urllib.request.urlopen(url2, timeout=5) as resp:
            page = resp.read().decode("utf-8")
        assert re.search(r"let\s+greeted\s*=\s*false\s*;", page)
        assert re.search(r"let\s+sessionReady\s*=\s*false\s*;", page)
        assert re.search(r"let\s+audioAttached\s*=\s*false\s*;", page)


# ─────────────────────────────────────────────────────────────
# The greet wires, executed (#995 — these have no other pin)
# ─────────────────────────────────────────────────────────────

pytestmark_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the client's own JS"
)


def _page() -> str:
    return client.page("buddy", "tok")


def _slice(page: str, start_pat: str, end_pat: str, what: str, *, shape: str) -> str:
    """Cut a region out of the served page, failing loudly if the anchor moved.

    Extraction rather than a copy, for the reason ``announcer_source`` gives:
    a test that re-derives its subject proves something about the copy. And a
    silent miss here would be the worst outcome — the wire could be gone and
    this file would still be green, which is the #995 failure repeated by the
    test written to close it.

    "Loudly" has to cover the PARTIAL match too. A start anchor that still hits
    while the end anchor has drifted yields a syntactically broken fragment,
    and what the reader then sees is an opaque node ``SyntaxError`` rather than
    "the anchor moved". So each slice declares the *shape* it must still have —
    chosen to be invariant under the mutations these tests exist to catch, so a
    cut wire fails as a behaviour failure and never as a stale-anchor one.
    """
    start = re.search(start_pat, page)
    assert start, f"anchor for {what} not found — the page moved, this test is stale"
    end = re.search(end_pat, page[start.start():])
    assert end, f"end anchor for {what} not found — the page moved, this test is stale"
    region = page[start.start():start.start() + end.end()]
    assert re.search(shape, region), (
        f"extracted {what} does not have the shape this test assumes — the page "
        f"moved and the extraction is stale, NOT a behaviour failure. Got:\n{region}"
    )
    return region


def _greet_program(*, fire: str) -> str:
    page = _page()
    greet_block = _slice(
        page, r"const GREETING\s*=", r"function maybeGreet\(\)\s*\{[\s\S]*?\n\}",
        "the greeting block",
        # Shape, not behaviour: the declarations and the function's existence.
        # Whether maybeGreet's BODY still greets is what the tests decide.
        shape=r"let greeted[\s\S]*function maybeGreet\(\)\s*\{[\s\S]*\}\s*$",
    )
    ontrack = _slice(
        page, r"pc\.ontrack\s*=", r";\s*\n", "the pc.ontrack wire",
        # An assignment of an arrow function, ending in a statement. True with
        # or without the maybeGreet() call inside it.
        shape=r"^pc\.ontrack\s*=\s*\([\s\S]*=>[\s\S]*;\s*$",
    )
    session_created = _slice(
        page, r'case "session\.created":', r"break;", "the session.created wire",
        shape=r'^case "session\.created":[\s\S]*break;$',
    )
    # `case`/`break` are only legal inside a switch; the body is what is under
    # test, so it is lifted into a function verbatim.
    body = session_created[len('case "session.created":'):-len("break;")]

    return "\n".join([
        "const announced = [];",
        "function announce(text, meta, fallback) { announced.push({ text, meta, fallback }); }",
        "let inboxNotifier = { starts: 0, start() { this.starts++; } };",
        "let audioEl = {};",
        "let pc = {};",
        greet_block,
        ontrack,
        "function fireOntrack() { pc.ontrack({ streams: [{}] }); }",
        f"function fireSessionCreated() {{ {body} }}",
        fire,
        "console.log(JSON.stringify({ announced, greeted, "
        "sessionReady, audioAttached, notifierStarts: inboxNotifier.starts }));",
    ])


def _run_greet(fire: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _greet_program(fire=fire)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytestmark_node
class TestBothGreetWiresAreLoadBearing:
    """Cut either wire and the buddy comes back from a restart silent.

    Before this file, cutting either one left the entire suite green (#995).
    """

    def test_ontrack_alone_does_not_greet(self):
        report = _run_greet("fireOntrack();")
        assert report["audioAttached"] is True
        assert report["announced"] == []
        assert report["greeted"] is False

    def test_session_created_alone_does_not_greet(self):
        report = _run_greet("fireSessionCreated();")
        assert report["sessionReady"] is True
        assert report["announced"] == []
        assert report["greeted"] is False

    def test_the_ontrack_wire_greets_when_it_lands_last(self):
        """THE pin for #995's worst wire, and the ordering is load-bearing.

        ``pc.ontrack``'s ``maybeGreet()`` call only decides anything when
        ontrack arrives SECOND — with the other order, session.created's call
        fires the greeting and the cut is masked. A single "both wires" test in
        the convenient order would have left the wire unpinned while looking
        like it covered it.
        """
        report = _run_greet("fireSessionCreated(); fireOntrack();")
        assert len(report["announced"]) == 1
        assert report["greeted"] is True

    def test_the_session_created_wire_greets_when_it_lands_last(self):
        """The mirror, pinning the other wire's call for the same reason."""
        report = _run_greet("fireOntrack(); fireSessionCreated();")
        assert len(report["announced"]) == 1
        assert report["greeted"] is True

    def test_both_wires_greet_exactly_once(self):
        report = _run_greet("fireOntrack(); fireSessionCreated();")
        assert report["greeted"] is True
        assert len(report["announced"]) == 1
        item = report["announced"][0]
        assert item["text"] == "Hey, I'm listening. What's on your mind?"
        assert item["meta"] == {"greeting": True}
        # The fallback text is NOT the greeting: a fallback-spoken greeting
        # would confirm the browser voice while model audio is dead, and
        # nothing could be approved (#950).
        assert "isn't working" in item["fallback"]

    def test_a_second_pass_does_not_re_greet(self):
        report = _run_greet(
            "fireOntrack(); fireSessionCreated(); fireOntrack(); fireSessionCreated();"
        )
        assert len(report["announced"]) == 1
        assert report["notifierStarts"] == 2  # the notifier wire ran both times

    def test_the_session_created_wire_also_starts_the_inbox_notifier(self):
        """Ordering that the wire encodes: the notifier starts AFTER the greeting
        is queued, so the first inbox tick defers behind it."""
        report = _run_greet("fireOntrack(); fireSessionCreated();")
        assert report["notifierStarts"] == 1
        assert len(report["announced"]) == 1
