"""#917 (path-scoped grants) × #925 (concealment narrowing), composed.

Neither change's own suite can see this. #925 decides WHICH COMMANDS REACH grant
evaluation; #917 decides what happens when they land. They met for the first
time when this branch rebased onto ``1736abd``, and the composition had never
been measured because #917 was forty minutes old.

**The sets do intersect**, and stating that plainly was the ask:

    git commit -m "$(cat msg.txt)"
      before #925:  core.ambiguous-command   — never reached grant evaluation
      after  #925:  git.commit               — DOES reach grant evaluation

So the narrowing genuinely routes commands into #917's scoped-grant path that
never arrived there before. What happens when they land is pinned below, and it
splits cleanly on one question — *can the substitution decide WHERE the command
acts?*

* It supplies the commit MESSAGE → the ``git.commit`` grant applies, and an
  unscoped grant permits it. That is the owner's stated policy taking effect,
  not a hole: it is what "route on the rule, not on the presence of ``$(``"
  means. **Disclosed in the PR body.**
* It supplies the ``-C`` DIRECTORY → scope evaluation refuses, because a
  substitution can decide the directory and #914's posture is to refuse rather
  than guess.

A refused → permitted transition is a hole only when nothing affirmatively
granted it. Every composed permit here is backed by an explicit grant, and
``test_no_composed_permit_is_ungranted`` is what keeps that true.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentwire.safety import _core as C  # noqa: N812

REPO = Path(__file__).resolve().parent.parent.parent
BUNDLED_RULES = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
BUNDLED_TOOLDEFS = REPO / "agentwire" / "tooldefs"

EXPECTED_PATTERNS = 265
EXPECTED_ANCHORED = 101

STORE = "/Users/dotdev/.claude/projects/-x/memory"


@pytest.fixture
def cfg():
    config = C.load_config(BUNDLED_RULES, BUNDLED_TOOLDEFS)
    pats = config.get("bashToolPatterns", [])
    assert len(pats) == EXPECTED_PATTERNS, (
        f"loaded {len(pats)} patterns — a corpus that did not load reads ALLOW "
        f"everywhere and makes this file vacuously green")
    assert sum(1 for p in pats if isinstance(p, dict) and p.get("anchored")) == \
        EXPECTED_ANCHORED
    config["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return config


def scoped(cfg, paths):
    cfg["safety"]["unattended_allow"] = [{"id": "git.commit", "paths": paths}]
    return cfg


def verdict(cfg, command, cwd):
    """Decision, rule id and the grant's own reason — the full composed answer."""
    r = C.check_command(command, cfg)
    if r["decision"] != "ask":
        return r["decision"], r.get("id"), ""
    grants = C.resolve_unattended_grants(cfg)
    ok, why = C.unattended_grant_allows(
        r.get("id"), command, grants, cwd, pattern=r.get("pattern"))
    return ("allow" if ok else "block"), r.get("id"), why


class TestTheRoutingReallyChanged:
    """The premise. Without this the rest could be measuring nothing."""

    def test_a_substituted_operand_now_reaches_its_real_rule(self, cfg):
        _, rule_id, _ = verdict(cfg, 'git commit -m "$(cat msg.txt)"', STORE)
        assert rule_id == "git.commit", (
            "the narrowing is not routing this into grant evaluation, so the "
            "composition below is not being exercised")


class TestSubstitutionThatCannotDecideWhere:
    """A substituted commit MESSAGE. The grant applies normally."""

    def test_an_unscoped_grant_permits_it(self, cfg):
        decision, rule_id, why = verdict(cfg, 'git commit -m "$(cat msg.txt)"', STORE)
        assert (decision, rule_id) == ("allow", "git.commit")
        assert "granted unattended" in why, (
            "permitted with no affirmative grant behind it — that would be a "
            "hole rather than the policy")

    def test_a_scoped_grant_refuses_it_because_scope_is_unknowable(self, cfg):
        """Scope asks a different question and answers it independently.

        ``detect_obfuscation`` returns None here — the verb is literally
        ``git commit``. Scope must NOT inherit that answer: a substitution can
        still decide the directory, so scope keeps its own check.
        """
        decision, rule_id, why = verdict(
            scoped(cfg, [STORE + "/"]), 'git commit -m "$(cat msg.txt)"', STORE)
        assert (decision, rule_id) == ("block", "git.commit")
        assert "command substitution" in why


class TestSubstitutionThatCanDecideWhere:
    """A substituted ``-C`` DIRECTORY. Must refuse under any grant."""

    @pytest.mark.parametrize("command", [
        "git -C $(cat /tmp/dir) commit -m x",
        "git --git-dir=$(cat /tmp/dir)/.git commit -m x",
        "cd $(cat /tmp/dir) && git commit -m x",
    ])
    def test_scoped_grant_refuses(self, cfg, command):
        decision, _, why = verdict(scoped(cfg, [STORE + "/"]), command, STORE)
        assert decision == "block", f"{command!r} was permitted: {why}"


class TestNoComposedPermitIsUngranted:
    """The invariant that makes the whole composition safe to ship."""

    CASES = [
        ('git commit -m "$(cat msg.txt)"', STORE, None),
        ('git commit -m "$(cat msg.txt)"', "/work/other", [STORE + "/"]),
        ("git -C $(cat /tmp/dir) commit -m x", STORE, [STORE + "/"]),
        ("uv run pytest $(cat files.txt)", "/work/repo", None),
        ('git commit -m "memory: rewrite"', STORE, [STORE + "/"]),
    ]

    @pytest.mark.parametrize("command,cwd,paths", CASES)
    def test_every_permit_names_its_grant(self, cfg, command, cwd, paths):
        if paths:
            scoped(cfg, paths)
        decision, rule_id, why = verdict(cfg, command, cwd)
        if decision == "allow":
            assert "granted unattended" in why, (
                f"{command!r} was permitted with no grant behind it "
                f"(rule={rule_id!r}, why={why!r})")

    def test_scope_owns_its_substitution_check(self):
        """Regression guard for the coupling that caused this.

        ``command_scope_dirs`` used to reach the substitution case only via
        ``detect_obfuscation``. Narrowing that predicate silently changed what
        scope refused — #917's own test caught it. Scope now checks
        independently, so the two can be tuned without moving each other.
        """
        dirs, err = C.command_scope_dirs(
            'git commit -m $(cat /tmp/x)', "/work/repo", r"\bgit\s+commit\b")
        assert err is not None and "command substitution" in err
        assert C.detect_obfuscation('git commit -m $(cat /tmp/x)') is None, (
            "concealment and scope must disagree here — that is the point")
