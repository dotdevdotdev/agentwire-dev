"""The remaining unpinned browser-event wires in ``client.py`` (#995).

#995's shape: a single line wires a browser event to a code path, and nothing
asserts the wire exists. The tests that look like they cover it assert a
parameter name in a signature, or assert the downstream function behaves —
never that the event handler calls it. Cut the line and the whole suite stays
green.

#993 closed two instances (``utterance.onerror`` / ``utterance.onend``) and
#1001 closed the worst one (``pc.ontrack`` → ``maybeGreet()``, pinned in
``test_buddy_restart.py``). Four were left, and they are what this file pins:

    client.py  dc.addEventListener("open",  …)   status → "listening", stop enabled
    client.py  dc.addEventListener("close", …)   status → "closed"
    client.py  $start.addEventListener("click", start)
    client.py  $stop.addEventListener("click", stop)

Same technique as ``test_buddy_restart.py``, and deliberately the SAME
extractor (:func:`tests.page_slice.page_slice`) rather than a second idiom —
including its guard against a partial anchor match, which otherwise degrades
to an opaque node ``SyntaxError`` instead of saying "the anchor moved".

Why these two pairs are worth a node run rather than a substring assertion.
The substring half is here too (``TestTheWiresArePresentAtAll``) and it is what
gives the honest message when a whole line is deleted — but a substring cannot
tell ``("click", start)`` from ``("click", stop)``, and it cannot tell a status
wire that fires from one that sets the wrong thing. Both of those are live
mutations: the start/stop pair is the owner's only entry and exit point, and a
swapped pair makes the page unusable while every existing test stays green.

What this does NOT establish: that the browser ever fires these events. That
half is not in reach of a unit harness and is not what #995 is about — the
claim under test is that the page WIRES them, which is exactly what a mutation
can silently remove.
"""

import json
import shutil
import subprocess

import pytest

from agentwire.voice_layer import client
from tests.page_slice import page_slice

#: Applied per CLASS rather than per module: the presence checks at the bottom
#: are pure string work and are the half that must still run on a machine
#: without node.
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the client's own JS"
)


def _page() -> str:
    return client.page("buddy", "tok")


def _run(program: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout.strip().splitlines()[-1])


# ─────────────────────────────────────────────────────────────
# The data-channel status wires
# ─────────────────────────────────────────────────────────────


def _status_program(*, fire: str) -> str:
    page = _page()
    open_wire = page_slice(
        page, r'dc\.addEventListener\("open"', r";\s*\n", "the dc open wire",
        # Shape, not behaviour: a listener registration ending in a statement.
        # True with or without the setStatus/$stop lines inside it, which is
        # what keeps a cut wire failing as a behaviour failure.
        shape=r'^dc\.addEventListener\("open",\s*\([\s\S]*=>[\s\S]*\);\s*$',
    )
    close_wire = page_slice(
        page, r'dc\.addEventListener\("close"', r";\s*\n", "the dc close wire",
        shape=r'^dc\.addEventListener\("close",\s*\([\s\S]*=>[\s\S]*\);\s*$',
    )
    return "\n".join([
        "const statuses = [];",
        "const $stop = { disabled: true };",
        "function setStatus(text) { statuses.push(text); }",
        "const handlers = {};",
        "const dc = { addEventListener: (name, fn) => { handlers[name] = fn; } };",
        open_wire,
        close_wire,
        'function fireOpen() { handlers["open"](); }',
        'function fireClose() { handlers["close"](); }',
        fire,
        "console.log(JSON.stringify({ statuses, stopDisabled: $stop.disabled, "
        "wired: Object.keys(handlers).sort() }));",
    ])


