"""A dangerous payload INSIDE a quoted substitution stays refused (#925).

The arrangement that breaks, reported by the merge-queue holder:

    git commit -m "$(rm -rf /tmp/x)"

Three things have to line up, and no single change does it:

1. ``masked_subcommands`` blanks a fully-quoted whitespace-containing token, so
   the payload leaves the masked haystack. Pre-existing.
2. **#920** anchors ``core.rm-*``, so those rules stop reading the RAW haystack
   — which is currently the only thing catching it.
3. **#917** ships ``git.commit`` unscoped, so the residual ``ask`` resolves to
   **allow** with no human present.

The earlier "``$`` and ``(`` are ordinary characters to the masker, so the
token survives" reasoning holds only when the dangerous token sits OUTSIDE the
quoted span (``rm -rf "$(cat /tmp/x)"`` — verb outside, survives). The breaking
arrangement is the opposite: benign outer verb, dangerous payload INSIDE.

Two properties keep it closed here, and both are tested:

* **The body is extracted as its own haystack, before masking runs.** So the
  payload is reachable whether or not the enclosing token is masked — strictly
  more inclusive than un-masking the token, and independent of anchoring.
* **The body is judged as its own command, and its verdict outranks the outer
  one.** Folding it into the haystacks was not enough: the ladder returns the
  FIRST matching rule's id, and the unattended resolver keys the grant on that
  id — so a granted outer verb laundered an ungranted inner payload.

``TestUnderNumber920Anchoring`` removes the raw-haystack backstop on purpose, by
flipping ``anchored`` on the ``core.rm-*`` rules exactly as #920's rule files
do. Without that the whole file would pass on a coincidence.

Every row asserts the **UNATTENDED** column. The interactive ``ask`` looks
harmless and is precisely what hid this.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from agentwire.safety import _core as C  # noqa: N812

REPO = Path(__file__).resolve().parent.parent.parent
BUNDLED_RULES = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
BUNDLED_TOOLDEFS = REPO / "agentwire" / "tooldefs"

EXPECTED_PATTERNS = 265
EXPECTED_ANCHORED = 101

RM = "r" + "m"

# Payload, and the rule that must refuse it. Chosen to span rule FAMILIES, and
# to include the irreversible / outward-facing tier the unattended guard exists
# for — an unattended `uv publish` cannot be un-published.
PAYLOADS = [
    (f"{RM} -rf /tmp/x", "core.rm-with-recursive-or-force-flags"),
    (f"sudo {RM} -rf /etc", "core.rm-with-recursive-or-force-flags"),
    ("rmdir /Users/dotdev/projects/real", "core.rmdir-use-git-clean-or-manual-cleanup"),
    ("git push --force origin main", "git.git-push-force-use-force-with-lease"),
    ("git reset --hard HEAD~5", "git.git-reset-hard-use-soft-or-stash"),
    ("git clean -fdx", "git.git-clean-with-force-directory-flags"),
    ("gh pr merge 5 --admin", "tooldef.github-cli-merge-a-pull-request"),
    ("vercel deploy --prod", "deploy.vercel"),
    ("uv publish", "tooldef.uv-publish-package-to-pypi"),
    ("terraform destroy -auto-approve",
     "infrastructure.terraform-destroy-destroys-all-infrastructure"),
]


def _load():
    config = C.load_config(BUNDLED_RULES, BUNDLED_TOOLDEFS)
    pats = config.get("bashToolPatterns", [])
    assert len(pats) == EXPECTED_PATTERNS, (
        f"loaded {len(pats)} patterns — a corpus that did not load reads ALLOW "
        f"everywhere and makes this file vacuously green")
    assert sum(1 for p in pats if isinstance(p, dict) and p.get("anchored")) == \
        EXPECTED_ANCHORED
    config["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return config


@pytest.fixture
def cfg():
    return _load()


@pytest.fixture
def cfg_920():
    """The corpus with ``core.rm-*`` anchored, as #920's rule files leave it.

    Removes the raw-haystack backstop deliberately. Without this the rows below
    would pass on the very coincidence this file exists to stop relying on.
    """
    config = copy.deepcopy(_load())
    flipped = 0
    for p in config["bashToolPatterns"]:
        if not isinstance(p, dict):
            continue
        rid = str(p.get("id", ""))
        if (rid.startswith(("core.rm", "core.sudo-rm", "core.rmdir"))
                or "exec-rm" in rid) and not p.get("anchored"):
            p["anchored"] = True
            flipped += 1
    assert flipped >= 5, (
        f"only flipped {flipped} rules — the #920 simulation is not in effect "
        f"and these rows prove nothing")
    return config


def unattended(cfg, command, cwd="/work/repo"):
    """The verdict a scheduler dispatch actually gets."""
    r = C.check_command(command, cfg)
    if r["decision"] != "ask":
        return r["decision"], r.get("id")
    grants = C.resolve_unattended_grants(cfg)
    ok, _why = C.unattended_grant_allows(
        r.get("id"), command, grants, cwd, pattern=r.get("pattern"))
    return ("allow" if ok else "block"), r.get("id")


class TestQuotedPayloadUnderAGrantedOuterVerb:
    """``git commit`` carries an UNSCOPED default grant. It must not launder."""

    @pytest.mark.parametrize("payload,expected_id", PAYLOADS,
                             ids=[p[0][:24] for p in PAYLOADS])
    def test_refused_unattended(self, cfg, payload, expected_id):
        decision, rule_id = unattended(cfg, f'git commit -m "$({payload})"')
        assert decision == "block", (
            f'git commit -m "$({payload})" reached an unattended session '
            f"via {rule_id!r} — the fail-closed guarantee is gone")
        assert rule_id == expected_id, (
            f"refused via {rule_id!r} rather than the payload's own rule "
            f"{expected_id!r} — a generic catcher would keep this green while "
            f"the real coverage is missing")

    def test_the_outer_verb_really_is_granted(self, cfg):
        """The premise. Without it these rows prove nothing about laundering."""
        assert unattended(cfg, 'git commit -m "release notes"') == ("allow", "git.commit")

    def test_the_ungranted_contrast(self, cfg):
        """``echo`` has no grant — the control that shows the grant is the risk.

        Identical shape, no grant behind it. If this row and the ``git commit``
        rows ever disagree, the grant is load-bearing rather than incidental.
        """
        assert unattended(cfg, f'echo "$({RM} -rf /tmp/x)"')[0] == "block"


class TestUnderNumber920Anchoring:
    """The same corpus with the raw-haystack backstop removed."""

    @pytest.mark.parametrize("payload,expected_id", PAYLOADS,
                             ids=[p[0][:24] for p in PAYLOADS])
    def test_still_refused_when_core_rm_is_anchored(self, cfg_920, payload,
                                                    expected_id):
        decision, rule_id = unattended(cfg_920, f'git commit -m "$({payload})"')
        assert (decision, rule_id) == ("block", expected_id)

    def test_the_simulation_actually_removes_the_backstop(self, cfg_920):
        """Guard against a simulation that silently did nothing."""
        rm_rules = [p for p in cfg_920["bashToolPatterns"]
                    if isinstance(p, dict)
                    and str(p.get("id", "")).startswith("core.rm-with")]
        assert rm_rules and all(p.get("anchored") for p in rm_rules)


class TestTheMechanism:
    """Pin WHY it holds, not just that it does."""

    def test_the_body_is_extracted_before_masking(self):
        cmd = f'git commit -m "$({RM} -rf /tmp/x)"'
        assert C.split_substitutions(cmd)[1] == [f"{RM} -rf /tmp/x"]
        assert f"{RM} -rf /tmp/x" in C.masked_subcommands(cmd)

    def test_the_outer_token_is_still_masked(self):
        """The extraction does not work by weakening masking.

        Un-masking the token would also re-expose ordinary quoted argument text
        to command rules, which is #915's regression. The body rides ALONGSIDE
        the masked token instead.
        """
        masked = C.masked_subcommands(f'git commit -m "$({RM} -rf /tmp/x)"')
        assert any("\x01" in m or "\x00" in m for m in masked), masked

    def test_the_inner_verdict_names_the_inner_command(self, cfg):
        """So an operator can see WHAT was refused, not just that something was."""
        r = C.check_command(f'git commit -m "$({RM} -rf /tmp/x)"', cfg)
        assert r.get("inner_command") == f"{RM} -rf /tmp/x"

    def test_an_allowlisted_payload_is_still_permitted(self, cfg):
        """The tightening must not become a blanket refusal.

        ``rmdir /tmp/x`` is allowed standalone (``/tmp`` is allowlisted), so
        wrapping it in a substitution must not refuse it — otherwise this is
        just the blunt rule again wearing a different hat. This row was
        originally miswritten as a FAILURE case; measuring the standalone
        verdict is what corrected it.
        """
        assert unattended(cfg, "rmdir /tmp/x")[0] == "allow"
        assert unattended(cfg, 'git commit -m "$(rmdir /tmp/x)"')[0] == "allow"

    def test_a_benign_payload_does_not_become_refused(self, cfg):
        assert unattended(cfg, 'git commit -m "$(date +%F)"')[0] == "allow"

    def test_single_quotes_expand_nothing_so_nothing_is_extracted(self):
        """SINGLE quotes suppress expansion entirely — nothing runs.

        ``git commit -m '$(rm -rf /)'`` commits the literal characters
        ``$(rm -rf /)``; the shell executes no substitution.
        ``split_substitutions`` skips single-quoted spans for exactly that
        reason, so this contributes no inner command — the difference between
        guarding the OPERATION and guarding text that merely looks like one.
        """
        assert C.split_substitutions(f"git commit -m '$({RM} -rf /)'")[1] == []

    def test_the_single_quoted_form_is_still_refused_by_an_unanchored_rule(self, cfg):
        """...but on TODAY's corpus it is refused anyway, and not by this PR.

        ``core.rm-with-recursive-or-force-flags`` is unanchored on main, so it
        matches the RAW command — including a commit message that merely quotes
        the text. That is #915's false-positive shape, and #920's anchoring is
        the fix for it, not anything here.

        Recorded rather than asserted as desirable: it is what the corpus does
        today, and the row below shows it resolving once #920 lands.
        """
        assert unattended(cfg, f"git commit -m '$({RM} -rf /)'")[0] == "block"

    def test_and_under_920_anchoring_it_correctly_becomes_allow(self, cfg_920):
        """The row the merge-queue holder flagged as correctly-ALLOW.

        With ``core.rm-*`` anchored, the raw haystack is no longer read, the
        quoted token is masked, and single quotes contributed no inner command
        — so nothing dangerous is reachable, because nothing dangerous RUNS.
        Allowing it is right; refusing it would refuse every commit message and
        incident report that quotes a dangerous command.
        """
        assert unattended(cfg_920, f"git commit -m '$({RM} -rf /)'")[0] == "allow"

    def test_but_double_quotes_around_the_same_text_still_block_under_920(
            self, cfg_920):
        """The contrast that makes the row above a distinction, not a hole."""
        assert unattended(cfg_920, f'git commit -m "$({RM} -rf /)"') == (
            "block", "core.rm-with-recursive-or-force-flags")
