"""Tests for `tail_transcript` — the SDK watch-mode source.

Verifies replay-then-follow behavior on the JSONL transcript file:
existing lines yield first, new appends yield as they arrive, malformed
lines are skipped, missing files don't hang past the boot grace window.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentwire.repl.persistence import (
    DEFAULT_REPL_HOME,
    create_session,
    tail_transcript,
)


def _events_path(home: Path, name: str) -> Path:
    return home / name / "transcript.jsonl"


@pytest.mark.asyncio
async def test_replays_existing_events_then_stops_when_not_following(tmp_path):
    t = create_session(mode="bypass", model="m", allowed_tools=["Read"], name="run-a", home=tmp_path)
    t.write_event({"type": "session", "session_id": "abc"})
    t.write_event({"type": "message_end", "message": {"role": "user", "content": "hi"}})
    t.close()

    events = []
    async for ev in tail_transcript("run-a", home=tmp_path, follow=False):
        events.append(ev)

    assert len(events) == 2
    assert events[0]["type"] == "session"
    assert events[1]["type"] == "message_end"


@pytest.mark.asyncio
async def test_streams_new_appends_in_follow_mode(tmp_path):
    t = create_session(mode="bypass", model="m", allowed_tools=["Read"], name="run-b", home=tmp_path)
    t.write_event({"type": "session"})

    seen: list[dict] = []
    stop = asyncio.Event()

    async def consume():
        async for ev in tail_transcript("run-b", home=tmp_path, poll_interval=0.05):
            seen.append(ev)
            if len(seen) >= 3:
                stop.set()
                return

    async def produce():
        await asyncio.sleep(0.1)
        t.write_event({"type": "agent_start"})
        await asyncio.sleep(0.05)
        t.write_event({"type": "turn_end"})

    consumer = asyncio.create_task(consume())
    producer = asyncio.create_task(produce())
    await producer
    try:
        await asyncio.wait_for(stop.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(f"only saw {len(seen)} events: {seen}")
    finally:
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        t.close()

    assert [e["type"] for e in seen[:3]] == ["session", "agent_start", "turn_end"]


@pytest.mark.asyncio
async def test_malformed_lines_skipped(tmp_path):
    base = tmp_path / "run-c"
    base.mkdir()
    (base / "transcript.jsonl").write_text(
        '{"type": "ok"}\n'
        "this is not json\n"
        '{"type": "ok2"}\n'
    )
    events = [ev async for ev in tail_transcript("run-c", home=tmp_path, follow=False)]
    assert [e["type"] for e in events] == ["ok", "ok2"]


@pytest.mark.asyncio
async def test_missing_file_returns_empty_quickly_when_not_following(tmp_path):
    events = [ev async for ev in tail_transcript("never-existed", home=tmp_path, follow=False)]
    assert events == []
