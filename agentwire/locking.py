"""Session locking for concurrent task execution.

Uses flock-based locking to ensure only one `ensure` command runs
for a given session at a time.
"""

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockError(Exception):
    """Raised when a lock cannot be acquired."""

    pass


class LockTimeout(LockError):  # noqa: N818  # public API name, renaming breaks callers
    """Raised when waiting for a lock times out."""

    pass


class LockConflict(LockError):  # noqa: N818  # public API name, renaming breaks callers
    """Raised when a lock is held by another process (non-blocking)."""

    pass


# Lock directory
LOCKS_DIR = Path.home() / ".agentwire" / "locks"


def _get_lock_path(session: str) -> Path:
    """Get the lock file path for a session.

    Sanitizes session name to be filesystem-safe.

    Args:
        session: Session name (may contain / for worktrees)

    Returns:
        Path to the lock file
    """
    # Replace / with -- for worktree sessions (e.g., project/branch)
    safe_name = session.replace("/", "--")
    return LOCKS_DIR / f"{safe_name}.lock"


@contextmanager
def session_lock(
    session: str,
    wait: bool = False,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
) -> Iterator[None]:
    """Acquire an exclusive lock for a session.

    Usage:
        with session_lock("my-session"):
            # Only one process can be here for this session
            do_work()

    Args:
        session: Session name to lock
        wait: If True, wait for lock; if False, fail immediately if locked
        timeout: Maximum seconds to wait for lock (only if wait=True)
        poll_interval: Seconds between lock attempts (only if wait=True)

    Yields:
        None when lock is acquired

    Raises:
        LockConflict: If wait=False and lock is held by another process
        LockTimeout: If wait=True and timeout is exceeded
    """
    # Ensure locks directory exists
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)

    lock_path = _get_lock_path(session)
    lock_file = None

    try:
        # Open (create if needed) the lock file
        lock_file = open(lock_path, "w")

        if wait:
            # Blocking with timeout
            start_time = time.time()
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break  # Lock acquired
                except BlockingIOError:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        raise LockTimeout(
                            f"Timeout waiting for lock on session '{session}' "
                            f"after {timeout:.1f}s"
                        )
                    # Check if the lock holder is still alive
                    holder_pid = get_lock_holder(session)
                    if holder_pid is not None and not _is_process_running(holder_pid):
                        # Stale lock from a dead process — clean it up
                        remove_lock(session)
                        # Close our current file handle (the old lock file is gone)
                        lock_file.close()
                        lock_file = open(lock_path, "w")
                        continue  # Retry immediately
                    time.sleep(poll_interval)
        else:
            # Non-blocking - fail immediately if locked
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockConflict(
                    f"Session '{session}' is locked by another ensure process. "
                    f"Use --wait-lock to wait for the lock."
                )

        # Write PID for debugging
        lock_file.write(f"{__import__('os').getpid()}\n")
        lock_file.flush()

        yield

    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass


def is_session_locked(session: str) -> bool:
    """Check if a session is currently locked.

    Args:
        session: Session name to check

    Returns:
        True if session is locked, False otherwise
    """
    lock_path = _get_lock_path(session)

    if not lock_path.exists():
        return False

    try:
        with open(lock_path, "r+") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return False  # We could acquire it, so it's not locked
            except BlockingIOError:
                return True  # Locked by another process
    except Exception:
        return False


def get_lock_holder(session: str) -> int | None:
    """Get the PID of the process holding a lock (if any).

    Args:
        session: Session name

    Returns:
        PID of lock holder, or None if not locked or can't determine
    """
    lock_path = _get_lock_path(session)

    if not lock_path.exists():
        return None

    try:
        content = lock_path.read_text().strip()
        if content:
            return int(content)
    except (ValueError, OSError):
        pass

    return None


def _is_process_running(pid: int) -> bool:
    """Check if a process with given PID is running.

    Args:
        pid: Process ID to check

    Returns:
        True if process is running, False otherwise
    """
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def list_locks() -> list[dict]:
    """List all locks with their metadata.

    Returns:
        List of dicts with keys: session, pid, age_seconds, status
        status is 'active' (process running), 'stale' (process dead/missing),
        or 'unknown' (can't determine)
    """

    if not LOCKS_DIR.exists():
        return []

    results = []
    current_time = time.time()

    for lock_file in LOCKS_DIR.glob("*.lock"):
        session = lock_file.stem.replace("--", "/")

        try:
            stat = lock_file.stat()
            age_seconds = int(current_time - stat.st_mtime)
        except OSError:
            age_seconds = 0

        # Read PID from file
        pid = None
        try:
            content = lock_file.read_text().strip()
            if content:
                pid = int(content)
        except (ValueError, OSError):
            pass

        # Determine status
        if pid is None:
            status = "stale"  # No PID recorded
        elif _is_process_running(pid):
            # Double check with flock
            if is_session_locked(session):
                status = "active"
            else:
                status = "stale"  # Process exists but doesn't hold lock
        else:
            status = "stale"  # Process is dead

        results.append({
            "session": session,
            "pid": pid,
            "age_seconds": age_seconds,
            "status": status,
        })

    return sorted(results, key=lambda x: x["session"])


def clean_stale_locks(dry_run: bool = False) -> list[str]:
    """Remove all stale locks.

    A lock is stale if:
    - No PID is recorded in the lock file
    - The recorded PID's process is not running
    - The lock file exists but no process holds the flock

    Args:
        dry_run: If True, don't actually remove, just return what would be removed

    Returns:
        List of session names whose locks were removed (or would be removed)
    """
    removed = []

    for lock_info in list_locks():
        if lock_info["status"] == "stale":
            session = lock_info["session"]
            if not dry_run:
                remove_lock(session)
            removed.append(session)

    return removed


def remove_stale_lock(session: str) -> bool:
    """Remove a lock only if it's stale (not held by a running process).

    Args:
        session: Session name

    Returns:
        True if a stale lock was removed, False otherwise
    """
    lock_path = _get_lock_path(session)
    if not lock_path.exists():
        return False
    if is_session_locked(session):
        return False  # Actively held — don't touch
    return remove_lock(session)


def remove_lock(session: str) -> bool:
    """Force-remove a lock file.

    Args:
        session: Session name

    Returns:
        True if lock was removed, False if it didn't exist
    """
    lock_path = _get_lock_path(session)

    if not lock_path.exists():
        return False

    try:
        lock_path.unlink()
        return True
    except OSError:
        return False
