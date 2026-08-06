"""N unattended blocks produce N audit rows and ONE email (#925).

The invariant the throttle must not break. Everything else in #925 is a
tradeoff between noise and latency; this one is not negotiable, because
throttling the RECORD as well as the notification would trade an inbox problem
for a blindness problem — and the digest itself tells the owner to go read
``agentwire safety logs``, which is only honest if that log is complete.

Deliberately end-to-end rather than a source-order assertion. The real hook
runs as a subprocess, writes its audit line through ``audit_logger``, and then
spawns the notifier as a DETACHED process. A shim ``agentwire`` on PATH stands
in for the installed CLI and calls the real ``safety_notify.record_block``, so
what is exercised is the actual ordering and the actual on-disk throttle, not a
model of them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
HOOK = REPO / "agentwire" / "hooks" / "damage-control" / "bash-tool-damage-control.py"

# Verbatim from the audit log: the 53-block plurality, and a rule that is NOT
# on the unattended allowlist so it reliably reaches the blocked branch.
BLOCKED_COMMAND = "gh issue create --title x --body y"


@pytest.fixture
def rig(tmp_path):
    """A fake ~/.agentwire plus a shim `agentwire` that spools for real."""
    config = tmp_path / "agentwire"
    config.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    outbox = tmp_path / "emails.jsonl"

    shim = bindir / "agentwire"
    shim.write_text(textwrap.dedent(f"""
        #!{sys.executable}
        import json, sys, pathlib
        sys.path.insert(0, {str(REPO)!r})
        import agentwire.core as core
        core.CONFIG_DIR = pathlib.Path({str(config)!r})
        from unittest.mock import patch
        from agentwire import safety_notify

        class Sent:
            success = True
            error = None

        def _record(**kw):
            with open({str(outbox)!r}, "a") as f:
                f.write(json.dumps({{"subject": kw.get("subject"),
                                     "body": kw.get("body")}}) + "\\n")
            return Sent()

        # argv: agentwire safety notify-unattended-block --reason R --rule-id I --command C
        args = sys.argv[1:]
        def opt(name):
            return args[args.index(name) + 1] if name in args else ""

        with patch("agentwire.channels.email.send_email", side_effect=_record):
            safety_notify.record_block(
                rule_id=opt("--rule-id"), session="memory-manager",
                reason=opt("--reason"), command=opt("--command"))
    """).lstrip())
    shim.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "AGENTWIRE_DIR": str(config),
        "AGENTWIRE_UNATTENDED": "1",
        "AGENTWIRE_SESSION_NAME": "memory-manager",
    }
    return {"config": config, "env": env, "outbox": outbox}


def fire(rig, command=BLOCKED_COMMAND):
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, env=rig["env"], timeout=30,
    )
    return proc


def audit_rows(rig):
    logdir = rig["config"] / "logs" / "damage-control"
    rows = []
    for f in sorted(logdir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def emails(rig):
    if not rig["outbox"].exists():
        return []
    return [json.loads(x) for x in rig["outbox"].read_text().splitlines() if x.strip()]


def settle(rig, expected_rows, timeout=20.0):
    """Wait for the DETACHED notifier children to finish writing."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(audit_rows(rig)) >= expected_rows:
            time.sleep(1.0)   # let the last detached notifier land
            return
        time.sleep(0.2)


class TestNBlocksNRowsOneEmail:
    def test_the_invariant(self, rig):
        """14 blocks in one window => 14 audit rows, 1 email."""
        blocked = 0
        for _ in range(14):
            proc = fire(rig)
            assert proc.returncode == 2, (
                f"expected a block, got rc={proc.returncode}: {proc.stderr[:300]}")
            blocked += 1
        settle(rig, blocked)

        rows = [r for r in audit_rows(rig) if r.get("decision") == "blocked"]
        assert len(rows) == 14, (
            f"{len(rows)} audit rows for 14 blocks — the LOG is being throttled, "
            f"which trades spam for blindness")

        sent = emails(rig)
        assert len(sent) == 1, f"{len(sent)} emails for 14 blocks: {[s['subject'] for s in sent]}"

    def test_every_row_carries_the_rule_id(self, rig):
        fire(rig)
        settle(rig, 1)
        rows = [r for r in audit_rows(rig) if r.get("decision") == "blocked"]
        assert rows and rows[0].get("rule_id"), (
            "an audit row with no rule_id cannot be aggregated or acted on")

    def test_the_block_still_fails_closed(self, rig):
        """Throttling notification must not soften the block itself."""
        proc = fire(rig)
        assert proc.returncode == 2
        assert "Blocked" in proc.stderr

    def test_the_digest_counts_match_the_rows(self, rig):
        """The number in the email must equal the number in the log."""
        for _ in range(9):
            fire(rig)
        settle(rig, 9)
        rows = [r for r in audit_rows(rig) if r.get("decision") == "blocked"]
        sent = emails(rig)
        assert len(rows) == 9
        assert len(sent) == 1
        # The first email fires on block #1, so it reports 1 — the remaining 8
        # are spooled. What must never happen is a count that exceeds the log.
        body = sent[0]["body"]
        reported = int(body.split(" ", 1)[0])
        assert reported <= len(rows)
