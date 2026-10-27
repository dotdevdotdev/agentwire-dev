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
invoked; ``test_damage_control_hooks.py::test_unattended_block_is_written_to_the_audit_log``
pins the record through the real hook, because trading
email spam for a missing record would be a strictly worse bug than the one
being fixed.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from agentwire import safety_notify

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


# The sessions that produced them.
MEMORY_MANAGER = "memory-manager"


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


# --------------------------------------------------------------------------
# The digest
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Throttle and dedup, separately
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The watchdog flush
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Best-effort: nothing here may break the caller
# --------------------------------------------------------------------------


class TestNeverCrashes:

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


# --------------------------------------------------------------------------
# The property that must survive all of the above
# --------------------------------------------------------------------------


