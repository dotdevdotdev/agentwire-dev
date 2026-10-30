"""The machine-wide local-speaker playback lane (#1198).

Two utterances must never overlap, order is FIFO across sessions and
processes, and a backlog never grows without bound — while the newest report
always plays. No real audio and no real waits: the player and the clock are
stubs.
"""

import pytest

from agentwire import playback_lane as pl


@pytest.fixture
def lane(tmp_path):
    clock = {"t": 1000.0}
    lane = pl.Lane(directory=tmp_path, clock=lambda: clock["t"], pid_alive=lambda pid: True)
    lane._clock = clock
    return lane


def test_back_to_back_utterances_never_overlap(lane):
    """The second `say` starts only after the first finished playing."""
    events: list[str] = []
    first = lane.turn("say", "first utterance", "a")

    assert first.__enter__() is True
    events.append("start first")

    def finish_first_while_second_waits(_seconds):
        # The second caller is blocked here; it may only proceed once the
        # first one is done playing and has released its turn.
        if "end first" not in events:
            events.append("end first")
            first.__exit__(None, None, None)

    lane.sleep = finish_first_while_second_waits
    with lane.turn("say", "second utterance", "b") as play:
        assert play is True
        events.append("start second")

    assert events == ["start first", "end first", "start second"]


def test_order_is_fifo_across_sessions(lane):
    tickets = [lane.enqueue("say", f"utterance {n}", session) for n, session
               in enumerate(["a", "b", "a"])]
    order = []
    while len(order) < 3:
        head = next(t for t in tickets if t not in order and lane.status(t) == "go")
        assert all(lane.status(t) == "wait" for t in tickets if t not in order and t != head)
        order.append(head)
        lane.release(head)
    assert order == tickets


def test_an_ack_is_dropped_once_its_report_is_queued(lane):
    playing = lane.enqueue("say", "something already playing", "other")
    assert lane.status(playing) == "go"
    ack = lane.enqueue("ack", "On it.", "agentwire-voice")
    other_ack = lane.enqueue("ack", "On it.", "someone-else")
    report = lane.enqueue("report", "Four sessions are running.", "agentwire-voice")

    assert lane.status(ack) == "dropped"
    assert lane.status(other_ack) == "wait"
    lane.release(playing)
    lane.release(other_ack)
    assert lane.status(report) == "go"


def test_a_playing_ack_is_never_cut_off(lane):
    ack = lane.enqueue("ack", "On it.", "agentwire-voice")
    assert lane.status(ack) == "go"
    lane.enqueue("report", "Done.", "agentwire-voice")
    assert lane.status(ack) == "go"


def test_backlog_is_bounded_and_the_newest_report_survives(lane):
    playing = lane.enqueue("say", "head", "x")
    lane.status(playing)
    long_text = " ".join(["word"] * 30)  # ~12.5 s each
    says = [lane.enqueue("say", long_text, "x") for _ in range(4)]
    report = lane.enqueue("report", long_text, "agentwire-voice")

    # 5 x 12.5 s queued against a 20 s budget: the says go oldest-first and
    # the report alone is left to play after the head.
    assert [t for t in [*says, report] if lane.status(t) != "dropped"] == [report]


def test_a_dead_holder_cannot_wedge_the_lane(tmp_path):
    lane = pl.Lane(directory=tmp_path, pid_alive=lambda pid: pid != 4242)
    lane.enqueue("say", "crashed mid-playback", "x")
    # Simulate the holder's pid: rewrite the head's pid to a dead one.
    import json

    state = json.loads((tmp_path / "lane.json").read_text())
    state["items"][0]["pid"] = 4242
    (tmp_path / "lane.json").write_text(json.dumps(state))

    nxt = lane.enqueue("say", "next", "y")
    assert lane.status(nxt) == "go"


def test_a_waiter_that_times_out_plays_nothing_and_leaves_the_queue(lane):
    head = lane.enqueue("say", "long running", "x")
    lane.status(head)

    def tick(_seconds):
        lane._clock["t"] += 1.0

    lane.sleep = tick
    with lane.turn("say", "too late", "y", timeout=3) as play:
        assert play is False
    assert len(lane._read()["items"]) == 1


async def test_the_portal_turn_waits_without_blocking(lane):
    head = lane.enqueue("say", "playing", "x")
    lane.status(head)
    lane.release(head)
    async with lane.aturn("say", "portal fallback") as play:
        assert play is True
    assert lane._read()["items"] == []


def test_a_queued_report_outlives_a_flood_of_later_chatter(lane):
    playing = lane.enqueue("say", "head", "x")
    lane.status(playing)
    long_text = " ".join(["word"] * 30)
    report = lane.enqueue("report", long_text, "agentwire-voice")
    for _ in range(3):
        lane.enqueue("say", long_text, "chatty")
    assert lane.status(report) == "wait"  # still queued, oldest, never dropped
