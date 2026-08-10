"""Cut a region out of the served buddy page, failing loudly if it moved.

One implementation, shared by every file that pins a browser-event wire
(#995). It arrived in ``tests/unit/test_buddy_restart.py`` with #1001 and was
lifted here the moment a second file needed it: two copies of an extractor
whose whole job is to fail honestly is two chances for one of them to fail
quietly instead.
"""

from __future__ import annotations

import re


def page_slice(page: str, start_pat: str, end_pat: str, what: str, *, shape: str) -> str:
    """Return the region of *page* from *start_pat* through *end_pat*.

    Extraction rather than a copy, for the reason ``announcer_source`` gives:
    a test that re-derives its subject proves something about the copy. And a
    silent miss here would be the worst outcome — the wire could be gone and
    the test would still be green, which is the #995 failure repeated by the
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
