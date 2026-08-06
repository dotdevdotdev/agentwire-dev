"""Unattended blocks are spooled, throttled and digested, never streamed (#925).

The corpus below is not invented. Every rule id, session name and command
sample is taken verbatim from ``~/.agentwire/logs/damage-control/`` over the 14
days to 2026-08-06 — the same 111 unattended blocks that produced 111 emails
and got the guard put up for removal. That matters for the same reason
``test_auth_expired``'s fixtures are verbatim: a test written against a tidy
invented burst passes while missing the shape that actually happens, which here
is *one rule, one session, over and over*.

What the real distribution looks like, and what each property below is for:

* 53 of 111 blocks are ``core.ambiguous-command``, most of them the same
  session looping. ``test_forty_blocks_of_one_pair_send_one_email`` is that.
* The volume is accelerating (3/day at the start of the window, 39 on the last
  day), so the throttle has to bound the RATE, not just deduplicate — hence the
  window assertions rather than only pair-count assertions.
* 15 distinct rule ids appear, so the digest has to group and stay readable
  rather than concatenate.

The one thing this module must never do is make a block go unrecorded. The
audit log is written by the hook on a separate path *before* the notifier is
invoked; ``TestTheAuditLogIsNotThrottled`` pins that ordering, because trading
email spam for a missing record would be a strictly worse bug than the one
being fixed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from agentwire import safety_notify

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control"

# --------------------------------------------------------------------------
# Verbatim from the audit log, 2026-07-24 .. 2026-08-06
# --------------------------------------------------------------------------

# The 53-block plurality: a loop over the per-project memory stores. Benign,
# blocked purely for containing `$(...)`.
AMBIGUOUS_CMD = (
    'for s in -Users-dotdev-projects-agentwire-dev '
    '-Users-dotdev-projects-documentscribe; do d="$HOME/.claude/projects/$s/memory"; '
    'printf "%-45s AUDIT.md:%s\\n" "$s" "$([ -f "$d/AUDIT.md" ] && echo yes)"; done'
)
AMBIGUOUS_REASON = "Unverifiable command (command substitution) — confirm before running"
AMBIGUOUS_RULE = "core.ambiguous-command"

# The 18-block runner-up, from the artifactsmmo scheduled task.
UV_RUN_CMD = "uv run amo status 2>&1"
UV_RUN_REASON = "uv run: a script in project environment"
UV_RUN_RULE = "tooldef.uv-run-a-script-in-project-environment"

# The sessions that produced them.
MEMORY_MANAGER = "memory-manager"
ARTIFACTSMMO = "artifactsmmo-auto"


class Sent:
    """A stand-in for ``EmailResult`` that records what was sent."""

    def __init__(self, success: bool = True):
        self.success = success
        self.error = None if success else "no API key"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate CONFIG_DIR into tmp_path.

    Patched on the MODULE (``agentwire.core.CONFIG_DIR``), which is the whole
    reason ``safety_notify._config_dir`` is a function — an import-time
    ``from .core import CONFIG_DIR`` would ignore this and scribble a real
    spool onto the operator's machine (#902).
    """
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    return tmp_path


@pytest.fixture
def mail():
    """Patch the shared Resend wiring and hand back the mock."""
    with patch("agentwire.channels.email.send_email", return_value=Sent()) as m:
        yield m


def block(rule=AMBIGUOUS_RULE, session=MEMORY_MANAGER, reason=AMBIGUOUS_REASON,
          command=AMBIGUOUS_CMD, at=None):
    return safety_notify.record_block(
        rule_id=rule, session=session, reason=reason, command=command, now=at
    )


def bodies(mail):
    return [c.kwargs.get("body", "") for c in mail.call_args_list]


def subjects(mail):
    return [c.kwargs.get("subject", "") for c in mail.call_args_list]


# --------------------------------------------------------------------------
# The bug: one email per block
# --------------------------------------------------------------------------


