#!/usr/bin/env python3
"""Instrumented paste-delivery repro for #889 / #867.

Answers one question with a measurement instead of an argument: **does the
pre-#890 blind paste path leave a large task prompt sitting unsubmitted in
Claude Code's input box?**

#867's signature is an unattended `memory-manager` dispatch that made zero
tool calls for two hours. The leading theory is that its ~21 KB / 524-line
prompt was pasted by `pane_manager.send_to_target` — paste, `sleep(1.0)`,
Enter, `sleep(0.5)`, Enter, no verification, `None` return — and never
submitted, so `ensure` waited for a completion signal that could not arrive.

The harness drives BOTH paths against a throwaway Claude session at a range
of payload sizes:

  blind     `pane_manager.send_to_target(target, text, enter=True)`
            — the exact pre-#890 call `ensure_cli.py:734` made.
  verified  `session_ready.send_verified(...)`  — what `ensure` calls today
            via `ensure_cli.send_task_prompt` (#890).

Deliberately NOT a single-payload test. #901/#898/#902 all shipped past green
suites because the fixture decided what the test could see, and a one-size
send cannot see a size-dependent race. The sweep varies payload size across
an order of magnitude and repeats each size, because the failure is
intermittent (#867 hung on 08-04 and succeeded on 08-05 on the same code).

Isolation: creates its OWN tmux session in a temp directory and kills it on
exit. Touches no registered agentwire session, no scheduler, no live daemon.

RESULT (2026-08-05, macOS, idle host, tmux default 80x24): **negative.**
36/36 deliveries submitted — 18 blind, 18 verified — across 1 KB → 80 KB
(27 → 2002 lines), every one confirmed on scrollback within 35 ms of the send
call returning. The blind path's fixed ``sleep(1.0)`` did not lose a paste
even at ~4x memory-manager's payload. #867's actual cause was found upstream
and is unrelated: the 08-04 conversation transcript shows the 20,433-byte
prompt arriving as a ``user`` message at 08:00:20.785Z and the turn being
rejected 15 ms later with ``error: authentication_failed`` — an expired
Claude login, which agentwire has no detector for (#906).

This does NOT reproduce a bogged-down host, which is the condition #889's
theory turns on, so it bounds the blind path's failure rate rather than
proving it cannot fail. Keep it as the regression harness for any future
"the prompt never submitted" claim: run it before theorizing.

Usage:
    uv run python scripts/repro_889_paste_delivery.py
    uv run python scripts/repro_889_paste_delivery.py --sizes 20000,40000 --trials 5
    uv run python scripts/repro_889_paste_delivery.py --mode blind --keep
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentwire import pane_manager, prompt_router, session_ready  # noqa: E402

SESSION = "probe-889-repro"
PANE = 0

# How long, AFTER the send call returns, we keep watching for the prompt to
# submit. Generous on purpose: pre-#890 `ensure` did not wait at all — it went
# straight into `wait_for_completion_signal`, which polls for hours. A paste
# that submits at t=10s is a slow success, not the #867 failure. Only a box
# still holding the text at the end of this window is the hang.
SETTLE = 30.0
POLL = 0.25

# One filler line, sized so ~40 chars/line reproduces memory-manager's shape:
# 524 lines at ~21 KB. Numbered so a truncated paste is visible in the capture.
FILLER = "{i:05d} audit finding filler payload line"

TAIL = (
    "\n\nThis is an automated DELIVERY TEST for agentwire issue #889.\n"
    "Do NOT use any tools. Do NOT read or write any files.\n"
    "Reply with exactly one word: ACK\n"
)


@dataclass
class Trial:
    mode: str
    size: int
    lines: int
    trial: int
    outcome: str          # submitted | stuck | vanished
    submit_seconds: float | None
    call_seconds: float
    call_returned: object  # None for blind, bool for verified
    box_len_final: int
    recovered: bool


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------

def make_payload(target_bytes: int) -> str:
    body_budget = max(target_bytes - len(TAIL), 0)
    lines: list[str] = []
    total = 0
    i = 0
    while total < body_budget:
        line = FILLER.format(i=i)
        lines.append(line)
        total += len(line) + 1
        i += 1
    return "\n".join(lines) + TAIL


# --------------------------------------------------------------------------
# session lifecycle (throwaway, ours alone)
# --------------------------------------------------------------------------

def session_exists(name: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    ).returncode == 0


def start_session(workdir: Path) -> None:
    if session_exists(SESSION):
        raise SystemExit(
            f"tmux session '{SESSION}' already exists — refusing to reuse it. "
            f"Kill it first: tmux kill-session -t {SESSION}"
        )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", SESSION, "-c", str(workdir)],
        check=True,
    )
    time.sleep(0.2)
    # Same posture the scheduler dispatches with (bypass). Launched directly
    # rather than through `agentwire new` so nothing lands in the session
    # registry / cohort ledger.
    subprocess.run(
        ["tmux", "send-keys", "-t", SESSION,
         "claude --dangerously-skip-permissions", "Enter"],
        check=True,
    )
    print(f"[setup] session '{SESSION}' launched in {workdir}")
    if not session_ready.wait_for_session_ready(SESSION, timeout=90):
        dump_pane()
        raise SystemExit("[setup] agent never became ready")
    print("[setup] agent ready")


def kill_session() -> None:
    subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True)


def dump_pane(lines: int = 40) -> None:
    try:
        print(pane_manager.capture_pane(SESSION, PANE, lines=lines))
    except Exception as exc:  # pragma: no cover - diagnostic only
        print(f"(capture failed: {exc})")


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def snapshot() -> str:
    return pane_manager.capture_pane(
        SESSION, PANE, lines=session_ready.VERIFY_SCROLLBACK_LINES
    )


def box_text() -> str:
    try:
        return session_ready.input_box(snapshot()) or ""
    except Exception:
        return ""


def box_is_empty() -> bool:
    try:
        return prompt_router.prompt_is_empty(SESSION, PANE)
    except Exception:
        return False


def wait_idle(timeout: float = 60.0) -> bool:
    """Wait for the box to be empty AND the agent to stop working."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cap = snapshot()
        if box_is_empty() and not session_ready.pane_shows_activity(cap[-2000:]):
            return True
        time.sleep(0.5)
    return False