@needs_node
class TestTheDataChannelStatusWires:
    """Connection state is the one thing the owner can only learn from the page.

    Screenless makes the *absence* of these wires indistinguishable from a
    connection that is merely slow: the buddy says nothing either way. So the
    status text is the whole signal, and nothing asserted it was wired.
    """

    def test_both_events_are_registered(self):
        report = _run(_status_program(fire=""))
        assert report["wired"] == ["close", "open"]

    def test_the_open_wire_says_listening_and_enables_stop(self):
        report = _run(_status_program(fire="fireOpen();"))
        assert report["statuses"] == ["listening"]
        # The stop button is the owner's exit. Enabled by the OPEN event, not by
        # start() — a page that connects and leaves stop disabled traps them.
        assert report["stopDisabled"] is False

    def test_the_close_wire_says_closed(self):
        report = _run(_status_program(fire="fireClose();"))
        assert report["statuses"] == ["closed"]

    def test_a_close_does_not_enable_stop(self):
        """The two handlers are independent, and the assertion above would pass
        for a page that had wired the open body to `close`."""
        report = _run(_status_program(fire="fireClose();"))
        assert report["stopDisabled"] is True

    def test_a_reconnect_sequence_ends_on_the_last_event(self):
        report = _run(_status_program(fire="fireOpen(); fireClose(); fireOpen();"))
        assert report["statuses"] == ["listening", "closed", "listening"]


# ─────────────────────────────────────────────────────────────
# The owner's entry and exit points
# ─────────────────────────────────────────────────────────────


def _click_program(*, fire: str) -> str:
    page = _page()
    start_wire = page_slice(
        page, r'\$start\.addEventListener\("click"', r";", "the start click wire",
        shape=r'^\$start\.addEventListener\("click",\s*\w+\);$',
    )
    stop_wire = page_slice(
        page, r'\$stop\.addEventListener\("click"', r";", "the stop click wire",
        shape=r'^\$stop\.addEventListener\("click",\s*\w+\);$',
    )
    return "\n".join([
        "const fired = [];",
        'function start() { fired.push("start"); }',
        'function stop() { fired.push("stop"); }',
        "const startHandlers = {}, stopHandlers = {};",
        "const $start = { addEventListener: (n, fn) => { startHandlers[n] = fn; } };",
        "const $stop = { addEventListener: (n, fn) => { stopHandlers[n] = fn; } };",
        start_wire,
        stop_wire,
        'function clickStart() { startHandlers["click"](); }',
        'function clickStop() { stopHandlers["click"](); }',
        fire,
        "console.log(JSON.stringify({ fired, startEvents: Object.keys(startHandlers), "
        "stopEvents: Object.keys(stopHandlers) }));",
    ])


@needs_node
class TestTheStartAndStopButtons:
    """The owner's entry and exit points, which nothing asserted at all.

    A swap here — ``$start`` wired to ``stop`` — is the mutation a presence
    check cannot see, and it costs the whole page: Start talking tears down a
    session that was never built, and the buddy is silent for a reason the
    owner has no screen to read.
    """

    def test_both_buttons_listen_for_a_click(self):
        report = _run(_click_program(fire=""))
        assert report["startEvents"] == ["click"]
        assert report["stopEvents"] == ["click"]

    def test_clicking_start_calls_start_and_nothing_else(self):
        report = _run(_click_program(fire="clickStart();"))
        assert report["fired"] == ["start"]

    def test_clicking_stop_calls_stop_and_nothing_else(self):
        report = _run(_click_program(fire="clickStop();"))
        assert report["fired"] == ["stop"]

    def test_a_full_session_is_start_then_stop(self):
        report = _run(_click_program(fire="clickStart(); clickStop();"))
        assert report["fired"] == ["start", "stop"]


class TestTheWiresArePresentAtAll:
    """The honest message when a whole line is deleted.

    The node tests above go red on a deleted wire too — but through
    ``page_slice``'s "the page moved, this test is stale" assertion, which
    reads as a maintenance problem rather than as the defect. These say the
    true thing, and they are the cheap half that keeps working if node is
    unavailable (every test above skips without it).
    """

    @pytest.mark.parametrize("wire", [
        'dc.addEventListener("open"',
        'dc.addEventListener("close"',
        '$start.addEventListener("click", start)',
        '$stop.addEventListener("click", stop)',
    ])
    def test_the_wire_is_in_the_served_page(self, wire):
        assert wire in _page(), f"the wire `{wire}` is gone from the page (#995)"