class TestOneEmailNotForty:
    def test_forty_blocks_of_one_pair_send_one_email(self, env, mail):
        """The load-bearing assertion. This is the 53-block shape, verbatim.

        A scheduled task looping on one blocked command is ONE fact. Before
        #925 it was forty emails; the throttle alone would still allow one per
        hour forever, so the ``(rule_id, session)`` dedup is what makes the
        count stop at one.
        """
        now = safety_notify._now()
        for i in range(40):
            block(at=now + timedelta(minutes=i * 3))  # 2 hours of looping

        assert mail.call_count == 1, (
            f"expected one digest for 40 repeats of one pair, got "
            f"{mail.call_count}: {subjects(mail)}"
        )

    def test_the_one_email_names_the_rule_and_session(self, env, mail):
        block()
        assert AMBIGUOUS_RULE in subjects(mail)[0]
        assert MEMORY_MANAGER in subjects(mail)[0]
        body = bodies(mail)[0]
        assert AMBIGUOUS_RULE in body
        assert MEMORY_MANAGER in body
        assert AMBIGUOUS_REASON in body

    def test_the_first_block_is_not_delayed(self, env, mail):
        """An isolated block still surfaces immediately.

        Batching everything on a timer would be a different bug: the point is
        to stop the flood, not to stop the signal. First sighting emails at
        once, exactly as ``auth_expired._escalate`` does.
        """
        assert block()["emailed"] is True
        assert mail.call_count == 1


# --------------------------------------------------------------------------
# The digest
# --------------------------------------------------------------------------


class TestDigest:
    def test_a_burst_is_one_email_with_per_pair_counts(self, env, mail):
        """The shape the owner asked for: '9 × core.ambiguous-command in …'."""
        now = safety_notify._now()
        block(at=now)  # first sighting → emails immediately, 1 block
        mail.reset_mock()

        for i in range(8):
            block(at=now + timedelta(minutes=i + 1))
        for i in range(5):
            block(rule=UV_RUN_RULE, session=ARTIFACTSMMO, reason=UV_RUN_REASON,
                  command=UV_RUN_CMD, at=now + timedelta(minutes=i + 10))
        assert mail.call_count == 0, "throttle window not respected"

        # An hour later the next block releases the digest.
        block(rule=UV_RUN_RULE, session=ARTIFACTSMMO, reason=UV_RUN_REASON,
              command=UV_RUN_CMD, at=now + timedelta(hours=1, minutes=1))

        assert mail.call_count == 1
        body = bodies(mail)[0]
        assert "**8 ×**" in body, body
        assert "**6 ×**" in body, body          # 5 + the one that released it
        assert MEMORY_MANAGER in body and ARTIFACTSMMO in body
        assert "14 unattended" in body           # 8 + 6, the total, in the lede

    def test_the_digest_carries_a_sample_command(self, env, mail):
        """A rule id alone doesn't tell the owner whether the block was right."""
        block()
        assert AMBIGUOUS_CMD in bodies(mail)[0]

    def test_the_digest_points_at_the_unthrottled_record(self, env, mail):
        block()
        assert "agentwire safety logs" in bodies(mail)[0]

    def test_the_digest_names_how_to_permit_the_rule(self, env, mail):
        block()
        body = bodies(mail)[0]
        assert "unattended_allow" in body
        assert f"`{AMBIGUOUS_RULE}`" in body

    def test_many_pairs_are_summarised_not_dumped(self, env, mail):
        """15 distinct rule ids appear in the real window; a wall of text is unread."""
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        for i in range(30):
            block(rule=f"tooldef.rule-{i}", session=f"session-{i}",
                  at=now + timedelta(minutes=1))
        block(at=now + timedelta(hours=1, minutes=1))

        body = bodies(mail)[0]
        rendered = body.count("**1 ×**") + body.count("**2 ×**")
        assert rendered <= safety_notify.DIGEST_PAIR_CAP + 1
        assert "further pairs" in body


# --------------------------------------------------------------------------
# Throttle and dedup, separately
# --------------------------------------------------------------------------


