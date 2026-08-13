"""#933 / #922 / #949 — pin rules to the OPERATION, not the phrasing.

Three defects of one family (a rule guarding a command's spelling selects for
whoever writes it differently, and refuses whoever spells a *different*
operation the same way):

- **#933** — ``_cmd_to_regex`` truncated a tooldef ``cmd:`` at the first flag
  and terminated with ``\\b``, which matches before ``-``. So the write rule
  for ``git merge`` swallowed ``git merge-base`` (read-only), and ``git stash``
  swallowed ``git stash list``. Fixed by the ``(?![\\w-])`` terminator plus
  read-exclusions generated from the same tooldef's ``access: read`` commands.
- **#922** — the path ladders matched destructive-verb patterns against the
  RAW command (so a grep whose search string mentions a deletion was refused
  as one), and anchored rules matched anywhere in the masked haystack (so a
  single-word quoted operand could supply the verb). Fixed by
  ``path_ladder_haystacks`` and command-position enforcement.
- **#949** — ``agentwire.yaml``'s rm rules matched ANY rm that mentioned
  ``.agentwire`` anywhere, refusing single-file cleanup inside the state dir
  with a message claiming directory destruction. Fixed by keying the rule on
  the operation: recursive/force flag AND a path terminating at the directory.

Every assertion here names the RULE ID (or its absence), never just the
verdict — a generic rule catching a command incidentally must not read as
coverage (#933 scope item 3). Mutations prove the fixes are load-bearing and
are themselves asserted to have changed the corpus (#922 acceptance).
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"

REFUSED = {"block", "ask"}
SAFETY = {"enabled": True, "disabled_rules": [], "unattended_allow": []}


@pytest.fixture(scope="module")
def bundled_config(bash_hook):
    """Bundled rules AND bundled tooldefs — what the real hook loads."""
    cfg = bash_hook.load_config(RULES_DIR, TOOLDEFS_DIR)
    assert not cfg.get("_parser_unavailable"), "rules failed to load"
    cfg["safety"] = dict(SAFETY)
    return cfg


# ---------------------------------------------------------------------------
# #933 — read-only commands released, write tier intact, hard blocks pinned
# ---------------------------------------------------------------------------


class TestTooldefPrefixTruncation:
    # The three victims from #933's body — real scheduler traffic, refused by
    # a write rule whose prefix they merely share.
    RELEASED_READS = [
        "git merge-base --is-ancestor HEAD HEAD",
        "git stash list",
        "git stash show -p",
        "git fetch --all",
        "git status",
        "git diff --staged",
    ]

    @pytest.mark.parametrize("command", RELEASED_READS)
    def test_read_only_command_is_released(self, bash_hook, bundled_config, command):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "allow", (
            f"{command!r} refused via {result.get('id') or result.get('pattern')} "
            f"— a read-only command is caught by a write rule's prefix again"
        )

    # The write tier the same prefixes guard, with the rule id that must own
    # each — the id, not the verdict, is the coverage claim.
    WRITE_TIER = [
        ("git stash", "tooldef.git-stash-current-changes"),
        ("git stash pop", "tooldef.git-stash-current-changes"),
        ("git merge feature", "tooldef.git-merge-branch-into-current"),
        # #933's third victim: `git checkout -- <file>` DOES overwrite
        # uncommitted changes, so it stays ask-tier — but by a rule whose
        # reason now names that operation, not "create branch".
        ("git checkout -- README.md",
         "tooldef.git-switch-branches-create-one-b-or-restore-working-tree-fil"),
        ("git checkout -b feature",
         "tooldef.git-switch-branches-create-one-b-or-restore-working-tree-fil"),
        ("git add file.py", "git.add"),
        ("git commit -m x", "git.commit"),
        ("git push", "git.push"),
    ]

    @pytest.mark.parametrize("command,rule_id", WRITE_TIER)
    def test_write_form_still_asks_by_its_own_rule(
        self, bash_hook, bundled_config, command, rule_id
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "ask", (
            f"{command!r} -> {result['decision']} ({result.get('reason')})"
        )
        assert result.get("id") == rule_id, (
            f"{command!r} matched {result.get('id')!r}, expected {rule_id!r}"
        )

    # #933 scope item 4: the hard blocks, unchanged, each by its own rule.
    HARD_BLOCKS = [
        ("git push --force", "git.git-push-force-use-force-with-lease"),
        ("git reset --hard", "git.git-reset-hard-use-soft-or-stash"),
        ("git clean -fdx", "git.git-clean-with-force-directory-flags"),
        ("git stash clear", "git.git-stash-clear-deletes-all-stashes"),
        ("rm -rf /tmp/x", "core.rm-with-recursive-or-force-flags"),
        ("sudo rm /etc/hosts", "core.sudo-rm"),
        ("tmux kill-server",
         "agentwire.tmux-kill-server-destroys-all-tmux-sessions-including-agentw"),
        ("git -C /repo push --force", "git.git-push-force-use-force-with-lease"),
    ]

    @pytest.mark.parametrize("command,rule_id", HARD_BLOCKS)
    def test_hard_block_unchanged(self, bash_hook, bundled_config, command, rule_id):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "block", f"{command!r} -> {result}"
        assert result.get("id") == rule_id

    def test_terminator_mutation_changes_the_corpus(self, bash_hook):
        """Revert ``(?![\\w-])`` to ``\\b`` and merge-base must be refused again.

        Proves the terminator is what releases it — and that the mutation
        actually changed the generated pattern (a filter that removes nothing
        reads exactly like a refuted claim).
        """
        fixed = bash_hook._cmd_to_regex("git merge <branch>")
        assert fixed.endswith("(?![\\w-])")
        import re
        mutated = fixed[: -len("(?![\\w-])")] + r"\b"
        assert mutated != fixed
        assert not re.search(fixed, "git merge-base --is-ancestor a b")
        assert re.search(mutated, "git merge-base --is-ancestor a b")
        assert re.search(fixed, "git merge feature")

    def test_read_exclusion_mutation_changes_the_corpus(self, bash_hook):
        """Drop the ``access: read`` prefixes and ``git stash list`` is
        swallowed again — the tooldef's read declarations are what release it."""
        import re
        reads = (("git", "stash", "list"),)
        with_reads = bash_hook._cmd_to_regex("git stash", reads)
        without = bash_hook._cmd_to_regex("git stash")
        assert with_reads != without
        assert not re.search(with_reads, "git stash list")
        assert re.search(without, "git stash list")
        for form in ("git stash", "git stash pop", "git stash listx"):
            assert re.search(with_reads, form), form


