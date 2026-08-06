"""Which checkout is allowed to WRITE machine-global agentwire config (#936).

Every installer path in agentwire resolves its source as ``Path(__file__).parent``
— "the package I happen to be running from". That is correct exactly once: when
the process was launched from the canonically installed tool. It is wrong every
other time, and the failure is silent and machine-wide:

    a session on a task branch runs `agentwire safety install` (or `uv run
    agentwire ...`, or anything that heals hooks), and its OWN checkout — which
    may predate a P0 security fix by days — is copied over
    ``~/.agentwire/hooks/damage-control/`` for every other session on the box.

That is not hypothetical: it reverted #918's ``git -C`` bypass fix within an
hour of deployment, from one of six live worktrees carrying the pre-fix package.
``shutil.copy2`` preserves mtime, so the downgraded file looked *older* than the
deployment that it replaced, and ``doctor`` reported ``[ok] DC hook scripts
current`` — correctly, because the installed copy now matched whatever source
last ran.

So the guard is provenance, not version comparison: **only the canonical install
may write machine-global config.** A version stamp (see
``scripts/regen_damage_control_hooks.py``) is a useful second line and is what
lets ``doctor`` say "the installed hook is older than the package", but on its
own it cannot stop a stale branch that has itself regenerated its hooks.

Three states:

``canonical``
    The running package IS the installed tool. Writes proceed.
``bootstrap``
    No canonical install exists yet (fresh machine, CI, a test with a redirected
    ``$HOME``, a checkout-only dev box). Nothing to protect, so writes proceed —
    refusing here would make the tool uninstallable.
``worktree``
    No canonical install, but the running package sits in a git WORKTREE. A task
    branch is never a legitimate bootstrap source, so this refuses too. Refused.
``foreign``
    A canonical install exists and it is NOT what is running. Writes are refused
    unless the caller explicitly forces them.

Two things this deliberately does NOT key on, both measured rather than assumed:

* **The cwd.** ``~/.local/bin/agentwire`` is a console script with a venv
  shebang, so it imports site-packages regardless of where it is invoked from.
  Running ``agentwire hooks install`` while sitting in a worktree is already
  safe, and a cwd check would refuse it — while still letting ``uv run
  agentwire`` from a stale non-worktree checkout poison the machine. Over- and
  under-inclusive at once.
* **``PATH``.** ``uv run`` puts an ephemeral venv first, so a ``which``-based
  lookup would report the stale checkout AS the canonical install.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

# Distribution names this package has shipped under, most likely first.
_DIST_NAMES = ("agentwire-dev", "agentwire")

CANONICAL = "canonical"
BOOTSTRAP = "bootstrap"
WORKTREE = "worktree"
FOREIGN = "foreign"

#: States in which a machine-global write is refused without an explicit override.
REFUSING_STATES = (FOREIGN, WORKTREE)


def running_package_dir() -> Path:
    """Directory of the ``agentwire`` package this process imported."""
    return Path(__file__).resolve().parent.parent


def _package_under_venv(venv: Path) -> Optional[Path]:
    """``<venv>/lib/python*/site-packages/agentwire`` if it exists."""
    for candidate in sorted(venv.glob("lib/python*/site-packages/agentwire")):
        if candidate.is_dir():
            return candidate.resolve()
    return None


def canonical_package_dir() -> Optional[Path]:
    """The installed tool's ``agentwire`` package dir, or None if not installed.

    Resolved from the install LAYOUT rather than from ``PATH``: ``uv run`` and
    friends put an ephemeral venv's ``agentwire`` first on ``PATH``, so a
    ``which``-based lookup would happily report the stale checkout as canonical
    — the exact confusion this module exists to prevent.
    """
    override = os.environ.get("AGENTWIRE_CANONICAL_PACKAGE")
    if override:
        p = Path(override).expanduser()
        return p.resolve() if p.is_dir() else None

    home = Path.home()

    # uv tool install (the documented install path).
    tool_root = Path(os.environ.get("UV_TOOL_DIR") or home / ".local" / "share" / "uv" / "tools")
    for dist in _DIST_NAMES:
        pkg = _package_under_venv(tool_root / dist)
        if pkg:
            return pkg

    # pipx.
    for dist in _DIST_NAMES:
        pkg = _package_under_venv(home / ".local" / "pipx" / "venvs" / dist)
        if pkg:
            return pkg

    # Any other venv reachable through the console script on ~/.local/bin.
    console = home / ".local" / "bin" / "agentwire"
    if console.exists():
        pkg = _package_under_venv(console.resolve().parent.parent)
        if pkg:
            return pkg

    return None


def in_git_worktree(package_dir: Path) -> bool:
    """True when the package lives in a git WORKTREE (not a primary checkout).

    ``.git`` is a directory in a primary checkout and a FILE (holding a
    ``gitdir:`` pointer) in a linked worktree. Absent in an installed package.
    """
    return (package_dir.parent / ".git").is_file()


def install_provenance() -> Tuple[str, Optional[Path], Path]:
    """``(state, canonical_pkg_or_None, running_pkg)`` — see the module docstring."""
    running = running_package_dir()
    canonical = canonical_package_dir()
    if canonical is None:
        # No install to defer to. That is a legitimate bootstrap — unless the
        # source is a task branch, which is never a thing to install a machine
        # from, install present or not.
        return (WORKTREE if in_git_worktree(running) else BOOTSTRAP), None, running
    if canonical == running:
        return CANONICAL, canonical, running
    return FOREIGN, canonical, running


def refusal_lines(canonical: Optional[Path], running: Path, what: str) -> List[str]:
    """The loud, actionable refusal an operator sees. Never silent (#936)."""
    where = f"  installed at : {canonical}" if canonical else \
            "  installed at : (no canonical install found — and this is a git worktree)"
    return [
        f"REFUSED: {what} would be written from a NON-CANONICAL package.",
        f"  running from : {running}",
        where,
        "",
        "  These are machine-global files. Installing them from a checkout that is",
        "  not the installed tool is how a task branch silently DOWNGRADES the",
        "  security hooks for every other session on this machine (#936).",
        "",
        "  Fix: run this from the installed tool instead —",
        "       git -C <main checkout> pull --ff-only && agentwire rebuild",
        "  Or, if you genuinely mean to install THIS checkout machine-wide:",
        "       pass --allow-foreign-source",
    ]