class TestThrottle:
    def test_window_is_at_least_an_hour(self):
        assert safety_notify.THROTTLE >= timedelta(hours=1)

    def test_a_brand_new_pair_still_waits_out_the_window(self, env, mail):
        """Novelty does not bypass the rate gate.

        Tempting to let a never-seen pair through immediately — but the real
        window has 15 distinct ids, so 'new pairs are urgent' is just the old
        behaviour with extra steps.
        """
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        block(rule=UV_RUN_RULE, session=ARTIFACTSMMO, at=now + timedelta(minutes=5))
        assert mail.call_count == 0

    def test_the_window_opens_again_after_an_hour(self, env, mail):
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        block(rule=UV_RUN_RULE, session=ARTIFACTSMMO,
              at=now + safety_notify.THROTTLE + timedelta(minutes=1))
        assert mail.call_count == 1


class TestDedup:
    def test_repeats_alone_do_not_re_notify_hourly_forever(self, env, mail):
        """A loop nobody fixed must not become one email an hour, all identical.

        This is the property the throttle alone cannot give: 24 hours of the
        same pair blocking every minute is one email, not 24.
        """
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        for minute in range(1, 12 * 60, 7):
            block(at=now + timedelta(minutes=minute))
        assert mail.call_count == 0, (
            f"a repeating pair re-notified {mail.call_count} times in 12h")

    def test_dedup_expires_within_a_day(self):
        """Pinned as an absolute bound, deliberately.

        The obvious test — advance the clock by ``DEDUP_TTL + 1min`` and assert
        an email — moves with the constant it is meant to constrain: widen
        ``DEDUP_TTL`` to ten years and it still passes, having silently
        asserted nothing. Mutation testing found exactly that; this is the fix.
        """
        assert safety_notify.DEDUP_TTL <= timedelta(hours=24)

    def test_but_it_re_reports_once_the_dedup_ttl_lapses(self, env, mail):
        """Silence forever would be the other failure. It re-surfaces daily."""
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        block(at=now + timedelta(hours=25))
        assert mail.call_count == 1

    def test_suppressed_repeats_are_counted_not_discarded(self, env, mail):
        """The count keeps accruing while suppressed, and rides the next digest.

        Reporting '1 × ambiguous-command' after 300 blocks would understate the
        problem to exactly the person deciding whether to change the rule.
        """
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        for i in range(300):
            block(at=now + timedelta(minutes=i + 1))
        block(rule=UV_RUN_RULE, session=ARTIFACTSMMO,
              at=now + timedelta(hours=6))          # a NEW pair releases it
        assert mail.call_count == 1
        assert "**300 ×**" in bodies(mail)[0]

    def test_same_rule_in_a_different_session_is_a_different_pair(self, env, mail):
        """Dedup is (rule_id, session) — one task looping is not every task."""
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        block(session="ai-morning-briefing", at=now + timedelta(hours=1, minutes=1))
        assert mail.call_count == 1
        assert "ai-morning-briefing" in bodies(mail)[0]


# --------------------------------------------------------------------------
# The watchdog flush
# --------------------------------------------------------------------------


class TestTick:
    def test_a_tail_is_flushed_without_a_further_block(self, env, mail):
        """Ten blocks then silence must not report one and sit on nine.

        Without the watchdog stage the spool only drains when the NEXT block
        arrives, so a task that gives up takes its own report with it.
        """
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        for i in range(9):
            block(rule=UV_RUN_RULE, session=ARTIFACTSMMO, at=now + timedelta(minutes=i))
        assert mail.call_count == 0

        assert safety_notify.tick(now=now + timedelta(hours=1, minutes=1))["emailed"]
        assert "**9 ×**" in bodies(mail)[0]

    def test_tick_on_an_empty_spool_is_silent(self, env, mail):
        assert safety_notify.tick()["emailed"] is False
        assert mail.call_count == 0

    def test_tick_never_spools_anything(self, env, mail):
        """It releases; it does not record. A tick must not invent a block."""
        safety_notify.tick()
        safety_notify.tick()
        assert safety_notify.read_state().get("pending") in (None, {})


# --------------------------------------------------------------------------
# Best-effort: nothing here may break the caller
# --------------------------------------------------------------------------


