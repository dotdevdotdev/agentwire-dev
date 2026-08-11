#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["agentwire-dev @ file:///Users/dotdev/projects/agentwire-dev"]
# ///
"""Live-pane round trip for the §4b body: does the #689 heal actually fire?

**Why this exists even though the mechanism is unit-tested.** The unit tests
assert against the real functions with a real captured pane geometry, and that
proves the mechanism. It does not prove the behavior: a green test proves the
FIXTURE's shape. This is the highest-consequence failure in the slice — if the
heal does not fire, a buddy write whose Enter was swallowed wedges permanently
(never healed, never dead-lettered, therefore never emailed) on a channel whose
entire justification is that the owner is not watching a screen.

So this pastes a real rendered message into a REAL Claude Code pane, leaves the
Enter unsent (the swallowed-Enter state), and then runs the actual heal.

Also measures the two caps §4b says to measure rather than guess:

  1. ``flush_session``'s ``stuck`` test is a plain substring match against the
     box content with NO #851 window path, so a single-line body long enough
     that the box renders only a WINDOW of it fails the heal the same way a
     multi-line one does. Where is that boundary, in a real box?
  2. ``VERIFY_SCROLLBACK_LINES`` bounds the dedup capture. A needle that
     scrolls partly out returns False → stays pending → re-pastes → duplicate
     delivery, i.e. "the orchestrator acts twice".

Isolation: creates its OWN throwaway tmux session in this worktree, at a fixed
80x24 so the numbers are reproducible, and kills it at the end — including on
failure. It never touches another session.

**Repoint the ``dependencies`` line above at the checkout you are measuring.**
PEP 723 needs a literal path, and the probe measures whatever that install
holds — running it from a worktree while it points at ``~/projects`` measures
main's renderer under the worktree's name.

Usage:  ./tools/voice_heal_probe.py [--keep] [--width N] [--height N]
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from agentwire import inbox, pane_manager, prompt_router, session_ready
from agentwire.voice_layer import confirm, relay, write_tools

SESSION = "voice-heal-probe"
#: Overridable so the pane-DEPENDENT half of the measurement can be re-taken at
#: the smallest geometry you care about: the box shows a bounded number of ROWS,
#: so a shorter pane windows sooner than 80x24 does.
WIDTH, HEIGHT = 80, 24
WORKTREE = str(Path(__file__).resolve().parents[1])

#: Lengths to probe for the box-window boundary, coarse then fine.
PROBE_LENGTHS = (350, 420, 460, 490, 510, 530, 560, 620, 900)


def sh(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def start_session() -> None:
    # Default permissions on purpose: this pane will genuinely submit at the
    # end of the round trip, and a probe should not be able to act on what it
    # submits. The body is inert either way.
    sh(
        "tmux", "new-session", "-d", "-s", SESSION,
        "-x", str(WIDTH), "-y", str(HEIGHT), "-c", WORKTREE,
        "claude",
    )


def kill_session() -> None:
    sh("tmux", "kill-session", "-t", SESSION)


def wait_for_box(timeout: float = 90.0) -> bool:
    """Wait until Claude Code's input box is parseable and empty."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        capture = pane_manager.capture_pane(SESSION, 0, lines=60)
        if prompt_router.input_box_content(capture) is not None:
            if prompt_router.prompt_is_empty(SESSION, 0):
                return True
        time.sleep(2)
    return False


def clear_box() -> bool:
    return session_ready.clear_input_box(SESSION, 0)


def body_of_length(target: int) -> str:
    """A real rendered body padded to ~*target* chars, via the real renderer."""
    filler = "restart the portal and then report back on what the tests did "
    instruction = (filler * 40)[: max(10, target)]
    return confirm.SEP.join([
        instruction,
        'said: "confirm tango"',
        f"{confirm.POINTER_LABEL}{relay.relay_path('a1b2c3')}",
        "#a1b2c3",
    ])


def rendered(body: str) -> str:
    return inbox.Message(
        id="1700000000000000000-abc123",
        sender="buddy",
        to="orchestrator",
        kind=write_tools.WRITE_KIND,
        text=body,
        ts=1700000000000,
    ).render()


