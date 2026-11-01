"""One local-speaker playback lane per machine (#1198).

The browser already plays audio through a sequential queue. The Mac-speaker
paths did not: every ``agentwire say`` without a browser, and the portal's
no-client fallback, spawned an independent ``afplay``/``say``, so an ack and a
report — or two sessions — talked over each other.

Every local-speaker utterance now takes a turn in one machine-wide FIFO lane:
a ticket queue in a small JSON file, guarded by ``flock`` so separate
processes (CLI ``say`` calls, the portal) agree on the order. A turn covers a
whole utterance — all of its chunks — so nothing can slip in between them.

Anti-backlog policy, applied when an item is enqueued: if the queued (not yet
playing) audio exceeds ``MAX_BACKLOG_S``, the oldest items are dropped until it
fits — except the newest, which always plays.

A holder that died without releasing (its pid is gone) is pruned, so a
crashed ``say`` can never wedge the lane; a waiter that times out gives up its
ticket and plays nothing.
"""

import asyncio
import fcntl
import json
import os
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Iterator

MAX_BACKLOG_S = 20.0
WORDS_PER_S = 2.5
POLL_S = 0.05
WAIT_TIMEOUT_S = 180.0


def estimate_seconds(words: int) -> float:
    return 0.5 + words / WORDS_PER_S


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def default_dir() -> Path:
    return Path.home() / ".agentwire" / "voice" / "lane"


@dataclass
class Lane:
    """The machine's playback queue. Injectable seams for tests: directory,
    clock, sleep, pid liveness."""

    directory: Path = field(default_factory=lambda: default_dir())
    clock: Callable[[], float] = field(default=lambda: time.time())
    sleep: Callable[[float], None] = field(default=lambda s: time.sleep(s))
    pid_alive: Callable[[int], bool] = field(default=lambda pid: _pid_alive(pid))

    # ── state, always read and written under the lock ──

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / "lane.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self._read()
                yield state
                self._write(state)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            state = json.loads((self.directory / "lane.json").read_text())
            if isinstance(state.get("items"), list):
                return state
        except (OSError, ValueError, AttributeError):
            pass
        return {"seq": 0, "items": []}

    def _write(self, state: dict) -> None:
        tmp = self.directory / "lane.json.tmp"
        tmp.write_text(json.dumps(state))
        tmp.replace(self.directory / "lane.json")

    # ── queue operations ──

    def enqueue(self, text: str, session: str = "", pid: int | None = None) -> int:
        """Queue an utterance; returns its ticket. Applies the drop policy."""
        with self._locked() as state:
            state["seq"] += 1
            ticket = state["seq"]
            state["items"].append({
                "id": ticket, "session": session, "words": len(text.split()),
                "pid": pid if pid is not None else os.getpid(),
                "enqueued": self.clock(), "started": False,
            })
            _apply_policy(state["items"])
            return ticket

    def status(self, ticket: int) -> str:
        """``go`` (your turn — now marked playing), ``wait``, or ``dropped``."""
        with self._locked() as state:
            items = state["items"]
            while items and not self.pid_alive(items[0]["pid"]):
                items.pop(0)  # a holder that died never released
            ids = [i["id"] for i in items]
            if ticket not in ids:
                return "dropped"
            if ids[0] == ticket:
                items[0]["started"] = True
                return "go"
            return "wait"

    def release(self, ticket: int) -> None:
        with self._locked() as state:
            state["items"] = [i for i in state["items"] if i["id"] != ticket]

    def _poll(self, ticket: int, deadline: float) -> str | None:
        """One wait step: the final state, or None to keep waiting."""
        state = self.status(ticket)
        if state != "wait":
            return state
        return "timeout" if self.clock() >= deadline else None

    @contextmanager
    def turn(self, text: str, session: str = "",
             timeout: float = WAIT_TIMEOUT_S) -> Iterator[bool]:
        """Wait for this utterance's turn. Yields True to play, False if it was
        dropped or timed out (play nothing). Always releases the ticket."""
        ticket = self.enqueue(text, session)
        deadline = self.clock() + timeout
        try:
            while (state := self._poll(ticket, deadline)) is None:
                self.sleep(POLL_S)
            yield state == "go"
        finally:
            self.release(ticket)

    @asynccontextmanager
    async def aturn(self, text: str, session: str = "",
                    timeout: float = WAIT_TIMEOUT_S) -> AsyncIterator[bool]:
        """:meth:`turn` for the portal's event loop — waits without blocking it."""
        ticket = self.enqueue(text, session)
        deadline = self.clock() + timeout
        try:
            while (state := self._poll(ticket, deadline)) is None:
                await asyncio.sleep(POLL_S)
            yield state == "go"
        finally:
            self.release(ticket)


def _apply_policy(items: list[dict]) -> None:
    """Trim the backlog, in place, oldest first. The playing head and the
    newest item are never dropped."""
    keep_ids = {i["id"] for i in items if i["started"]}
    keep_ids.add(items[-1]["id"])

    def backlog() -> float:
        return sum(estimate_seconds(i["words"]) for i in items if not i["started"])

    while backlog() > MAX_BACKLOG_S:
        victim = next((i for i in items if i["id"] not in keep_ids), None)
        if victim is None:
            break
        items.remove(victim)