class TestNeverCrashes:
    def test_a_send_failure_does_not_raise(self, env):
        with patch("agentwire.channels.email.send_email", return_value=Sent(False)):
            assert block()["spooled"] is True

    def test_an_exploding_sender_does_not_raise(self, env):
        with patch("agentwire.channels.email.send_email",
                   side_effect=RuntimeError("resend down")):
            assert block()["spooled"] is True

    def test_a_failed_send_keeps_the_spool_for_the_next_attempt(self, env):
        """Only a successful send clears. Following ``auth_expired._escalate``.

        A failed send that counted as delivered would lose the report; the
        trade is a retry on the next block, which stops the moment one lands.
        """
        with patch("agentwire.channels.email.send_email", return_value=Sent(False)):
            block()
        assert safety_notify.read_state()["pending"], "spool cleared on a failed send"

        with patch("agentwire.channels.email.send_email", return_value=Sent()) as m:
            block()
        assert m.call_count == 1
        assert "**2 ×**" in m.call_args.kwargs["body"]

    def test_an_unwritable_spool_does_not_raise(self, env, monkeypatch):
        monkeypatch.setattr(safety_notify, "state_path",
                            lambda: Path("/proc/nonexistent/spool.json"))
        assert block() == {"spooled": False, "emailed": False}

    def test_a_corrupt_spool_heals_rather_than_wedging(self, env, mail):
        safety_notify.state_path().parent.mkdir(parents=True, exist_ok=True)
        safety_notify.state_path().write_text("{ not json")
        assert block()["spooled"] is True
        assert mail.call_count == 1

    def test_the_state_file_stays_valid_json(self, env, mail):
        for i in range(5):
            block(rule=f"r{i}")
        json.loads(safety_notify.state_path().read_text())


class TestBounded:
    def test_the_spool_cannot_grow_without_bound(self, env, mail):
        now = safety_notify._now()
        block(at=now)
        for i in range(safety_notify.PAIR_CAP + 50):
            block(rule=f"tooldef.r{i}", session=f"s{i}", at=now + timedelta(minutes=1))
        pending = safety_notify.read_state()["pending"]
        assert len(pending) <= safety_notify.PAIR_CAP

    def test_blocks_past_the_cap_are_counted_not_silently_dropped(self, env, mail):
        """A digest whose total is short of reality is a digest that lies."""
        now = safety_notify._now()
        block(at=now)
        mail.reset_mock()
        for i in range(safety_notify.PAIR_CAP + 20):
            block(rule=f"tooldef.r{i}", session=f"s{i}", at=now + timedelta(minutes=1))
        assert safety_notify.read_state()["overflow"] > 0

        block(at=now + timedelta(hours=1, minutes=1))
        assert "spool cap" in bodies(mail)[0]


# --------------------------------------------------------------------------
# The throttle must survive process death
# --------------------------------------------------------------------------


# Each invocation is a real, separate interpreter. The hook spawns the notifier
# via ``Popen(..., start_new_session=True)``, so in production NOTHING is shared
# between two blocks except the filesystem. An in-process counter would be a
# no-op there — and would still pass every test above, all of which call
# ``record_block`` twice in one interpreter. That is the fixture-shaped blind
# spot aimed straight at this feature, so it gets a test that cannot have it.
_CHILD = textwrap.dedent("""
    import json, sys
    from unittest.mock import patch
    import agentwire.core as core
    core.CONFIG_DIR = __import__("pathlib").Path(sys.argv[1])
    from agentwire import safety_notify

    class Sent:
        success = True
        error = None

    sent = []
    with patch("agentwire.channels.email.send_email",
               side_effect=lambda **kw: (sent.append(kw), Sent())[1]):
        safety_notify.record_block(
            rule_id=sys.argv[2], session=sys.argv[3],
            reason="Unverifiable command (command substitution)",
            command="for p in a b; do echo x; done",
        )
    print(json.dumps({"emails": len(sent), "bodies": [s["body"] for s in sent]}))
""")


