"""Tests for agentwire/locking.py — path sanitization + dead-holder recovery race (#491)."""

import fcntl
import multiprocessing as mp
import time
from pathlib import Path

from agentwire.locking import (
    LOCKS_DIR,
    _get_lock_path,
    session_lock,
)


class TestLockPathSanitization:
    def test_simple_session(self):
        path = _get_lock_path("myapp")
        assert path == LOCKS_DIR / "myapp.lock"

    def test_worktree_slash_replaced(self):
        path = _get_lock_path("myapp/feature")
        assert path == LOCKS_DIR / "myapp--feature.lock"

    def test_deep_worktree(self):
        path = _get_lock_path("myapp/feat/sub")
        assert path == LOCKS_DIR / "myapp--feat--sub.lock"


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


def _live_holder_with_dead_pid(lockdir: str, session: str, ready, die_at) -> None:
    """Hold the flock for real, while keeping a *dead* PID in the lock file.

    This deterministically recreates the race window: the flock is genuinely
    held (so the lock IS occupied), yet the file's recorded PID looks dead to
    any waiter that inspects it. A waiter opens the file with "w" (truncating
    it), so we continuously rewrite the dead PID to guarantee the waiter reads a
    dead holder on its next poll — exactly the state the old cleanup unlinked.
    """
    _point_lockdir(lockdir)
    lock_path = Path(lockdir) / f"{session}.lock"
    f = open(lock_path, "w")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    ready.set()
    while not die_at.is_set():
        f.seek(0)
        f.truncate()
        f.write("999999999\n")  # a PID that is not running
        f.flush()
        time.sleep(0.003)
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    f.close()


def _entering_waiter(lockdir: str, session: str, entered) -> None:
    """Acquire via the wait path and signal the instant we enter the section."""
    _point_lockdir(lockdir)
    with session_lock(session, wait=True, timeout=8.0, poll_interval=0.01):
        entered.set()
        time.sleep(0.2)


def _hold_then_die(lockdir: str, session: str, ready, hold_secs: float) -> None:
    """Acquire the lock, signal ready, hold briefly, then exit (releasing flock)."""
    _point_lockdir(lockdir)
    with session_lock(session, wait=False):
        ready.set()
        time.sleep(hold_secs)
    # On exit the kernel releases the flock — simulating a holder that vanished.


def test_waiter_never_steals_live_lock_with_dead_pid(tmp_path, monkeypatch):
    """A waiter must NOT enter while a live holder holds the flock, even when the
    lock file records a dead PID. Reproduces #491: the old code unlinked the live
    lock and entered immediately; the fix keeps the waiter blocked on the flock."""
    monkeypatch.setattr("agentwire.locking.LOCKS_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    session = "race-steal"

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    die_at = ctx.Event()
    entered = ctx.Event()

    holder = ctx.Process(
        target=_live_holder_with_dead_pid, args=(str(tmp_path), session, ready, die_at)
    )
    holder.start()
    assert ready.wait(timeout=5), "holder never acquired the flock"

    waiter = ctx.Process(
        target=_entering_waiter, args=(str(tmp_path), session, entered)
    )
    waiter.start()

    # Give the waiter ample time to (wrongly) recover. The fix keeps it blocked
    # because the flock is genuinely held; the old code would have unlinked and
    # entered here.
    stole = entered.wait(timeout=1.5)
    assert not stole, (
        "waiter entered the critical section while a live holder still held the "
        "flock — mutual exclusion broken (#491)"
    )

    # Release the real holder; the waiter must now acquire promptly.
    die_at.set()
    holder.join(timeout=5)
    assert entered.wait(timeout=5), "waiter never acquired after the holder released"
    waiter.join(timeout=5)


def test_dead_holder_recovered_without_unlink(tmp_path, monkeypatch):
    """A waiter recovers from a genuinely-dead holder via flock auto-release,
    and the lock file is never unlinked during recovery (same inode throughout)."""
    monkeypatch.setattr("agentwire.locking.LOCKS_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    session = "race-recover"
    lock_path = tmp_path / f"{session}.lock"

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    holder = ctx.Process(
        target=_hold_then_die, args=(str(tmp_path), session, ready, 0.3)
    )
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
