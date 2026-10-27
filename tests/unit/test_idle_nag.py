"""Unit tests for the edge-triggered idle-nag decision helper (#464).

`should_nag_idle_session` is a pure function: it decides whether an idle
session should be included in the nag batch this scan, given its current
`last_output_timestamp` and the timestamp captured at its last nag. These
tests exercise it without the async loop / tmux.
"""

from agentwire.server import should_nag_idle_session


def test_continuously_idle_session_nags_once():
    """An unchanged last_output_timestamp across scans → nag once, then silent."""
    nagged: dict[str, float] = {}
    ts = 1000.0

    # First scan: never nagged → include, caller records the timestamp.
    assert should_nag_idle_session("piinpoint", ts, nagged) is True
    nagged["piinpoint"] = ts

    # Subsequent scans with the same (fixed) timestamp → skipped forever.
    for _ in range(30):
        assert should_nag_idle_session("piinpoint", ts, nagged) is False


