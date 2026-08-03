"""Real-tmux integration test for session_ready.send_verified (#621).

The unit tests mock paste/Enter/capture; this drives an ACTUAL tmux pane through
the real `paste-buffer` + `send-keys Enter` + `capture-pane` round-trip, so a
regression in the paste→land→submit→confirm mechanic (the failure behind both
the polite-msg redelivery loop and notify-parent "sat there unsent") is caught.

The pane runs a tiny terminal app that emulates Claude Code's input box: it
renders the `❯`-prefixed box between two horizontal rules (so
`prompt_router.input_box_content` parses it), treats newline bytes from a paste
as LITERAL text (Claude's bracketed-paste semantics — pasted newlines don't
submit), and treats a carriage-return (the real Enter keystroke tmux sends) as
SUBMIT: the buffer clears and the turn scrolls into history. Crucially the
emulator shows NO spinner/activity after submit and lets the turn scroll out of
view — the exact "quiet submit" shape that false-negatived the old check.
"""

import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from agentwire import session_ready

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not available"
)

# A self-contained emulator — written to a temp file and run as the pane's
# command. Reads stdin byte-by-byte in raw mode; \r submits, \n is literal,
# \x7f (backspace) erases. An optional argv[1] delay renders the banner+box
# immediately but leaves stdin unconsumed for that many seconds — the #695
# "input handler not wired yet" window (keystrokes buffer in the PTY).
EMULATOR = r'''
import os, sys, time, tty
RULE = "─" * 20
buf = ""
# argv[2]: cap the box at N visible rows — Claude Code's input box has a
# bounded height and SCROLLS, so a draft taller than it renders only its tail
# (#851). 0 (default) keeps the old unbounded rendering.
MAX_ROWS = int(sys.argv[2]) if len(sys.argv) > 2 else 0

def wrap(text, width):
    out = []
    for line in text.split("\n"):
        while len(line) > width:
            out.append(line[:width]); line = line[width:]
        out.append(line)
    return out

def draw():
    # Clear screen, render only the input box (submitted turns scrolled away).
    # Render embedded newlines with \r\n so a multi-line buffer displays as
    # clean stacked rows between the rules (Claude's pasted-text rendering).
    sys.stdout.write("\x1b[2J\x1b[H")
    if MAX_ROWS:
        try:
            width = os.get_terminal_size(sys.stdout.fileno()).columns
        except OSError:
            width = 80
        rows = wrap(buf, width - 2) if buf else [""]
        rows = rows[-MAX_ROWS:]          # only the tail window is on screen
        body = "\r\n".join(rows)         # glyph goes on the first VISIBLE row
    else:
        body = buf.replace("\n", "\r\n")
    glyph = "❯ " + body if buf else "❯"
    sys.stdout.write(RULE + "\r\n" + glyph + "\r\n" + RULE + "\r\n")
    sys.stdout.flush()

# Enable bracketed-paste mode (DECSET 2004) so tmux paste-buffer wraps pasted
# content in \e[200~..\e[201~ and does NOT convert its newlines to carriage
# returns — exactly how Claude Code keeps pasted newlines from submitting.
sys.stdout.write("\x1b[?2004h")
sys.stdout.flush()
tty.setraw(sys.stdin.fileno())
draw()

# Simulated late input wiring (#695): the screen is up (banner + box) but
# nothing consumes stdin yet — keystrokes sent now buffer in the PTY and are
# dumped into the loop all at once when the "handler" finally wires.
if len(sys.argv) > 1:
    time.sleep(float(sys.argv[1]))

data = b""
in_paste = False
START = b"\x1b[200~"
END = b"\x1b[201~"
while True:
    chunk = os.read(sys.stdin.fileno(), 4096)
    if not chunk:
        break
    data += chunk
    progressed = True
    while data and progressed:
        progressed = False
        marker = END if in_paste else START
        if data.startswith(marker):
            data = data[len(marker):]; in_paste = not in_paste
            progressed = True; continue
        # If the remaining bytes could be the start of the marker, wait for more.
        if marker.startswith(data):
            break
        # Consume a WHOLE utf-8 sequence, not one byte: delivery markers are
        # non-ASCII by design (⟨#send-xxxxxx⟩, mirroring inbox's ⟨#id⟩), and a
        # byte-at-a-time decode turns each into three replacement chars, so
        # the box would never render what was actually pasted.
        lead = data[0]
        n = 4 if lead >= 0xF0 else 3 if lead >= 0xE0 else 2 if lead >= 0xC0 else 1
        if len(data) < n:
            break                    # rest of the sequence hasn't arrived yet
        ch = data[:n].decode("utf-8", "replace"); data = data[n:]
        progressed = True
        if in_paste:
            buf += ch                # pasted bytes are literal (incl. \n / \r)
        elif ch == "\r":             # real Enter keystroke -> submit
            if buf:
                buf = ""
        elif ch == "\x7f":           # backspace keystroke -> erase one char
            buf = buf[:-1]
        elif ch == "\x03":
            sys.exit(0)
        else:
            buf += ch
    draw()
'''


