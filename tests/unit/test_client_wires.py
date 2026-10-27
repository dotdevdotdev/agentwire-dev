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


#: The shape each dc status wire must still have, and it is doing more work
#: than it looks. The end anchor is "the first ``;`` at end of line", which a
#: reflowed handler — formatting only, wire intact — satisfies at the FIRST
#: statement of the body, yielding a syntactically broken fragment. A loose
#: shape (``=>[\s\S]*\);``) matches that fragment, because the ``);`` of the
#: truncated call inside it stands in for the listener's own. The reader then
#: gets an opaque node ``SyntaxError`` instead of "the anchor moved" — which is
#: the exact degradation #995 names by name, reproduced by the test written to
#: close it.
#:
#: So the shape demands the arrow function's own terminator: either a BALANCED
#: brace body with nothing nested (``{[^{}]*}``) or a single expression with no
#: statement break in it, and then the call's ``);``. A truncated fragment has
#: neither. Still invariant under the mutations these tests exist to catch — an
#: emptied body is ``{}``, which matches — so a cut wire fails as a behaviour
#: failure and never as a stale-anchor one.
_ARROW_BODY = r"(?:\{[^{}]*\}|[^;{}]*)"




@needs_node
class TestTheDataChannelStatusWires:
    """Connection state is the one thing the owner can only learn from the page.

    Screenless makes the *absence* of these wires indistinguishable from a
    connection that is merely slow: the buddy says nothing either way. So the
    status text is the whole signal, and nothing asserted it was wired.
    """







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


    def test_clicking_start_calls_start_and_nothing_else(self):
        report = _run(_click_program(fire="clickStart();"))
        assert report["fired"] == ["start"]






class TestTheWiresArePresentAtAll:
    """The honest message when a whole line is deleted.

    The node tests above go red on a deleted wire too — but through
    ``page_slice``'s "the page moved, this test is stale" assertion, which
    reads as a maintenance problem rather than as the defect. These say the
    true thing, and they are the cheap half that keeps working if node is
    unavailable (every test above skips without it).
    """

