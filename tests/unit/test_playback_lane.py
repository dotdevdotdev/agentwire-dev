"""The machine-wide local-speaker playback lane (#1198).

Two utterances must never overlap, order is FIFO across sessions and
processes, and a backlog never grows without bound — while the newest utterance
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
    first = lane.turn("first utterance", "a")

    assert first.__enter__() is True
    events.append("start first")

    def finish_first_while_second_waits(_seconds):
        # The second caller is blocked here; it may only proceed once the
        # first one is done playing and has released its turn.
        if "end first" not in events:
            events.append("end first")
            first.__exit__(None, None, None)

    lane.sleep = finish_first_while_second_waits
    with lane.turn("second utterance", "b") as play:
        assert play is True
        events.append("start second")

    assert events == ["start first", "end first", "start second"]


def test_order_is_fifo_across_sessions(lane):
    tickets = [lane.enqueue(f"utterance {n}", session) for n, session
               in enumerate(["a", "b", "a"])]
    order = []
    while len(order) < 3:
        head = next(t for t in tickets if t not in order and lane.status(t) == "go")
        assert all(lane.status(t) == "wait" for t in tickets if t not in order and t != head)
        order.append(head)
        lane.release(head)
    assert order == tickets


def test_backlog_is_bounded_and_the_newest_utterance_survives(lane):
    playing = lane.enqueue("head", "x")
    lane.status(playing)
    long_text = " ".join(["word"] * 30)  # ~12.5 s each
    says = [lane.enqueue(long_text, "x") for _ in range(5)]

    # 5 x 12.5 s queued against a 20 s budget: oldest go first, the newest
    # alone is left to play after the head.
    assert [t for t in says if lane.status(t) != "dropped"] == [says[-1]]
    assert lane.status(playing) == "go"


def test_a_flood_never_drops_the_newest_utterance(lane):
    playing = lane.enqueue("head", "x")
    lane.status(playing)
    long_text = " ".join(["word"] * 30)
    over_budget = " ".join(["word"] * 60)  # ~24.5 s: over the 20 s budget alone
    lost = []
    for n in range(8):
        ticket = lane.enqueue(over_budget if n % 2 else long_text, "chatty")
        if lane.status(ticket) == "dropped":
            lost.append(n)
    assert not lost, lost


def test_a_dead_holder_cannot_wedge_the_lane(tmp_path):
    lane = pl.Lane(directory=tmp_path, pid_alive=lambda pid: pid != 4242)
    lane.enqueue("crashed mid-playback", "x")
    # Simulate the holder's pid: rewrite the head's pid to a dead one.
    import json

    state = json.loads((tmp_path / "lane.json").read_text())
    state["items"][0]["pid"] = 4242
    (tmp_path / "lane.json").write_text(json.dumps(state))

    nxt = lane.enqueue("next", "y")
    assert lane.status(nxt) == "go"


def test_a_waiter_that_times_out_plays_nothing_and_leaves_the_queue(lane):
    head = lane.enqueue("long running", "x")
    lane.status(head)

    def tick(_seconds):
        lane._clock["t"] += 1.0

    lane.sleep = tick
    with lane.turn("too late", "y", timeout=3) as play:
        assert play is False
    assert len(lane._read()["items"]) == 1


async def test_the_portal_turn_waits_without_blocking(lane):
    head = lane.enqueue("playing", "x")
    lane.status(head)
    lane.release(head)
    async with lane.aturn("portal fallback") as play:
        assert play is True
    assert lane._read()["items"] == []