def _tmux(*args, **kw):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, **kw)


def _capture(session):
    return _tmux("capture-pane", "-t", f"{session}.0", "-p").stdout


@pytest.fixture
def emulator_factory(tmp_path):
    """Spawn emulator panes on a private tmux socket; kill them on teardown.

    ``make(wire_delay=N)`` renders the banner+box immediately but leaves the
    emulator's stdin unconsumed for N seconds — the #695 unwired window.
    """
    script = tmp_path / "claude_box_emulator.py"
    script.write_text(EMULATOR)
    created = []

    def make(wire_delay: float = 0.0, box_rows: int = 0,
             width: int = 120, height: int = 40):
        session = f"awtest-{uuid.uuid4().hex[:8]}"
        # Dedicated server socket so we never touch the user's live tmux. The
        # socket path MUST be short — macOS caps Unix-domain socket paths at
        # ~104 bytes, and pytest's tmp_path easily blows past that (silently
        # failing new-session), so keep it in a short temp dir.
        sock_dir = Path(tempfile.mkdtemp(prefix="awt-"))
        socket = str(sock_dir / "s")

        def tmux_s(*args):
            return subprocess.run(
                ["tmux", "-S", socket, *args], capture_output=True, text=True
            )

        cmd = f"{sys.executable} {script}"
        if wire_delay or box_rows:
            cmd += f" {wire_delay}"
        if box_rows:
            cmd += f" {box_rows}"
        tmux_s("new-session", "-d", "-s", session,
               "-x", str(width), "-y", str(height), cmd)
        # Wait for the box to render.
        deadline = time.time() + 5
        while time.time() < deadline:
            if "❯" in subprocess.run(
                ["tmux", "-S", socket, "capture-pane", "-t", f"{session}.0", "-p"],
                capture_output=True, text=True).stdout:
                break
            time.sleep(0.1)
        created.append((session, tmux_s))
        return session, socket, tmux_s

    yield make
    for session, tmux_s in created:
        tmux_s("kill-session", "-t", session)


@pytest.fixture
def emulator_session(emulator_factory):
    return emulator_factory()


def _patch_pane_manager(monkeypatch, socket):
    """Point pane_manager's tmux calls at our private socket."""
    from agentwire import pane_manager

    real_run = pane_manager.run_command

    def run_command(cmd, **kw):
        if cmd and cmd[0] == "tmux":
            cmd = ["tmux", "-S", socket, *cmd[1:]]
        return real_run(cmd, **kw)

    monkeypatch.setattr(pane_manager, "run_command", run_command)

    def capture_pane(session, pane_index=0, lines=60):
        out = subprocess.run(
            ["tmux", "-S", socket, "capture-pane", "-t",
             f"{session}.{pane_index}", "-p"],
            capture_output=True, text=True)
        return out.stdout

    monkeypatch.setattr(pane_manager, "capture_pane", capture_pane)


def test_single_line_submits_and_confirms(emulator_session, monkeypatch):
    session, socket, _ = emulator_session
    _patch_pane_manager(monkeypatch, socket)

    ok = session_ready.send_verified(session, "hello orchestrator")
    assert ok, "send_verified should confirm a real single-line submit"
    # Box is back to empty (the turn submitted and scrolled away).
    deadline = time.time() + 3
    box = ""
    while time.time() < deadline:
        box = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
        if "hello orchestrator" not in box:
            break
        time.sleep(0.1)
    assert "hello orchestrator" not in box  # cleared, not sitting unsent


def test_quiet_submit_is_confirmed_not_false_negatived(emulator_session, monkeypatch):
    # The #621 regression: the paste lands and submits, and the pane goes QUIET
    # (the emulator shows no spinner and the turn scrolls off). The old confirm
    # demanded a spinner / echoed turn and so reported the landed-and-submitted
    # paste as unverified — the redelivery loop / notify "sat there unsent". This
    # asserts send_verified confirms it against a real pane.
    session, socket, _ = emulator_session
    _patch_pane_manager(monkeypatch, socket)

    ok = session_ready.send_verified(session, "quiet report no spinner here")
    assert ok
    box = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    # No activity markers on screen — pure quiet confirm.
    assert not session_ready.pane_shows_activity(box)
    assert "quiet report no spinner here" not in box


