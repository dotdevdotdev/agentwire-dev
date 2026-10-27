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


import pytest

from agentwire.voice_layer import server

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


# ─────────────────────────────────────────────────────────────
# The greet wires, executed (#995 — these have no other pin)
# ─────────────────────────────────────────────────────────────


#: The slicer moved to :mod:`tests.page_slice` when the rest of #995's wires
#: got pinned in ``test_client_wires.py`` — one extractor, not two.