def observe(message: str, marker: str) -> tuple[str, float | None, int]:
    """Watch for SETTLE seconds. Returns (outcome, submit_seconds, box_len)."""
    t0 = time.monotonic()
    deadline = t0 + SETTLE
    while True:
        cap = snapshot()
        # Positive proof of submission: the marker rides INSIDE the pasted
        # text, so it can only appear on scrollback OUTSIDE the input box if
        # THIS paste actually submitted (#839).
        if session_ready.message_on_scrollback(cap, marker):
            return "submitted", time.monotonic() - t0, len(box_text())
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL)
    box = box_text()
    if not box.strip():
        # Neither on scrollback nor in the box: the paste never rendered at
        # all. Distinct from "stuck" and just as fatal to the caller.
        return "vanished", None, 0
    # "stuck" is only meaningful if it is OUR text sitting there. A box
    # holding something else would mean the trial never got a clean start.
    if session_ready.box_shows_message(box, message):
        return "stuck", None, len(box)
    return "stuck-foreign", None, len(box)


def recover() -> bool:
    """Return the pane to an idle, empty-box state between trials."""
    # Interrupt any turn the submitted prompt started; then clear the box.
    pane_manager.run_command(
        ["tmux", "send-keys", "-t", f"{SESSION}.{PANE}", "Escape"], timeout=5
    )
    time.sleep(1.0)
    if not box_is_empty():
        session_ready.clear_input_box(SESSION, PANE)
    return wait_idle(timeout=45.0)