def test_stuck_paste_finished_enter_only_no_duplicate(emulator_session, monkeypatch):
    # #689: a prior delivery pasted the message but its Enter was swallowed —
    # the message sits rendered in the input box. finish_submit must heal it
    # with Enter ONLY (no re-paste, so the #621 dedup holds: the emulator's
    # buffer would show the text twice if a second paste happened).
    session, socket, _ = emulator_session
    _patch_pane_manager(monkeypatch, socket)

    msg = "[MSG from worker - done] PR 42 drafted  (#deadbe)"
    # Simulate the stuck state: paste lands, Enter never fires.
    session_ready.paste_no_enter(session, msg)
    deadline = time.time() + 5
    while time.time() < deadline:
        cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
        if session_ready.text_landed(cap, msg):
            break
        time.sleep(0.1)
    cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    assert session_ready.text_landed(cap, msg), "stuck-state setup failed"
    # The stuck message must NOT read as delivered/on-scrollback (#689).
    assert not session_ready.message_on_scrollback(cap, msg)

    assert session_ready.finish_submit(session, msg)
    cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    box = session_ready.input_box(cap)
    assert box == "", f"box should be empty after finish_submit, got: {box!r}"
    # No duplicate: the emulator clears on submit; a re-paste would have left a
    # second copy sitting in the box.
    assert msg not in (box or "")


def test_wait_ready_probe_roundtrip_real_tmux(emulator_factory, monkeypatch):
    # #695: against a wired pane, readiness confirms via the probe round-trip
    # (type a char, see it render, erase it) and leaves the box clean for the
    # real paste — which then delivers end-to-end.
    session, socket, _ = emulator_factory()
    _patch_pane_manager(monkeypatch, socket)

    assert session_ready.wait_for_session_ready(session, timeout=15)
    cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    assert session_ready.input_box(cap) == "", "probe left residue in the box"
    assert session_ready.send_verified(session, "the seed prompt")


def test_wait_ready_holds_until_input_handler_wired(emulator_factory, monkeypatch):
    # #695 live repro shape: banner + input box render immediately, but stdin
    # is not consumed for 3s (keystrokes buffer in the PTY — the unwired
    # window). The pre-#695 rule (two identical 500ms frames) declared ready
    # ~1s in and pasted into the void; the probe must hold readiness until the
    # handler actually consumes keystrokes, then clean up every buffered probe.
    session, socket, _ = emulator_factory(wire_delay=3.0)
    _patch_pane_manager(monkeypatch, socket)

    t0 = time.time()
    assert session_ready.wait_for_session_ready(session, timeout=30)
    assert time.time() - t0 >= 2.5, "declared ready inside the unwired window"
    cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    assert session_ready.input_box(cap) == "", "buffered probes not erased"
    # The seed that used to fragment/sit unsubmitted now delivers.
    assert session_ready.send_verified(session, "seed after late wiring")


# The production message shape behind #851: one long line (Claude only
# collapses MULTI-line pastes to a [Pasted text] chip, so this one has no chip
# fallback), too tall for the box's visible region once it wraps.
TALL_MSG = (
    "Review the file-based memory store for this project and prune whatever "
    "has "
    "rotted. The current memories are in the store's REVIEW.md. Verify each "
    "one against the current state of this repo before deciding anything. "
    "Bump verified: on the "
    "memories you confirm, delete the ones the code now contradicts, and merge "
    "duplicates into a single file. Report back with a one-paragraph summary "
    "naming any systemic pattern you noticed (vs a one-off), and send it back "
    "with the command agentwire msg send --to memory-manager --kind done."
)


def _patch_prompt_router(monkeypatch, socket):
    """Point prompt_router's own tmux captures at our private socket.

    prompt_router reads the pane through ``usage_limit._capture`` → ``_tmux``,
    not through pane_manager, so a test that exercises ``prompt_is_empty``
    (clear_input_box) must redirect that path too — otherwise it inspects the
    developer's live tmux server.
    """
    from agentwire import usage_limit

    real = usage_limit._tmux
    monkeypatch.setattr(
        usage_limit, "_tmux", lambda args, timeout=5: real(["-S", socket, *args], timeout)
    )


def test_tall_single_line_draft_delivers(emulator_factory, monkeypatch):
    # #851: an 80x24 pane whose input box shows at most 3 rows. A 500+ char
    # single-line message renders only its TAIL there, so the pre-fix landing
    # gate (full message must be visible in the box) could never pass — Enter
    # was never pressed and the prompt sat unsent, which is how a 4-child
    # fan-out hung silently.
    session, socket, _ = emulator_factory(box_rows=3, width=80, height=24)
    _patch_pane_manager(monkeypatch, socket)

    assert len(TALL_MSG) > 500
    assert session_ready.send_verified(session, TALL_MSG)
    box = session_ready.input_box(
        _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    )
    assert box == "", f"draft still sitting unsent: {box!r}"


