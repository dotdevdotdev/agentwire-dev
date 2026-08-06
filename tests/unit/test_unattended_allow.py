"""The unattended allowlist grants an OPERATION and shadows no hard block (#925).

Two rules of engagement govern every assertion here.

**Name the rule set you measured.** ``_load`` builds from BUNDLED rules +
BUNDLED tooldefs explicitly and asserts the pattern and anchored counts at load
time. A bare interpreter without pyyaml makes ``load_config`` return empty, at
which point every command reads ALLOW and a green run proves nothing — the
count assertion turns that into a crash instead of a nicer-looking number. The
live copies under ``~/.agentwire/`` are neither loaded nor consulted; they drift
(measured 2026-08-06: bundled 265/101, live 225/87) and tuning against
semantics that do not ship is how a fix lands backwards.

**Assert the rule ID, not the verdict.** ``uv run git push --force`` reads
BLOCK both before and after this change, so a verdict-only test passes either
way. What actually decides whether the allowlist opens a hole is *which id came
back*: rules are evaluated in order and the first match returns, so an earlier
``ask`` rule can hide a later ``block``, and the unattended resolver then turns
that hidden block into a permit. Every case below pins the id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentwire.safety import _core as C

REPO = Path(__file__).resolve().parent.parent.parent
BUNDLED_RULES = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
BUNDLED_TOOLDEFS = REPO / "agentwire" / "tooldefs"

# Pinned at load. Dropping ``tooldefs_dir`` silently removes the anchored
# patterns; a missing pyyaml removes all of them. Both would otherwise present
# as a smaller, greener run.
EXPECTED_PATTERNS = 265
EXPECTED_ANCHORED = 101

UV_IDS = {
    "tooldef.uv-run-a-script-in-project-environment",
    "tooldef.uv-run-a-command-in-project-environment",
}

# Keep the literal out of this file's own text where it would be scanned as a
# command by the very hooks under test (#915).
RM = "r" + "m"


@pytest.fixture(scope="module")
def cfg():
    config = C.load_config(BUNDLED_RULES, BUNDLED_TOOLDEFS)
    pats = config.get("bashToolPatterns", [])
    assert len(pats) == EXPECTED_PATTERNS, (
        f"loaded {len(pats)} patterns, expected {EXPECTED_PATTERNS}. Either the "
        f"corpus changed (update the constant deliberately) or the rules did "
        f"not load at all — in which case every verdict below reads ALLOW and "
        f"means nothing.")
    anchored = sum(1 for p in pats if isinstance(p, dict) and p.get("anchored"))
    assert anchored == EXPECTED_ANCHORED, (
        f"loaded {anchored} anchored patterns, expected {EXPECTED_ANCHORED} — "
        f"tooldefs_dir probably did not resolve")
    config["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return config


def decide(cfg, command):
    r = C.check_command(command, cfg)
    return r["decision"], r.get("id")


def unattended_verdict(cfg, command, allow=None):
    """What an unattended session actually gets: the hook's resolution, in full.

    Mirrors the branch in every ``*-damage-control.py`` ``main()`` — a hard
    block stays blocked; an ``ask`` becomes ``allow`` only if the MATCHED id is
    on the allowlist, and ``block`` otherwise.
    """
    decision, rule_id = decide(cfg, command)
    if decision == "block":
        return "block", rule_id
    if decision == "ask":
        allowed = C.DEFAULT_UNATTENDED_ALLOW if allow is None else allow
        return ("allow" if rule_id in allowed else "block"), rule_id
    return decision, rule_id


# ---------------------------------------------------------------------------
# Part 2 — the grant
# ---------------------------------------------------------------------------


class TestUvRunIsPermittedUnattended:
    """A scheduled task that cannot run its own tooling cannot verify its work."""

    @pytest.mark.parametrize("command", [
        "uv run amo status 2>&1",                    # verbatim, 4 of the 18 blocks
        "uv run pytest -q",
        "uv run --extra dev pytest tests/unit -q",
        "uv run python -m mypackage.check",
        "uv run ruff check .",
        "uv run mypy agentwire",
    ])
    def test_allowed(self, cfg, command):
        decision, rule_id = unattended_verdict(cfg, command)
        assert rule_id in UV_IDS, (
            f"{command!r} resolved to {rule_id!r}, not a uv-run id — the "
            f"allowlist entry is not what is being exercised here")
        assert decision == "allow"

    def test_both_ids_are_on_the_list(self):
        """Guard the operation, not the yaml line order.

        The two tooldef lines compile to the identical pattern, so which id a
        command returns is decided by which is listed first. Listing only one
        makes the permission depend on that ordering.
        """
        assert UV_IDS <= C.DEFAULT_UNATTENDED_ALLOW

    def test_the_two_ids_really_are_the_same_operation(self, cfg):
        """The premise of the test above, measured rather than asserted."""
        pats = [p for p in cfg["bashToolPatterns"]
                if isinstance(p, dict) and p.get("id") in UV_IDS]
        assert len(pats) == 2
        assert pats[0]["pattern"] == pats[1]["pattern"] == r"\buv\s+run\b"

    def test_every_default_id_resolves_to_a_real_rule(self, cfg):
        """An allowlisted id matching nothing is a permission that does nothing.

        Five of six ids were in exactly that state on the owner's machine on
        2026-08-06 (live tooldefs lacked the stable ``id:`` lines), so this is
        a measured failure mode, not a hypothetical one.
        """
        ids = {p.get("id") for p in cfg["bashToolPatterns"]
               if isinstance(p, dict) and p.get("id")}
        missing = sorted(C.DEFAULT_UNATTENDED_ALLOW - ids)
        assert not missing, f"allowlisted ids match no rule in the corpus: {missing}"


# ---------------------------------------------------------------------------
# Rules of engagement: the hard blocks named as non-negotiable
# ---------------------------------------------------------------------------


HARD_BLOCKS = [
    ("force-push",  "git push --force origin main", "git.git-push-force-use-force-with-lease"),
    ("force-push-f", "git push -f origin main",     "git.git-push-f-use-force-with-lease"),
    ("hard-reset",  "git reset --hard HEAD~5",      "git.git-reset-hard-use-soft-or-stash"),
    ("clean-fdx",   "git clean -fdx",               "git.git-clean-with-force-directory-flags"),
    ("rm-rf",       f"{RM} -rf /tmp/x",             "core.rm-with-recursive-or-force-flags"),
    ("rm-rf-root",  f"{RM} -rf /",                  "core.rm-with-recursive-or-force-flags"),
]

# Launcher prefixes. Each moves the shell off word 0, which is the whole point:
# a guard keyed on "bash is the first word" guards the phrasing.
PREFIXES = ["", "uv run ", "env ", "nice ", "time ", "nohup ", "stdbuf -o0 ",
            "timeout 5 ", "command ", "xargs -I{} ", "uvx --from x ",
            "poetry run ", "npx -y "]


class TestHardBlocksSurvive:
    @pytest.mark.parametrize("name,command,expected_id", HARD_BLOCKS,
                             ids=[h[0] for h in HARD_BLOCKS])
    def test_plain_form_still_blocks_with_its_own_rule(self, cfg, name, command,
                                                       expected_id):
        decision, rule_id = unattended_verdict(cfg, command)
        assert decision == "block"
        assert rule_id == expected_id, (
            f"{command!r} blocked via {rule_id!r}, not its own rule "
            f"{expected_id!r} — a generic rule is currently doing the work, so "
            f"this test would stay green if {expected_id} were deleted")

    @pytest.mark.parametrize("prefix", PREFIXES)
    @pytest.mark.parametrize("name,command,expected_id", HARD_BLOCKS,
                             ids=[h[0] for h in HARD_BLOCKS])
    def test_still_blocks_through_any_launcher_prefix(self, cfg, prefix, name,
                                                      command, expected_id):
        """The operation is 'run this payload'; the prefix is only phrasing.

        Before the masked-rescan fix, 62 of these 78 cells were NOT hard
        blocked: ``masked_subcommands`` recursed into a ``sh -c`` payload only
        when the shell was word 0, so any launcher blinded every anchored rule.
        ``rm -rf`` survived on luck alone — its rule is unanchored and still saw
        the raw haystack.
        """
        decision, rule_id = unattended_verdict(cfg, f"{prefix}bash -c '{command}'")
        assert decision == "block", (
            f"{prefix}bash -c '{command}' is NOT hard-blocked (id={rule_id!r})")
        assert rule_id == expected_id

    @pytest.mark.parametrize("name,command,expected_id", HARD_BLOCKS,
                             ids=[h[0] for h in HARD_BLOCKS])
    def test_uv_run_does_not_shadow_the_destructive_rule(self, cfg, name, command,
                                                        expected_id):
        """The specific hole Part 2 could have opened.

        ``uv run`` is now allowlisted, so if a destructive command behind it
        came back with a uv-run id, the resolver would ALLOW it unattended.
        Pinning the id is the only way to see that; the verdict alone reads
        BLOCK either way.
        """
        _, rule_id = decide(cfg, f"uv run {command}")
        assert rule_id not in UV_IDS
        assert rule_id == expected_id
        assert unattended_verdict(cfg, f"uv run {command}")[0] == "block"


class TestTheAllowlistCannotReachABlockTier:
    def test_no_default_id_belongs_to_a_hard_block_rule(self, cfg):
        """Structural: allowlisting only ever relaxes ``ask``, never ``block``.

        The resolver checks the allowlist on the ``ask`` branch only, so a
        block-tier id on the list would be inert rather than dangerous — but an
        inert entry reads as a granted permission to whoever adds the next one.
        """
        by_id = {p["id"]: p for p in cfg["bashToolPatterns"]
                 if isinstance(p, dict) and p.get("id")}
        for rule_id in sorted(C.DEFAULT_UNATTENDED_ALLOW):
            rule = by_id.get(rule_id)
            assert rule is not None
            assert rule.get("ask") or rule.get("bypassable"), (
                f"{rule_id} is a hard-block rule; allowlisting it is inert and "
                f"misleading")


# ---------------------------------------------------------------------------
# The masked-rescan fix, on its own terms
# ---------------------------------------------------------------------------


class TestShellPayloadRescan:
    def test_payload_is_rescanned_behind_a_prefix(self):
        """The unit-level statement of the fix."""
        masked = C.masked_subcommands("uv run bash -c 'git push --force'")
        assert "git push --force" in masked

    def test_word_zero_case_still_works(self):
        """Strictly additive: everything that recursed before still does."""
        assert "git push --force" in C.masked_subcommands("bash -c 'git push --force'")

    def test_absolute_shell_path_is_still_recognised(self):
        assert "git push --force" in C.masked_subcommands(
            "env /bin/bash -c 'git push --force'")

    def test_a_report_describing_the_command_is_not_rescanned(self, cfg):
        """#915's regression, which this fix must not reintroduce.

        A message *about* a blocked command is one quoted span, so ``bash`` is
        never a bare token in it and the rescan condition cannot fire. If this
        ever goes red, the tooling used to investigate an incident is refused
        by the incident's own rule — seven commands died that way in one day.
        """
        report = (
            "agentwire msg send --to orchestrator --kind done "
            "\"blocked: uv run bash -c 'git push --force' was refused\""
        )
        decision, _ = decide(cfg, report)
        assert decision != "block"

    def test_a_commit_message_quoting_the_command_is_not_blocked(self, cfg):
        commit = (
            'git commit -m "fix(safety): stop bash -c \'git push --force\' '
            'slipping past anchored rules"'
        )
        assert decide(cfg, commit)[0] != "block"

    def test_nested_payloads_still_terminate(self, cfg):
        """Recursion is on real nesting; it must not run away."""
        assert decide(cfg, "bash -c 'bash -c \"bash -c \\\"echo hi\\\"\"'")[0] != "block"