def run_trial(mode: str, size: int, trial_no: int) -> Trial:
    payload = make_payload(size)
    marker = session_ready.new_delivery_marker()
    tagged = session_ready.tag_message(payload, marker)
    lines = tagged.count("\n") + 1

    if not box_is_empty():
        session_ready.clear_input_box(SESSION, PANE)

    t0 = time.monotonic()
    returned: object
    if mode == "blind":
        # The exact pre-#890 call from ensure_cli.py:734. No revert on disk —
        # the blind primitive still exists and is still called elsewhere
        # (usage_limit, council, channels), so it can be driven directly.
        pane_manager.send_to_target(f"{SESSION}.{PANE}", tagged, enter=True)
        returned = None
    elif mode == "verified":
        returned = session_ready.send_verified(
            SESSION, tagged, marker=marker, retries=0, pane_index=PANE
        )
    else:  # pragma: no cover
        raise ValueError(mode)
    call_seconds = time.monotonic() - t0

    outcome, submit_seconds, box_len = observe(tagged, marker)
    recovered = recover()

    t = Trial(
        mode=mode, size=len(tagged), lines=lines, trial=trial_no,
        outcome=outcome, submit_seconds=submit_seconds,
        call_seconds=round(call_seconds, 2), call_returned=returned,
        box_len_final=box_len, recovered=recovered,
    )
    sub = f"{submit_seconds:.1f}s" if submit_seconds is not None else "—"
    print(
        f"  [{mode:8s}] {len(tagged):>6d}B / {lines:>4d}L  #{trial_no}  "
        f"call={call_seconds:5.1f}s  ret={returned!s:<5}  "
        f"-> {t.outcome.upper():9s} submit={sub:>6s} "
        f"box={box_len:>6d}{'' if recovered else '  !! NOT RECOVERED'}"
    )
    return t


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sizes", default="1000,5000,10000,21000,40000,80000",
        help="comma-separated payload byte targets",
    )
    ap.add_argument("--trials", type=int, default=3, help="trials per size")
    ap.add_argument(
        "--mode", default="both", choices=["blind", "verified", "both"],
    )
    ap.add_argument("--keep", action="store_true", help="leave the session up")
    ap.add_argument("--out", default="", help="write JSON results here")
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    modes = ["blind", "verified"] if args.mode == "both" else [args.mode]

    if not shutil.which("claude"):
        raise SystemExit("`claude` not on PATH")

    workdir = Path(tempfile.mkdtemp(prefix="repro889-"))
    results: list[Trial] = []
    try:
        start_session(workdir)
        for mode in modes:
            print(f"\n=== {mode} path ===")
            for size in sizes:
                for n in range(1, args.trials + 1):
                    results.append(run_trial(mode, size, n))
    finally:
        if not args.keep:
            kill_session()
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"\n[keep] session '{SESSION}' left up; workdir {workdir}")

    print("\n=== summary ===")
    print(f"{'mode':9s} {'bytes':>7s} {'lines':>6s} {'submitted':>10s} "
          f"{'stuck':>6s} {'vanished':>9s} {'median submit':>14s}")
    for mode in modes:
        for size in sizes:
            rows = [r for r in results if r.mode == mode
                    and abs(r.size - size) < max(200, size * 0.05)]
            if not rows:
                continue
            ok = [r for r in rows if r.outcome == "submitted"]
            times = sorted(r.submit_seconds for r in ok if r.submit_seconds)
            med = f"{times[len(times) // 2]:.1f}s" if times else "—"
            print(
                f"{mode:9s} {rows[0].size:>7d} {rows[0].lines:>6d} "
                f"{len(ok):>10d} "
                f"{sum(1 for r in rows if r.outcome == 'stuck'):>6d} "
                f"{sum(1 for r in rows if r.outcome == 'vanished'):>9d} "
                f"{med:>14s}"
            )

    if args.out:
        Path(args.out).write_text(
            json.dumps([asdict(r) for r in results], indent=2, default=str)
        )
        print(f"\nwrote {args.out}")

    stuck = [r for r in results if r.outcome != "submitted"]
    print(f"\n{len(stuck)}/{len(results)} deliveries did NOT submit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