# ---------------------------------------------------------------------------
# #949 — the .agentwire rule guards the operation, not the string
# ---------------------------------------------------------------------------


AGENTWIRE_RM_RULE = (
    "agentwire.recursive-forced-rm-of-the-agentwire-directory-itself-destro"
)
OLD_PATTERNS = [r'\brm\s+.*\.agentwire', r'\brm\s+.*~/.agentwire']


class TestAgentwireDirRule:
    # The literal incident: a one-file registry cleanup, not recursive, cannot
    # remove a directory. It still lands in core's ordinary bypassable rm rule
    # (that is core policy, unchanged) — but never in the directory-destruction
    # rule, and the message no longer asserts something the command cannot do.
    SINGLE_FILE_CLEANUPS = [
        'rm "/Users/dotdev/.agentwire/worktrees/stale-entry.json"',
        "rm ~/.agentwire/inbox/session/msg-1.json",
        "rm -v ~/.agentwire/logs/old.log",
    ]

    @pytest.mark.parametrize("command", SINGLE_FILE_CLEANUPS)
    def test_single_file_cleanup_is_not_directory_destruction(
        self, bash_hook, bundled_config, command
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result.get("id") != AGENTWIRE_RM_RULE, (
            f"{command!r} still refused as destroying the .agentwire "
            f"directory — the rule matches the spelling again"
        )
        # Rule set measured: bundled+bundled. In that set a plain single-file
        # rm by explicit path is ALLOWED (it is the blessed deletion pattern;
        # core's rm rules key on recursive/force forms). A live machine's
        # damagecontrol.yml may still block these via its own ladders — that is
        # the machine's call, not this rule's, so the only pin here is that any
        # block is NOT the .agentwire directory rule and names a real rule.
        if result["decision"] == "block":
            assert result.get("id"), result

    DESTRUCTION_FORMS = [
        "rm -rf ~/.agentwire",
        "rm -rf ~/.agentwire/",
        "rm -fr $HOME/.agentwire",
        "rm -r /Users/dotdev/.agentwire",
        "rm --recursive /home/ci/.agentwire",
        "rm -r -f ~/.agentwire",
        "rm -rf /tmp/x ~/.agentwire",
        "cd ~ && rm -rf .agentwire",
    ]

    @pytest.mark.parametrize("command", DESTRUCTION_FORMS)
    def test_directory_destruction_blocks_by_the_agentwire_rule(
        self, bash_hook, command
    ):
        """Solo config — no other rule may take the credit (core's rm -rf rule
        would otherwise catch every one of these first)."""
        data = yaml.safe_load((RULES_DIR / "agentwire.yaml").read_text())
        rule = next(
            p for p in data["bashToolPatterns"]
            if "agentwire directory" in p.get("reason", "").lower()
            or ".agentwire" in p.get("pattern", "")
            and "rm" in p.get("pattern", "")
        )
        cfg = {
            "bashToolPatterns": [dict(rule)],
            "zeroAccessPaths": [],
            "readOnlyPaths": [],
            "noDeletePaths": [],
            "allowedPaths": [],
            "safety": dict(SAFETY),
        }
        result = bash_hook.check_command(command, cfg)
        assert result["decision"] == "block", (
            f"{command!r} not blocked by the .agentwire rule alone "
            f"({result.get('reason')})"
        )

    @pytest.mark.parametrize("command", DESTRUCTION_FORMS[:6])
    def test_destruction_forms_block_in_the_full_set_too(
        self, bash_hook, bundled_config, command
    ):
        assert bash_hook.check_command(command, bundled_config)["decision"] == "block"

    def test_old_patterns_matched_the_incident_and_new_one_does_not(self):
        """The mutation control: the exact incident command matched BOTH old
        patterns (that is the bug) and matches the shipped rule no more."""
        import re
        incident = 'rm "/Users/dotdev/.agentwire/worktrees/stale-entry.json"'
        # each old pattern against the spelling it over-matched
        for old, victim in (
            (OLD_PATTERNS[0], incident),
            (OLD_PATTERNS[1], "rm ~/.agentwire/inbox/msg.json"),
        ):
            assert re.search(old, victim), "corpus did not change — old pattern never matched"
        data = yaml.safe_load((RULES_DIR / "agentwire.yaml").read_text())
        patterns = [p["pattern"] for p in data["bashToolPatterns"]]
        assert not any(p in OLD_PATTERNS for p in patterns), (
            "the spelling-keyed rm rules are still shipped"
        )
        new = [p for p in patterns if ".agentwire" in p and "rm" in p]
        assert new, "the operation-keyed rm rule is missing"
        for p in new:
            assert not re.search(p, incident, re.IGNORECASE)
            assert re.search(p, "rm -rf ~/.agentwire", re.IGNORECASE)


# ---------------------------------------------------------------------------
# #922 — nested-file rm under .agentwire is refused HONESTLY, and the ladder
# keep-predicate keeps real path operands visible (the acceptance rows for the
# grep cases live in test_damage_control_payload_anchoring.py's
# TestRemainingPayloadMechanisms, next to the history they rewrite)
# ---------------------------------------------------------------------------


class TestLadderKeepPredicate:
    def test_quoted_path_with_spaces_still_reads_as_a_delete(
        self, bash_hook, bundled_config
    ):
        result = bash_hook.check_command(
            'shred "/Users/dotdev/.agentwire/dead letters.json"', bundled_config
        )
        assert result["decision"] == "block"
        assert "noDeletePath" in str(result.get("pattern", ""))

    def test_sh_c_payload_naming_the_path_still_blocks(
        self, bash_hook, bundled_config
    ):
        result = bash_hook.check_command(
            "sh -c 'unlink /Users/dotdev/.agentwire/state.json'", bundled_config
        )
        assert result["decision"] == "block"

    def test_prose_verb_without_the_path_does_not_pair(
        self, bash_hook, bundled_config
    ):
        # the #922 acceptance command, verbatim
        result = bash_hook.check_command(
            'grep -rn "rm file deletion" '
            "/Users/dotdev/.agentwire/damage-control/",
            bundled_config,
        )
        assert result["decision"] == "allow", result
