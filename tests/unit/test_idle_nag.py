"""Unit tests for the edge-triggered idle-nag decision helper (#464).

`should_nag_idle_session` is a pure function: it decides whether an idle
session should be included in the nag batch this scan, given its current
`last_output_timestamp` and the timestamp captured at its last nag. These
tests exercise it without the async loop / tmux.
"""

from agentwire.server import should_nag_idle_session


def test_never_nagged_session_is_included():
    nagged: dict[str, float] = {}
    assert should_nag_idle_session("piinpoint", 1000.0, nagged) is True


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


def test_advancing_timestamp_triggers_new_nag():
    """New output (advanced timestamp) → a fresh nag."""
    nagged = {"piinpoint": 1000.0}

    # New question/error arrived: timestamp moved forward.
    assert should_nag_idle_session("piinpoint", 1500.0, nagged) is True
    nagged["piinpoint"] = 1500.0

    # And it then settles back to nagging once for the new fixed timestamp.
    assert should_nag_idle_session("piinpoint", 1500.0, nagged) is False


def test_reset_on_active_nags_again_next_episode():
    """Dropping below the idle threshold pops the entry → nags again next time."""
    nagged: dict[str, float] = {}
    ts = 1000.0

    assert should_nag_idle_session("piinpoint", ts, nagged) is True
    nagged["piinpoint"] = ts
    assert should_nag_idle_session("piinpoint", ts, nagged) is False

    # Session became active → caller resets episode state.
    nagged.pop("piinpoint", None)

    # New idle episode (even at the same timestamp) → nags again.
    assert should_nag_idle_session("piinpoint", ts, nagged) is True


def test_sessions_are_independent():
    nagged: dict[str, float] = {"a": 1000.0}
    assert should_nag_idle_session("a", 1000.0, nagged) is False
    assert should_nag_idle_session("b", 1000.0, nagged) is True