class TestThrottleSurvivesProcessDeath:
    @staticmethod
    def _invoke(config_dir: Path, rule="core.ambiguous-command", session="memory-manager"):
        env = {**os.environ, "PYTHONPATH": str(
            Path(__file__).resolve().parent.parent.parent)}
        p = subprocess.run(
            [sys.executable, "-c", _CHILD, str(config_dir), rule, session],
            capture_output=True, text=True, env=env)
        assert p.returncode == 0, f"child failed:\n{p.stderr}"
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_a_second_process_does_not_send(self, tmp_path):
        """The blocking test: two SEPARATE interpreters, one email."""
        config = tmp_path / "agentwire"
        first = self._invoke(config)
        second = self._invoke(config)
        assert first["emails"] == 1, "first block should surface immediately"
        assert second["emails"] == 0, (
            "a second process sent its own email — the throttle is in-process, "
            "which is a no-op in production where every block is a fresh "
            "Popen(start_new_session=True)")

    def test_ten_processes_send_one_email(self, tmp_path):
        config = tmp_path / "agentwire"
        total = sum(self._invoke(config)["emails"] for _ in range(10))
        assert total == 1, f"{total} emails from 10 separate processes"

    def test_the_state_is_on_disk_and_readable_by_the_next_process(self, tmp_path):
        config = tmp_path / "agentwire"
        self._invoke(config)
        spool = config / "safety" / "unattended-blocks.json"
        assert spool.exists(), "no on-disk state — nothing survives the process"
        assert json.loads(spool.read_text())["last_email_at"]

    def test_distinct_pairs_across_processes_still_coalesce(self, tmp_path):
        config = tmp_path / "agentwire"
        self._invoke(config, rule="core.ambiguous-command", session="memory-manager")
        second = self._invoke(config, rule="tooldef.uv-run-a-script-in-project-environment",
                              session="artifactsmmo-auto")
        assert second["emails"] == 0


# --------------------------------------------------------------------------
# The property that must survive all of the above
# --------------------------------------------------------------------------


class TestTheAuditLogIsNotThrottled:
    """Throttling the EMAIL must not throttle the LOG, or spam becomes blindness.

    The guarantee is structural, not incidental: every hook writes the audit
    line via ``log_blocked`` and only THEN invokes the notifier, on a code path
    the notifier cannot reach. These pin that ordering so a future refactor
    that folds logging into the notifier fails here instead of in production.
    """

    @pytest.mark.parametrize("hook", [
        "bash-tool-damage-control.py",
        "mcp-tool-damage-control.py",
    ])
    def test_the_hook_logs_before_it_notifies(self, hook):
        """Anchored on the CALL, not on its argument list.

        The first version of this matched the literal
        ``_notify_unattended_block(command, reason, rule_id)`` and broke when
        #917 started passing ``f"{reason} — {why}"`` — a pure rewording that
        changed nothing about the ordering this test exists to protect. Guard
        the operation, not the phrasing.
        """
        src = (HOOKS_DIR / hook).read_text()
        calls = [i for i in range(len(src))
                 if src.startswith("_notify_unattended_block(", i)]
        assert calls, f"{hook}: no call to _notify_unattended_block"
        notify = calls[-1]
        # The unattended branch's own log_blocked call, immediately above it.
        log = src.rindex("log_blocked", 0, notify)
        between = src[log:notify]
        assert "unattended" in between, (
            f"{hook}: the log_blocked above _notify_unattended_block is not the "
            f"unattended one — the ordering guarantee has moved")

    def test_the_notifier_does_not_log(self):
        """It has no audit-log surface at all, so it cannot suppress one.

        Checked on the parsed AST rather than the source text — the module's
        own docstring *describes* the ordering, and a substring check would
        pass or fail on the prose instead of on the code.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(safety_notify))
        called = {
            node.func.id if isinstance(node.func, ast.Name) else
            getattr(node.func, "attr", "")
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        assert not called & {"log_blocked", "log_allowed", "log_asked"}
        imported = {
            alias.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        } | {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any("audit" in name for name in imported)
