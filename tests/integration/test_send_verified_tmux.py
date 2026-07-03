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
# command. Reads stdin byte-by-byte in raw mode; \r submits, \n is literal.
EMULATOR = r'''
import os, sys, tty
RULE = "─" * 20
buf = ""

def draw():
    # Clear screen, render only the input box (submitted turns scrolled away).
    # Render embedded newlines with \r\n so a multi-line buffer displays as
    # clean stacked rows between the rules (Claude's pasted-text rendering).
    sys.stdout.write("\x1b[2J\x1b[H")
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
        ch = data[:1].decode("utf-8", "replace"); data = data[1:]
        progressed = True
        if in_paste:
            buf += ch                # pasted bytes are literal (incl. \n / \r)
        elif ch == "\r":             # real Enter keystroke -> submit
            if buf:
                buf = ""
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
def emulator_session(tmp_path):
    script = tmp_path / "claude_box_emulator.py"
    script.write_text(EMULATOR)
    session = f"awtest-{uuid.uuid4().hex[:8]}"
    # Dedicated server socket so we never touch the user's live tmux. The socket
    # path MUST be short — macOS caps Unix-domain socket paths at ~104 bytes, and
    # pytest's tmp_path easily blows past that (silently failing new-session), so
    # keep it in a short temp dir keyed by a tiny uuid.
    sock_dir = Path(tempfile.mkdtemp(prefix="awt-"))
    socket = str(sock_dir / "s")

    def tmux_s(*args):
        return subprocess.run(
            ["tmux", "-S", socket, *args], capture_output=True, text=True
        )

    tmux_s("new-session", "-d", "-s", session, "-x", "120", "-y", "40",
           f"{sys.executable} {script}")
    # Wait for the box to render.
    deadline = time.time() + 5
    while time.time() < deadline:
        if "❯" in subprocess.run(
            ["tmux", "-S", socket, "capture-pane", "-t", f"{session}.0", "-p"],
            capture_output=True, text=True).stdout:
            break
        time.sleep(0.1)
    yield session, socket, tmux_s
    tmux_s("kill-session", "-t", session)


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