def stuck_matches(line: str, box: str) -> bool:
    """``flush_session``'s stuck test, verbatim."""
    return "".join(line.split()) in "".join(box.split())


def paste_and_measure(line: str) -> dict:
    """Paste WITHOUT Enter and report what the real box does with it.

    Waits for the box to STABILIZE, not merely to be non-empty. A large paste
    renders progressively, so the first non-empty capture is a partial one —
    measuring that reports a box far shorter than the message and makes a
    perfectly healthy paste look like it windowed. (This probe's first run
    reported 38 chars for a 159-char body for exactly that reason.)
    """
    session_ready.paste_no_enter(SESSION, line, pane_index=0)
    deadline = time.time() + 20
    box, previous, stable = "", None, 0
    while time.time() < deadline:
        capture = pane_manager.capture_pane(SESSION, 0, lines=80)
        box = prompt_router.input_box_content(capture) or ""
        if box and box == previous:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        previous = box
        time.sleep(0.6)
    return {
        "len": len(line),
        "box_len": len(box),
        "chip": "[Pasted text" in box,
        "stuck_matches": stuck_matches(line, box),
        "text_landed": session_ready.text_landed(
            pane_manager.capture_pane(SESSION, 0, lines=80), line
        ),
    }


def _int_arg(flag: str, default: int) -> int:
    if flag in sys.argv:
        return int(sys.argv[sys.argv.index(flag) + 1])
    return default


