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

# A REAL directory, created per-test. The first version hardcoded an absolute
# path under the dev machine's home, which does not exist on a CI runner — so
# scope resolution (which realpaths both sides) matched locally and not in CI,
# and the permitted branch was only ever evaluated on one machine. That is how
# a wrong assertion in this file survived a green local run of 4547 tests.
@pytest.fixture
def store(tmp_path):
    d = tmp_path / "projects" / "-x" / "memory"
    d.mkdir(parents=True)
    return str(d)


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


def has_grant(cfg, rule_id):
    """Is this rule id granted at all — structurally, not by reason text?

    The first version of ``test_every_permit_names_its_grant`` asserted
    ``"granted unattended" in why``. That is the UNSCOPED grant's wording; a
    SCOPED grant that legitimately permits says "granted under <path>; this
    command targets <path>" instead, so a correct permit failed the assertion.

    It passed locally and failed in CI, which is the tell: the scope paths here
    are absolute paths under a home directory that exists on the dev machine
    and not on the runner, so locally the scope never matched, the permit never
    happened, and the assertion was never reached. A test that only evaluates
    its interesting branch on one machine is not testing that branch.

    Guard the operation — "is there a grant behind this permit?" — not the
    sentence the resolver happens to print.
    """
    return rule_id in C.resolve_unattended_grants(cfg)


class TestTheRoutingReallyChanged:
    """The premise. Without this the rest could be measuring nothing."""

    def test_a_substituted_operand_now_reaches_its_real_rule(self, cfg, store):
        _, rule_id, _ = verdict(cfg, 'git commit -m "$(cat msg.txt)"', store)
        assert rule_id == "git.commit", (
            "the narrowing is not routing this into grant evaluation, so the "
            "composition below is not being exercised")


class TestSubstitutionThatCannotDecideWhere:
    """A substituted commit MESSAGE. The grant applies normally."""

    def test_an_unscoped_grant_permits_it(self, cfg, store):
        decision, rule_id, why = verdict(cfg, 'git commit -m "$(cat msg.txt)"', store)
        assert (decision, rule_id) == ("allow", "git.commit")
        assert "granted unattended" in why, (
            "permitted with no affirmative grant behind it — that would be a "
            "hole rather than the policy")

    def test_a_scoped_grant_refuses_it_because_scope_is_unknowable(self, cfg, store):
        """Scope asks a different question and answers it independently.

        ``detect_obfuscation`` returns None here — the verb is literally
        ``git commit``. Scope must NOT inherit that answer: a substitution can
        still decide the directory, so scope keeps its own check.
        """
        decision, rule_id, why = verdict(
            scoped(cfg, [store + "/"]), 'git commit -m "$(cat msg.txt)"', store)
        assert (decision, rule_id) == ("block", "git.commit")
        assert "command substitution" in why


class TestSubstitutionThatCanDecideWhere:
    """A substituted ``-C`` DIRECTORY. Must refuse under any grant."""

    @pytest.mark.parametrize("command", [
        "git -C $(cat /tmp/dir) commit -m x",
        "git --git-dir=$(cat /tmp/dir)/.git commit -m x",
        "cd $(cat /tmp/dir) && git commit -m x",
    ])
    def test_scoped_grant_refuses(self, cfg, store, command):
        decision, _, why = verdict(scoped(cfg, [store + "/"]), command, store)
        assert decision == "block", f"{command!r} was permitted: {why}"


class TestNoComposedPermitIsUngranted:
    """The invariant that makes the whole composition safe to ship."""

    # (command, cwd_is_store, scope_the_grant) — resolved against the `store`
    # fixture inside the test, since a class-level list cannot use a fixture and
    # a hardcoded absolute path is what made this file machine-dependent.
    CASES = [
        ('git commit -m "$(cat msg.txt)"', True, False),
        ('git commit -m "$(cat msg.txt)"', False, True),
        ("git -C $(cat /tmp/dir) commit -m x", True, True),
        ("uv run pytest $(cat files.txt)", False, False),
        ('git commit -m "memory: rewrite"', True, True),
        ('git commit -m "memory: rewrite"', False, True),
    ]

    @pytest.mark.parametrize("command,in_store,scope_it", CASES)
    def test_every_permit_names_its_grant(self, cfg, store, command, in_store,
                                          scope_it):
        if scope_it:
            scoped(cfg, [store + "/"])
        cwd = store if in_store else "/work/other"
        decision, rule_id, why = verdict(cfg, command, cwd)
        if decision == "allow":
            assert has_grant(cfg, rule_id), (
                f"{command!r} was permitted with no grant behind it "
                f"(rule={rule_id!r}, why={why!r})")
            assert why, "a permit with no stated reason cannot be audited"

    def test_a_scoped_grant_does_permit_in_scope(self, cfg, store):
        """The branch CI caught and the dev machine never reached.

        With a real, existing store directory the scope matches and the permit
        actually happens — which is the only way the assertion above is
        exercised at all. Asserted explicitly so it can never silently stop
        happening again.
        """
        scoped(cfg, [store + "/"])
        decision, rule_id, why = verdict(cfg, 'git commit -m "memory: rewrite"',
                                         store)
        assert (decision, rule_id) == ("allow", "git.commit"), why
        assert has_grant(cfg, rule_id)

    def test_and_refuses_out_of_scope(self, cfg, store):
        scoped(cfg, [store + "/"])
        decision, _, _ = verdict(cfg, 'git commit -m "memory: rewrite"',
                                 "/work/other")
        assert decision == "block"

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
