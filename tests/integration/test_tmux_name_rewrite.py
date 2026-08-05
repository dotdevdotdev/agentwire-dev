"""``tmux_safe_name`` is verified against REAL tmux, not against our belief (#878).

``worktree.tmux_safe_name`` is safe to apply at *resolution* time only because
it MIRRORS tmux's own name mapping — the name it derives is the name tmux will
have chosen. That invariant is about an external binary's behavior, so a unit
test asserting our own constants can't establish it. This drives actual
``tmux new-session`` on a private socket and reads back the name tmux really
created.

Why it earns its keep: #865 → #868 → #870 → #878 is four rounds of this class,
three of them because the rewrite set was assumed rather than measured. If a
future tmux starts rewriting another character, THIS is the test that fails —
instead of a teardown silently killing nothing while reporting success.

Never touches the user's live tmux server: every command runs against a
dedicated ``-S <socket>`` in a short-lived temp dir (macOS caps Unix-domain
socket paths at ~104 bytes, so pytest's tmp_path is too long to use directly —
same constraint as test_send_verified_tmux.py).
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from agentwire.worktree import tmux_safe_name

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not available"
)

# Printable ASCII — the realistic input space for a session name. Control
# characters are excluded: tmux vis-escapes rather than substitutes them, which
# tmux_safe_name deliberately doesn't mirror (see its docstring and
# TestTmuxRewriteSet.test_vis_escaped_chars_are_a_documented_non_goal).
PRINTABLE = [chr(i) for i in range(32, 127)]

# The one PRINTABLE character tmux transforms by vis-escaping rather than
# substituting, so it is excluded from the mapping sweep and gets its own test
# below. Not an oversight — see test_backslash_has_no_fixed_point.
UNMIRRORABLE = "\\"


@pytest.fixture(scope="module")
def tmux_sock():
    """A private tmux server, torn down with the module."""
    sock_dir = Path(tempfile.mkdtemp(prefix="awn-"))
    socket = str(sock_dir / "s")

    def run(*args):
        return subprocess.run(
            ["tmux", "-S", socket, *args], capture_output=True, text=True
        )

    yield run
    run("kill-server")


def _real_name(run, requested: str) -> str | None:
    """Create a session named *requested*; return the name tmux actually used."""
    created = run("new-session", "-d", "-s", requested)
    if created.returncode != 0:
        return None
    listed = run("list-sessions", "-F", "#{session_name}")
    names = [n for n in listed.stdout.splitlines() if n]
    for n in names:
        run("kill-session", "-t", "=" + n)
    return names[0] if names else None


class TestMirrorsRealTmux:
    @pytest.mark.parametrize("requested", [
        ".claude-fix",
        "dotdev.dev-fix-bug",
        "proj:x-fork-1",
        "a.b:c.d",
        "myapp/feat",           # slash is legal — must NOT be rewritten
        "myapp-fix-bug",
        "café-fix",
    ])
    def test_derived_name_is_the_name_tmux_creates(self, tmux_sock, requested):
        """THE invariant. Ask tmux for the raw name; it must land on ours."""
        actual = _real_name(tmux_sock, requested)
        assert actual is not None, f"tmux refused to create {requested!r}"
        assert actual == tmux_safe_name(requested)

    @pytest.mark.parametrize("requested", [
        "_claude-fix", "proj_x-fork-1", "myapp/feat", "myapp-fix-bug",
    ])
    def test_sanitized_names_are_fixed_points_of_real_tmux(self, tmux_sock, requested):
        """A name we hand to new-session must survive it unchanged.

        Creation passes tmux_safe_name output straight to ``-s``; if tmux
        rewrites it again, the recorded name is not the live one all over again.
        """
        assert tmux_safe_name(requested) == requested  # precondition
        assert _real_name(tmux_sock, requested) == requested


class TestRewriteSetIsComplete:
    def test_sweep_of_printable_ascii_matches_our_mapping(self, tmux_sock):
        """Every printable ASCII char, through real tmux, versus our function.

        Catches BOTH directions in one pass: a character tmux rewrites that we
        don't (the #868/#878 bug — a lookup that can never match), and a
        character we rewrite that tmux doesn't (over-sanitizing, which renames
        sessions that were already fine).
        """
        mismatches = []
        for ch in PRINTABLE:
            if ch == UNMIRRORABLE:
                continue
            requested = f"a{ch}b"
            actual = _real_name(tmux_sock, requested)
            if actual is None:
                continue  # tmux refused it outright — not a mapping question
            if actual != tmux_safe_name(requested):
                mismatches.append((ch, actual, tmux_safe_name(requested)))
        assert not mismatches, (
            "tmux_safe_name diverges from real tmux for: "
            + ", ".join(f"{c!r}: tmux={a!r} ours={o!r}" for c, a, o in mismatches)
        )

    def test_measured_substitution_set_is_exactly_dot_and_colon(self, tmux_sock):
        """Pin the set itself, so a tmux upgrade that adds one is visible.

        The sweep above would also fail, but it reports a diff against our
        function; this reports the ground truth independently of it.
        """
        substituted = {
            ch for ch in PRINTABLE
            if (actual := _real_name(tmux_sock, f"a{ch}b")) is not None
            and actual == "a_b" and ch != "_"
        }
        assert substituted == {".", ":"}

    def test_backslash_has_no_fixed_point(self, tmux_sock):
        """Why `\\` is excluded above: it is *unmirrorable*, not merely rare.

        tmux vis-escapes it, and the escaping is NOT idempotent — it re-escapes
        on every pass:

            a\\b  (3 chars) -> a\\\\b  (4)
            a\\\\b (4 chars) -> a\\\\\\\\b (6)

        So no name containing a backslash has a fixed point under
        ``new-session``: there is no string you can hand to ``-s`` that comes
        back unchanged. ``tmux_safe_name``'s entire contract is "the name I
        derive is the name tmux will have chosen", and resolution applies it to
        already-recorded names (``session_cli`` re-sanitizes on read), so a
        mapping that grows on each application would break resolution rather
        than fix it. Leaving it untouched is the only coherent option.

        Unreachable in practice as well: git rejects `\\` in a branch name, and
        the other input is a project directory name.
        """
        once = _real_name(tmux_sock, "a\\b")
        twice = _real_name(tmux_sock, once)
        assert once != "a\\b"          # tmux mangles it
        assert twice != once           # ...and keeps mangling — no fixed point
        assert len(twice) > len(once)  # strictly growing, so no convergence

        # And we leave it alone rather than pretending to mirror it.
        assert tmux_safe_name("a\\b") == "a\\b"