def main() -> int:
    global WIDTH, HEIGHT
    keep = "--keep" in sys.argv
    WIDTH = _int_arg("--width", WIDTH)
    HEIGHT = _int_arg("--height", HEIGHT)
    print(f"Starting throwaway session {SESSION} at {WIDTH}x{HEIGHT} …")
    start_session()
    try:
        if not wait_for_box():
            print("FAIL: Claude Code's input box never became parseable+empty")
            return 2
        print("Input box ready.\n")

        # ---- 1. The round trip on the REAL shipped body ------------------
        # The SHIPPED worst case, not a short one: a body long enough that
        # every slot clips, so the #1015 relay pointer rides too.
        real_body = confirm.render_body(
            "tell the reviewer the branch is ready and the tests were run twice "
            "against a clean checkout, and then start on the follow-up list we "
            "talked through this morning before anything else lands ",
            "okay yeah go ahead and confirm tango please thats the right one and "
            "then start on the follow up list we went through this morning",
            "a1b2c3",
            reply_to="agentwire-dev-voice-confirm-spine",
            full_path=str(relay.relay_path("a1b2c3")),
        )
        assert confirm.POINTER_LABEL in real_body, "probe must exercise the pointer"
        line = rendered(real_body)
        print(f"[1] Round trip on the real rendered body ({len(line)} chars)")
        measured = paste_and_measure(line)
        print(f"    pasted, box holds {measured['box_len']} chars, "
              f"chip={measured['chip']}")
        print(f"    text_landed          : {measured['text_landed']}")
        print(f"    #689 stuck test hits : {measured['stuck_matches']}"
              "   <- the heal's precondition")
        if not measured["stuck_matches"]:
            print("    FAIL: the heal would never fire; message would wedge.")
            return 2

        # The swallowed Enter is now the real state: text in the box, unsent.
        healed = session_ready.finish_submit(SESSION, line, 0)
        print(f"    finish_submit()      : {healed}   <- the heal itself")
        if not healed:
            print("    FAIL: heal did not submit.")
            return 2

        # ---- 2. Dedup needle inside the scrollback window ----------------
        time.sleep(2)
        capture = pane_manager.capture_pane(
            SESSION, 0, lines=session_ready.VERIFY_SCROLLBACK_LINES
        )
        on_scrollback = session_ready.message_on_scrollback(capture, line)
        print(f"    dedup finds it after submit: {on_scrollback}"
              f"   <- inside VERIFY_SCROLLBACK_LINES={session_ready.VERIFY_SCROLLBACK_LINES}")
        if not on_scrollback:
            print("    FAIL: dedup would re-paste — the orchestrator acts twice.")
            return 2
        print("    ROUND TRIP CLOSED.\n")

        # ---- 3. Where does the box-window boundary actually fall? --------
        # The round trip SUBMITTED, so the pane is now processing a turn and
        # its box is neither parseable nor empty. Measuring through that
        # reports box=0 for every length and reads as "the cliff is below
        # everything we probed" — a probe measuring its own sequencing.
        print("Waiting for the pane to finish the submitted turn …")
        if not wait_for_box(timeout=240.0):
            print("FAIL: the box never came back after the round trip")
            return 2
        print("[2] Measuring the stuck-test boundary in a real box")
        results = []
        for target in PROBE_LENGTHS:
            clear_box()
            probe_line = rendered(body_of_length(target))
            m = paste_and_measure(probe_line)
            results.append(m)
            print(f"    body {m['len']:>5} chars -> box {m['box_len']:>5}, "
                  f"chip={str(m['chip']):<5} stuck={m['stuck_matches']}")
        clear_box()

        ok = [m["len"] for m in results if m["stuck_matches"]]
        bad = [m["len"] for m in results if not m["stuck_matches"]]
        print()
        print(f"    largest body the stuck test still finds: {max(ok) if ok else 'none'}")
        print(f"    smallest that it does NOT:               {min(bad) if bad else 'none of those probed'}")
        print(f"    MAX_BODY_CHARS is currently {confirm.MAX_BODY_CHARS}")
        if ok and confirm.MAX_BODY_CHARS > max(ok):
            print("    WARNING: the cap is above the measured boundary.")

        # ---- 4. Control characters — the second route to the same wedge ----
        print("\n[3] Control characters in the body (post-strip)")
        for label, raw in (
            ("ansi_escape", "restart \x1b[31mthe portal\x1b[0m now"),
            ("bel_soh", "restart the\x07 portal\x01 now"),
            ("clean_control", "restart the portal now"),
        ):
            clear_box()
            body = confirm.render_body(raw, "confirm tango", "a1b2c3")
            line = rendered(body)
            m = paste_and_measure(line)
            has_ctrl = any(ord(c) < 0x20 and c not in "\t" for c in line)
            print(f"    {label:<14} raw_ctrl_survived={has_ctrl!s:<5} stuck={m['stuck_matches']}")
            if has_ctrl or not m["stuck_matches"]:
                print(f"    FAIL: {label} would wedge.")
                return 2
        clear_box()

        # ---- 5. The variable that ACTUALLY governs: coalesced length ------
        # flush_session coalesces the whole queue into ONE paste
        # (inbox.py:1059, "\n".join(m.render() ...)) and then tests EACH
        # message's render against that single box. So no per-message cap can
        # bound what gets pasted — and the coalesced blob is MULTI-LINE BY
        # CONSTRUCTION, because the join is a newline.
        print("\n[4] Coalesced drain — per-message cap does NOT bound this")
        one = rendered(confirm.render_body(
            "restart the portal and report what the tests did", "confirm tango", "a1b2c3"
        ))
        for count in (1, 2, 3, 4):
            clear_box()
            batch = [one] * count
            blob = "\n".join(batch)
            session_ready.paste_no_enter(SESSION, blob, pane_index=0)
            deadline, box, prev, stable = time.time() + 20, "", None, 0
            while time.time() < deadline:
                b = prompt_router.input_box_content(
                    pane_manager.capture_pane(SESSION, 0, lines=80)
                ) or ""
                if b and b == prev:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                prev = b
                time.sleep(0.6)
            box = prev or ""
            hits = sum(1 for m in batch if stuck_matches(m, box))
            print(f"    {count} message(s), {len(blob):>5} chars -> box {len(box):>5}, "
                  f"chip={'[Pasted text' in box!s:<5} stuck hits {hits}/{count}")
        clear_box()
        print("\n    Any count where hits < count is a permanently wedged message")
        print("    on a swallowed Enter: never healed, never dead-lettered,")
        print("    therefore never emailed. That is #930, not this slice.")
        return 0
    finally:
        if keep:
            print(f"\n(keeping {SESSION} — kill it with: tmux kill-session -t {SESSION})")
        else:
            kill_session()
            print(f"\nKilled {SESSION}.")


if __name__ == "__main__":
    sys.exit(main())
