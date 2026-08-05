"""Detection half of the real-``~/.agentwire`` protection (#893).

Lives in its own module rather than in ``conftest.py`` for a reason that bit
this guard during development: pytest loads the root conftest as the top-level
module ``conftest``, while a test doing ``import tests.conftest`` gets a
*second, distinct* module object. Both copies then ran the installer, so two
audit hooks recorded every write, and a flag set on one copy did not silence
the other — the guard's own tests were suppressing one recorder while the other
kept failing the run.

That is the same second-copy-of-the-SSOT shape as #899, arriving by a different
route. One owning module, imported by absolute name from both sides, is the
fix: ``tests.home_guard`` resolves to a single object however conftest was
loaded.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

#: The owner's real config directory, captured at import — before any test
#: redirects ``$HOME``, so it keeps naming the real one.
REAL_AGENTWIRE_HOME = Path.home() / ".agentwire"

#: Writes under the real home that nothing sanctioned: the failures.
WRITES: list = []

#: Writes recorded inside :func:`sanctioned_real_home_write` — the evidence
#: the hook fired, kept separate so a sanction proves detection instead of
#: disabling it.
SANCTIONED_WRITES: list = []

_SANCTIONED = [0]
_INSTALLED = [False]

#: Audit events that create, modify or delete a path. ``open`` is handled
#: separately because its intent has to be read from mode *or* flags.
_WRITE_EVENTS = {
    "os.mkdir", "os.rename", "os.replace", "os.remove", "os.rmdir", "os.link",
    "os.symlink", "os.truncate", "os.chmod", "shutil.copyfile", "shutil.move",
}

_WRITING_FLAGS = (
    getattr(os, "O_WRONLY", 0) | getattr(os, "O_RDWR", 0)
    | getattr(os, "O_CREAT", 0) | getattr(os, "O_APPEND", 0)
    | getattr(os, "O_TRUNC", 0)
)


@contextlib.contextmanager
def sanctioned_real_home_write():
    """Let a block write under the real home without failing the run.

    Exists for exactly one caller: the tests that prove the hook can FAIL.
    Those must perform genuine writes under the real home — a hook only ever
    exercised against a tmp dir is how the ``os.open`` blindness survived —
    which would otherwise trip the very backstop they exercise.

    Narrow by construction: writes inside the block are still recorded, into
    :data:`SANCTIONED_WRITES`, which those tests assert on. Nothing else in the
    suite should use this.
    """
    _SANCTIONED[0] += 1
    try:
        yield SANCTIONED_WRITES
    finally:
        _SANCTIONED[0] -= 1


def install() -> None:
    """Install the audit hook once per interpreter.

    An audit hook cannot be removed, so installing twice would double every
    record. Idempotent for the same reason the module exists.
    """
    if _INSTALLED[0]:
        return
    _INSTALLED[0] = True

    real = str(REAL_AGENTWIRE_HOME)

    def hook(event, args):
        if event == "open":
            # (path, mode, flags). ``mode`` is the string only for
            # builtins.open/io.open; the low-level ``os.open`` passes None and
            # carries intent in ``flags``. Checking mode alone made the guard
            # blind to os.open — which is how seven production sites under the
            # config dir create files, including core.write_role_prompt (0o600
            # so the prompt is never briefly world-readable).
            if len(args) < 2:
                return
            mode, flags = args[1], (args[2] if len(args) > 2 else 0)
            if mode:
                if not any(c in str(mode) for c in "wax+"):
                    return
            elif not (isinstance(flags, int) and flags & _WRITING_FLAGS):
                return
            target = args[0]
        elif event in _WRITE_EVENTS:
            target = args[0] if args else None
        else:
            return
        try:
            path = str(target)
        except Exception:
            return
        if not path.startswith(real):
            return
        entry = (os.environ.get("PYTEST_CURRENT_TEST", "<session>"), event, path)
        (SANCTIONED_WRITES if _SANCTIONED[0] else WRITES).append(entry)

    sys.addaudithook(hook)


def report() -> str | None:
    """A human-readable failure, or None if nothing escaped."""
    if not WRITES:
        return None
    seen, lines = set(), []
    for test_id, event, path in WRITES:
        test_id = str(test_id).split(" (")[0]
        key = (test_id, path)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {test_id}\n      {event}  {path}")
    return (
        f"the test suite wrote into the REAL ~/.agentwire ({len(seen)} write(s), #893)\n"
        + "\n".join(lines[:25])
    )
