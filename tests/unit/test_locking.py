"""Tests for agentwire/locking.py — path sanitization + dead-holder recovery race (#491)."""

import multiprocessing as mp
import time
from pathlib import Path

from agentwire.locking import (
    session_lock,
)


# --- Dead-holder recovery race regression (#491) -------------------------------
#
# The waiter path used to unlink a lock file whenever it observed a dead holder
# PID, without re-checking the lock was still unheld. The dangerous state is a
# *live* holder whose lock file happens to record a dead PID — which occurs
# transiently every time a process acquires the flock but hasn't yet written its
# own PID over the previous (now-dead) holder's. A waiter that reads that dead
# PID would unlink the live holder's file and re-acquire on a fresh inode, so
# both processes end up "holding" the lock on different inodes — mutual
# exclusion broken. The fix relies on flock's kernel-level auto-release on
# holder death: recovery happens purely by re-acquiring the flock (which proves
# the prior holder is gone), never by unlinking based on a separately-read PID.
#
# Worker functions are module-level so multiprocessing ("spawn") can pickle them.


def _point_lockdir(lockdir: str) -> None:
    """Repoint the module's LOCKS_DIR in this (child) process."""
    import agentwire.locking as locking

    locking.LOCKS_DIR = Path(lockdir)


def _hold_then_die(lockdir: str, session: str, ready, hold_secs: float) -> None:
    """Acquire the lock, signal ready, hold briefly, then exit (releasing flock)."""
    _point_lockdir(lockdir)
    with session_lock(session, wait=False):
        ready.set()
        time.sleep(hold_secs)
    # On exit the kernel releases the flock — simulating a holder that vanished.


def test_dead_holder_recovered_without_unlink(tmp_path, monkeypatch):
    """A waiter recovers from a genuinely-dead holder via flock auto-release,
    and the lock file is never unlinked during recovery (same inode throughout)."""
    monkeypatch.setattr("agentwire.locking.LOCKS_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    session = "race-recover"
    lock_path = tmp_path / f"{session}.lock"

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    holder = ctx.Process(target=_hold_then_die, args=(str(tmp_path), session, ready, 0.3))
    holder.start()
    assert ready.wait(timeout=5), "holder never acquired the lock"

    inode_before = lock_path.stat().st_ino

    start = time.time()
    with session_lock(session, wait=True, timeout=5.0, poll_interval=0.02):
        # We only get here once the holder process has exited and released flock.
        assert time.time() - start >= 0.2
        assert lock_path.exists()
        assert lock_path.stat().st_ino == inode_before  # never unlinked

    holder.join(timeout=5)