def test_tall_draft_retry_does_not_double_paste(emulator_factory, monkeypatch):
    # The corruption half of #851: the whole-send retry could not recognize its
    # OWN landed paste through the window, so it pasted again — the child's
    # transcript showed the 517-char instruction concatenated with itself.
    # Here Enter is never delivered (press_enter stubbed out), so every retry
    # re-enters the pre-paste guard against a box holding only the tail window.
    session, socket, _ = emulator_factory(box_rows=3, width=80, height=24)
    _patch_pane_manager(monkeypatch, socket)
    monkeypatch.setattr(session_ready, "press_enter", lambda s, pane_index=0: None)
    # Every submit attempt is doomed (no Enter ever reaches the pane), so keep
    # the per-attempt budget short — this test is about the PASTE count.
    monkeypatch.setattr(session_ready, "SUBMIT_BUDGET", 1.0)
    monkeypatch.setattr(session_ready, "MIN_ENTER_ATTEMPTS", 1)

    real_paste = session_ready.paste_no_enter
    pastes = []

    def counting_paste(s, m, pane_index=0):
        pastes.append(m)
        real_paste(s, m, pane_index=pane_index)

    monkeypatch.setattr(session_ready, "paste_no_enter", counting_paste)

    assert not session_ready.send_verified(session, TALL_MSG, retries=2)
    # Three whole-send attempts, ONE paste: attempts 2 and 3 recognized their
    # own draft through the window instead of stacking another copy on it.
    # (A visible-window check can't catch this — the tail of a doubled draft is
    # still a suffix of the message. The paste count is the real invariant.)
    assert len(pastes) == 1, f"re-pasted a landed draft {len(pastes)}x"


def test_foreign_draft_is_never_pasted_over(emulator_factory, monkeypatch):
    # #845 against a real pane: a stale draft (a previous sender's message
    # whose Enter was swallowed) sits unsubmitted in the box. The pre-fix
    # guard only knew "does the box hold OUR message" and fell through to
    # paste_no_enter, concatenating the two drafts into one string that the
    # next Enter submits as a single garbled turn. This asserts the real
    # emulator's buffer is left holding EXACTLY the stale draft.
    session, socket, _ = emulator_factory()
    _patch_pane_manager(monkeypatch, socket)
    _patch_prompt_router(monkeypatch, socket)

    stale = "[MSG from other-worker - done] PR 41 drafted, needs review"
    session_ready.paste_no_enter(session, stale)
    deadline = time.time() + 5
    while time.time() < deadline:
        cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
        if session_ready.text_landed(cap, stale):
            break
        time.sleep(0.1)
    assert session_ready.text_landed(cap, stale), "stale-draft setup failed"

    ours = "run the full suite and report back"
    assert session_ready.box_holds_foreign_draft(session, ours)
    assert not session_ready.send_verified(session, ours, retries=1)

    box = session_ready.input_box(
        _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    )
    assert box == stale, f"box was mutated: {box!r}"
    assert ours not in box  # no concatenation — the whole point of #845


def test_tagged_paste_delivers_and_the_marker_lands_on_scrollback(
        emulator_factory, monkeypatch):
    # #839 against a real pane: the per-attempt marker rides inside the paste,
    # so after a genuine submit it is findable OUTSIDE the input box — which
    # is what makes _recover_unverified_send's "already_delivered" a fact
    # rather than a text-similarity guess. The emulator scrolls submitted
    # turns away, so we keep the turn on screen by checking the box first.
    session, socket, _ = emulator_factory()
    _patch_pane_manager(monkeypatch, socket)

    marker = session_ready.new_delivery_marker()
    tagged = session_ready.tag_message("continue", marker)
    assert session_ready.send_verified(session, tagged, marker=marker)

    cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    assert session_ready.input_box(cap) == "", "tagged draft sat unsent"
    # A DIFFERENT attempt's marker must never match this one's echo.
    assert not session_ready.message_on_scrollback(
        cap, session_ready.new_delivery_marker())


def test_clear_input_box_empties_a_tall_draft(emulator_factory, monkeypatch):
    # #851: Escape is inert on a tall draft (the emulator, like Claude, does
    # nothing with it), so "inbox_stuck" was terminal — the drain's empty-box
    # gate stayed blocked by the very draft it was meant to replace. The
    # backspace escalation must actually empty it.
    session, socket, _ = emulator_factory(box_rows=3, width=80, height=24)
    _patch_pane_manager(monkeypatch, socket)
    _patch_prompt_router(monkeypatch, socket)

    session_ready.paste_no_enter(session, TALL_MSG)
    deadline = time.time() + 10
    while time.time() < deadline:
        cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
        if session_ready.text_landed(cap, TALL_MSG):
            break
        time.sleep(0.1)
    assert session_ready.text_landed(cap, TALL_MSG), "stuck-draft setup failed"

    assert session_ready.clear_input_box(session)
    cap = _tmux("-S", socket, "capture-pane", "-t", f"{session}.0", "-p").stdout
    assert session_ready.input_box(cap) == ""
