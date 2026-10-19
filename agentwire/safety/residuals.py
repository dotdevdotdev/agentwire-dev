"""What the text hook cannot decide, and who closes it (#1065).

The single list behind ``agentwire doctor``'s "Residuals you own" section and
the closed / fail-closed / user-owned table in ``docs/wiki/internals/damage-control.md``.
A PreToolUse hook reads the command TEXT before it runs. Anything whose path or
verb comes into existence at run time, or whose content lives in a file the text
only names, is outside what text can decide; tightening rules only moves the
bypass to the next spelling. The honest claim is "every statically decidable
shape closed, the rest fails closed or is yours to enforce at the OS".

Informational only: never scored, never an issue. agentwire does not set these
controls for you.
"""

from typing import Tuple

# (residual, why the text hook cannot decide it, the control that closes it)
USER_OWNED_RESIDUALS: Tuple[Tuple[str, str, str], ...] = (
    (
        "SQL fed from a file (psql -f, mysql < f, sqlite3 db < f)",
        "the statements are in the file, not in the command",
        "give agent sessions a read-only database role; keep the write credential out of their environment",
    ),
    (
        "make / just / task recipes and package scripts (make clean, just nuke, npm run x)",
        "the recipe text lives in a Makefile / justfile, edited by anyone",
        "run agent sessions under a sandbox profile (sandbox-exec, container, separate uid) scoped to the project",
    ),
    (
        "check-then-use race (TOCTOU): a path the hook validated is swapped before the command uses it",
        "a PreToolUse hook sees the text once, before execution; it cannot hold the filesystem still",
        "set immutable flags on the control files yourself: chflags uchg (macOS) / chattr +i (Linux) on "
        "~/.agentwire/damagecontrol.yml, ~/.claude/settings.json, the installed hooks (chflags nouchg to edit)",
    ),
    (
        "run-time-assembled paths and verbs beyond the shapes that fail closed "
        "(decoded payloads, chr() chains, $(...) output, aliases defined in an earlier turn)",
        "the string does not exist until the program runs",
        "the same immutable flags on the crown-jewel files, or a sandbox profile that denies writes to them",
    ),
)
