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


def _status_program(*, fire: str, page: str | None = None) -> str:
    page = _page() if page is None else page
    open_wire = page_slice(
        page, r'dc\.addEventListener\("open"', r";\s*\n", "the dc open wire",
        shape=r'^dc\.addEventListener\("open",\s*\(\)\s*=>\s*' + _ARROW_BODY + r"\s*\);\s*$",
    )
    close_wire = page_slice(
        page, r'dc\.addEventListener\("close"', r";\s*\n", "the dc close wire",
        shape=r'^dc\.addEventListener\("close",\s*\(\)\s*=>\s*' + _ARROW_BODY + r"\s*\);\s*$",
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


@needs_node
class TestAMovedAnchorSaysSoInsteadOfCrashingNode:
    """The partial-anchor guard, MEASURED rather than claimed.

    ``page_slice``'s guarantee is that a drifted anchor reads as "the page
    moved" and never as an opaque node ``SyntaxError`` — and the guarantee is
    only as good as the shape each call site declares. It was NOT good enough
    here on first submission: reflowing either dc status wire across several
    lines (formatting only, the wire fully intact) truncated the region at the
    first statement and the loose shape accepted the wreckage, so node blew up.
    That is the exact degradation #995 names, reproduced by the tests written
    to close it — and the PR body claimed coverage that did not exist, which is
    worse than the gap.

    So the claim is a test now. The reflow below changes nothing but whitespace:
    every one of these pages still WIRES the handler, and a reader looking at
    the failure has to be told that.
    """

    #: ONE STATEMENT PER LINE, which is what a formatter actually produces and
    #: is also the strictly stronger control: it puts a line-ending ``;`` after
    #: a statement that closes a paren, so the truncated region ends in ``);``
    #: and a loose shape is fooled by it. A reflow that packs the body onto one
    #: line does not reproduce the defect at all — the region ends in
    #: ``false;`` — and a fixture that cannot reproduce the failure is the
    #: fixture-shaped blind spot this whole file exists to avoid.
    BODIES = {
        "open": ['setStatus("listening");', "$stop.disabled = false;"],
        "close": ['setStatus("closed");'],
    }

    def _reflowed(self, event: str) -> str:
        page = _page()
        original = [ln for ln in page.splitlines()
                    if f'dc.addEventListener("{event}"' in ln]
        assert len(original) == 1, "the wire this test reflows is not where it was"
        body = "\n".join(f"      {line}" for line in self.BODIES[event])
        return page.replace(
            original[0],
            f'    dc.addEventListener("{event}", () => {{\n{body}\n    }});',
        )

    @pytest.mark.parametrize("event", ["open", "close"])
    def test_a_reflowed_wire_reports_a_moved_anchor(self, event):
        with pytest.raises(AssertionError) as excinfo:
            _status_program(fire="", page=self._reflowed(event))
        message = str(excinfo.value)
        assert "does not have the shape this test assumes" in message
        assert "NOT a behaviour failure" in message

    @pytest.mark.parametrize("event", ["open", "close"])
    def test_the_reflow_really_did_leave_the_wire_intact(self, event):
        """The control. If the reflow broke the wire, the assertion above would
        be reporting a genuine defect and would prove nothing about anchors."""
        reflowed = self._reflowed(event)
        assert f'dc.addEventListener("{event}"' in reflowed
        for statement in self.BODIES[event]:
            assert statement in reflowed

    @pytest.mark.parametrize("event", ["open", "close"])
    def test_the_reflow_is_what_a_loose_shape_would_have_swallowed(self, event):
        """And the discriminator: the truncated region a loose shape accepted
        ends in ``);``, which is why it passed for the listener's own
        terminator. Asserted directly, so a future reflow that stops producing
        that ending cannot silently turn the two tests above into no-ops."""
        page = self._reflowed(event)
        start = page.index(f'dc.addEventListener("{event}"')
        truncated = page[start:page.index(";\n", start) + 1]
        assert truncated.endswith(");")
        assert truncated.count("{") > truncated.count("}")


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
