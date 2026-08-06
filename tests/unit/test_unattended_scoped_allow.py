"""Path-scoped unattended grants (#914).

The property under test is NOT "the allowed case works". It is: for every
scoped grant, the OUT-OF-SCOPE case is still refused. Half these tests are
therefore negative, and each positive case has a companion negative one that
differs only in the target directory.

Two layers deliberately:
  * unit tests over the scope primitives, and
  * end-to-end tests through `check_command` against the BUNDLED rule set —
    because a synthetic one-rule config proves the fixture's shape, not the
    behaviour (the TestAnchoredRules lesson: it stays green through 13 real
    regressions because it never loads the real YAMLs).
"""

import json
from pathlib import Path

import pytest

from agentwire.safety import _core
from agentwire.safety._core import (
    DEFAULT_UNATTENDED_ALLOW,
    command_scope_dirs,
    encode_unattended_allow,
    git_global_dirs,
    load_config,
    parse_unattended_allow,
    path_in_scope,
    resolve_unattended_grants,
    unattended_grant_allows,
)

BUNDLED_RULES = Path(_core.__file__).parent.parent / "hooks" / "damage-control" / "rules"
BUNDLED_TOOLDEFS = Path(_core.__file__).parent.parent / "tooldefs"

# The rule pattern the git-commit tooldef generates. Used where a test needs a
# pattern without loading the whole rule set.
COMMIT_PATTERN = r"\bgit\s+commit\b"


@pytest.fixture
def store(tmp_path):
    """A stand-in memory store, plus an out-of-scope repo beside it."""
    s = tmp_path / "projects" / "proj-a" / "memory"
    s.mkdir(parents=True)
    other = tmp_path / "elsewhere" / "agentwire-dev"
    other.mkdir(parents=True)
    return s, other, f"{tmp_path}/projects/*/memory/"


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------


class TestParseUnattendedAllow:
    def test_bare_string_form_still_works(self):
        """The deployed form. Breaking it breaks every live task."""
        grants, errors = parse_unattended_allow(["outbound.agentwire-email"])
        assert grants == {"outbound.agentwire-email": [[]]}
        assert errors == []

    def test_scoped_form(self):
        grants, errors = parse_unattended_allow(
            [{"id": "git.commit", "paths": ["~/x/", "/opt/y"]}]
        )
        assert grants == {"git.commit": [["~/x/", "/opt/y"]]}
        assert errors == []

    def test_mixed_forms(self):
        grants, _ = parse_unattended_allow(
            ["a.b", {"id": "c.d", "paths": ["/x"]}]
        )
        assert grants == {"a.b": [[]], "c.d": [["/x"]]}

    @pytest.mark.parametrize("entry,fragment", [
        ({"id": "a.b", "paths": "not-a-list"}, "must be a list"),
        ({"id": "a.b", "paths": []}, "is empty"),
        ({"id": "a.b", "paths": ["relative/dir"]}, "must be absolute"),
    ])
    def test_malformed_entry_grants_nothing_and_is_reported(self, entry, fragment):
        """Fail closed AND fail loudly: a dropped grant that says nothing is how
        this shows up as a 04:00 timeout instead of a review comment.

        The empty list is not the same as absent — see the fall-through test."""
        grants, errors = parse_unattended_allow([entry])
        assert grants == {"a.b": []}
        assert any(fragment in e for e in errors)

    @pytest.mark.parametrize("entry,fragment", [
        ({"paths": ["/x"]}, "no `id`"),
        (["nested"], "must be a rule id or a mapping"),
    ])
    def test_entry_naming_no_rule_is_dropped(self, entry, fragment):
        """Nothing to bind, so nothing is recorded — but it is still reported."""
        grants, errors = parse_unattended_allow([entry])
        assert grants == {}
        assert any(fragment in e for e in errors)

    def test_relative_scope_does_not_partially_grant(self):
        """An entry with one bad path must not fall back to its good ones —
        a half-parsed scope is a different grant than the one written."""
        grants, errors = parse_unattended_allow(
            [{"id": "a.b", "paths": ["/good", "bad/relative"]}]
        )
        assert grants == {"a.b": []}
        assert errors

    def test_malformed_scope_does_not_fall_through_to_the_unscoped_default(self, monkeypatch):
        """The sharpest failure mode in this feature, found by the acceptance
        matrix rather than by reasoning.

        `git.commit` is granted UNSCOPED by DEFAULT_UNATTENDED_ALLOW. If a
        refused entry merely vanished, a typo in a scope path
        (`paths: [relative/dir]`) would drop the task layer and hand the task
        the unscoped default — a typo silently granting commits in EVERY repo,
        which is strictly worse than the grant being written. Naming a rule
        binds it whether or not the entry parses."""
        assert "git.commit" in DEFAULT_UNATTENDED_ALLOW
        monkeypatch.setenv(
            "AGENTWIRE_UNATTENDED_ALLOW",
            json.dumps([{"id": "git.commit", "paths": ["relative/dir"]}]),
        )
        grants = resolve_unattended_grants({})
        assert grants["git.commit"] == [], "must NOT inherit the unscoped default"

        ok, why = unattended_grant_allows(
            "git.commit", "git commit -m x", grants, "/anywhere", COMMIT_PATTERN
        )
        assert not ok
        assert "malformed" in why

    def test_unknown_key_reported(self):
        _, errors = parse_unattended_allow([{"id": "a.b", "path": "/x"}])
        assert any("ignored key" in e for e in errors)


class TestWireFormat:
    def test_bare_list_stays_comma_separated(self):
        """Readable in `tmux show-environment` for the common case."""
        assert encode_unattended_allow(["a.b", "c.d"]) == "a.b,c.d"

    def test_scoped_list_becomes_json(self):
        encoded = encode_unattended_allow([{"id": "a.b", "paths": ["/x"]}])
        assert json.loads(encoded) == [{"id": "a.b", "paths": ["/x"]}]

    @pytest.mark.parametrize("entries", [
        ["a.b", "c.d"],
        [{"id": "a.b", "paths": ["/x", "/y"]}],
        ["a.b", {"id": "c.d", "paths": ["~/z/"]}],
    ])
    def test_round_trip_preserves_the_grant(self, entries, monkeypatch):
        """THE inheritance property: what a child inherits must be the same
        grant the host wrote, scope included. Flattening to a bare id here
        hands every child in a fan-out the unscoped version."""
        monkeypatch.setenv("AGENTWIRE_UNATTENDED_ALLOW", encode_unattended_allow(entries))
        assert parse_unattended_allow(_core._env_unattended_allow())[0] == \
            parse_unattended_allow(entries)[0]

    def test_undecodable_json_grants_nothing(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_UNATTENDED_ALLOW", '[{"id": "a.b"')
        assert _core._env_unattended_allow() == []


class TestPrecedence:
    """Naming a rule at a more specific layer REPLACES the looser grant.

    Under a union, `{id: git.commit, paths: [...]}` would be silently
    meaningless — `git.commit` is already granted unscoped by the defaults —
    and a config that reads as a constraint and isn't is the whole bug class
    this issue is about.
    """

    def test_task_scope_overrides_unscoped_default(self, monkeypatch):
        assert "git.commit" in DEFAULT_UNATTENDED_ALLOW
        monkeypatch.setenv(
            "AGENTWIRE_UNATTENDED_ALLOW",
            encode_unattended_allow([{"id": "git.commit", "paths": ["/only/here"]}]),
        )
        grants = resolve_unattended_grants({})
        assert grants["git.commit"] == [["/only/here"]]

    def test_default_survives_when_task_does_not_name_it(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_UNATTENDED_ALLOW", "some.other-rule")
        grants = resolve_unattended_grants({})
        assert grants["git.commit"] == [[]]

    def test_host_config_layer_overrides_default(self, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_UNATTENDED_ALLOW", raising=False)
        grants = resolve_unattended_grants(
            {"safety": {"unattended_allow": [{"id": "git.push", "paths": ["/repos"]}]}}
        )
        assert grants["git.push"] == [["/repos"]]

    def test_env_layer_overrides_host_config(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_UNATTENDED_ALLOW", "git.push")
        grants = resolve_unattended_grants(
            {"safety": {"unattended_allow": [{"id": "git.push", "paths": ["/repos"]}]}}
        )
        assert grants["git.push"] == [[]]


# ---------------------------------------------------------------------------
# Scope matching
# ---------------------------------------------------------------------------


class TestPathInScope:
    def test_directory_itself_and_descendants(self):
        assert path_in_scope("/a/b", "/a/b/")
        assert path_in_scope("/a/b/c/d", "/a/b/")

    def test_sibling_with_shared_prefix_is_out(self):
        """`/a/bc` must not match scope `/a/b` — prefix matching on raw strings
        is the classic way a scope leaks to a neighbour."""
        assert not path_in_scope("/a/bc", "/a/b/")

    def test_parent_is_out(self):
        assert not path_in_scope("/a", "/a/b/")

    def test_single_star_does_not_cross_a_separator(self):
        assert path_in_scope("/p/proj-a/memory", "/p/*/memory/")
        assert not path_in_scope("/p/x/y/memory", "/p/*/memory/")

    def test_double_star_crosses(self):
        assert path_in_scope("/p/x/y/memory", "/p/**/memory/")

    def test_case_is_not_folded(self):
        """A scope is a security boundary; folding case on a case-insensitive
        filesystem is the wrong direction to be wrong in."""
        assert not path_in_scope("/A/B", "/a/b/")


class TestGitGlobalDirs:
    @pytest.mark.parametrize("argv,expected", [
        (["git", "-C", "/r", "commit"], ["/r"]),
        (["git", "--git-dir=/r/.git", "commit"], ["/r/.git"]),
        (["git", "--git-dir", "/r/.git", "commit"], ["/r/.git"]),
        (["git", "--work-tree=/w", "commit"], ["/w"]),
        (["git", "--work-tree", "/w", "commit"], ["/w"]),
        (["git", "--git-dir=/r/.git", "--work-tree=/w", "commit"], ["/r/.git", "/w"]),
        (["git", "-C", "/a", "-C", "/b", "commit"], ["/a", "/b"]),
        (["git", "commit"], []),
    ])
    def test_collects_every_selector(self, argv, expected):
        """All three selectors pick the repo independently, so a scope that
        reads one of them is a scope over one of them."""
        assert git_global_dirs(argv)[0] == expected

    def test_strips_globals_to_the_form_rules_are_written_against(self):
        assert git_global_dirs(["git", "-C", "/r", "commit", "-m", "x"])[1] == \
            ["git", "commit", "-m", "x"]

    def test_dash_c_config_value_is_not_mistaken_for_a_subcommand(self):
        _, rest = git_global_dirs(["git", "-c", "core.pager=less", "commit"])
        assert rest == ["git", "commit"]


# ---------------------------------------------------------------------------
# Which directory does a command act on?
# ---------------------------------------------------------------------------


class TestCommandScopeDirs:
    def test_plain_command_uses_cwd(self):
        dirs, err = command_scope_dirs("git commit -m x", "/work/repo", COMMIT_PATTERN)
        assert err is None
        assert dirs == ["/work/repo"]

    def test_dash_c_overrides_cwd(self):
        dirs, err = command_scope_dirs("git -C /other commit -m x", "/work/repo", COMMIT_PATTERN)
        assert err is None
        assert dirs == ["/other"]

    def test_cd_joined_by_and_pins_the_directory(self):
        """The most natural phrasing of the motivating case. Refusing it would
        make the feature unusable for the task it exists for."""
        dirs, err = command_scope_dirs("cd /store && git commit -m x", "/work/repo", COMMIT_PATTERN)
        assert err is None
        assert dirs == ["/store"]

    def test_innocuous_tail_does_not_drag_cwd_in(self):
        dirs, err = command_scope_dirs(
            "git -C /store commit -m x && echo done", "/work/repo", COMMIT_PATTERN
        )
        assert err is None
        assert dirs == ["/store"]

    def test_relative_cd_resolves_against_cwd(self):
        dirs, err = command_scope_dirs("cd sub && git commit -m x", "/work/repo", COMMIT_PATTERN)
        assert err is None
        assert dirs == ["/work/repo/sub"]

    @pytest.mark.parametrize("command,fragment", [
        ("cd /store ; git commit -m x", "rather than '&&'"),
        ("cd /store || git commit -m x", "rather than '&&'"),
        ("cd - && git commit -m x", "not a literal path"),
        ("cd $TARGET && git commit -m x", "not a literal path"),
        ("sh -c 'git commit -m x'", "runs through sh"),
        ("xargs git commit -m x", "runs through xargs"),
        ("ssh host git commit -m x", "runs through ssh"),
        ("git commit -m $(cat /tmp/x)", "command substitution"),
        ("( cd /store && git commit -m x )", "subshell"),
        ("pushd /store && git commit -m x", "directory stack"),
    ])
    def test_unresolvable_forms_report_why(self, command, fragment):
        """Every one of these must refuse rather than guess. `;` is the sharp
        one: the next command runs even if the `cd` failed, so it could execute
        in either directory."""
        dirs, err = command_scope_dirs(command, "/work/repo", COMMIT_PATTERN)
        assert err is not None, f"{command!r} was resolved to {dirs}"
        assert fragment in err

    def test_traversal_is_normalized_not_trusted(self):
        """`..` must be collapsed before matching, and the resolved form is
        reported alongside it (on macOS /etc is itself a symlink, which is the
        realpath half doing its job)."""
        dirs, err = command_scope_dirs(
            "git -C /store/../../etc commit -m x", "/work", COMMIT_PATTERN
        )
        assert err is None
        assert "/etc" in dirs
        assert not any("/store" in d for d in dirs)

    def test_matches_git_stripped_form_too(self):
        """#913 will normalize git's global options out before rule matching.
        Scope evaluation must keep working on either side of that change, so it
        tries the stripped form as well."""
        dirs, err = command_scope_dirs("git -C /store commit -m x", "/work", r"\bgit\s+commit\b")
        assert err is None and dirs == ["/store"]


# ---------------------------------------------------------------------------
# The grant decision — every scoped grant with its out-of-scope twin
# ---------------------------------------------------------------------------


class TestGrantDecision:
    def _grants(self, scope):
        return {"git.commit": [[scope]]}

    def test_in_scope_allowed(self, store):
        s, _, scope = store
        ok, why = unattended_grant_allows(
            "git.commit", "git commit -m x", self._grants(scope), str(s), COMMIT_PATTERN
        )
        assert ok, why

    def test_out_of_scope_refused(self, store):
        """The companion every scoped grant needs. Same grant, same command —
        only the directory differs."""
        _, other, scope = store
        ok, why = unattended_grant_allows(
            "git.commit", "git commit -m x", self._grants(scope), str(other), COMMIT_PATTERN
        )
        assert not ok
        assert str(other) in why and scope in why

    def test_mixed_in_and_out_of_scope_is_refused(self, store):
        """`cd <in-scope> && git -C <out-of-scope> commit` — ALL-paths
        semantics. A "some in-scope path appears" check passes this."""
        s, other, scope = store
        ok, why = unattended_grant_allows(
            "git.commit", f"cd {s} && git -C {other} commit -m x",
            self._grants(scope), "/work", COMMIT_PATTERN,
        )
        assert not ok
        assert str(other) in why

    def test_in_scope_git_dir_with_out_of_scope_work_tree_is_refused(self, store):
        """Allowed today by construction: the in-scope selector satisfies a
        naive check while the out-of-scope one does the actual work."""
        s, other, scope = store
        ok, why = unattended_grant_allows(
            "git.commit", f"git --git-dir={s}/.git --work-tree={other} commit -m x",
            self._grants(scope), "/work", COMMIT_PATTERN,
        )
        assert not ok
        assert str(other) in why

    def test_traversal_out_of_scope_is_refused(self, store):
        s, _, scope = store
        ok, why = unattended_grant_allows(
            "git.commit", f"git -C {s}/../../.. commit -m x",
            self._grants(scope), "/work", COMMIT_PATTERN,
        )
        assert not ok

    def test_real_symlink_out_of_the_scope_dir_is_refused(self, store):
        """Not a string — an actual symlink on disk. The grantee can write
        inside the scope (`ln -s`, `mkdir`, `git init` are all allowed
        unattended), so a textual scope over a writable dir is one symlink from
        meaningless."""
        s, other, scope = store
        escape = s / "escape"
        escape.symlink_to(other)
        assert path_in_scope(str(escape), scope), "lexically inside — the trap"
        ok, why = unattended_grant_allows(
            "git.commit", f"git -C {escape} commit -m x",
            self._grants(scope), "/work", COMMIT_PATTERN,
        )
        assert not ok, "a symlink out of the scope dir must not be in scope"
        assert str(other) in why

    def test_symlinked_scope_root_still_admits_its_own_contents(self, tmp_path):
        """The other direction: resolving must not break a scope whose root is
        itself reached through a symlink."""
        real = tmp_path / "real" / "projects" / "p" / "memory"
        real.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real")
        scope = f"{link}/projects/*/memory/"
        ok, why = unattended_grant_allows(
            "git.commit", "git commit -m x", {"git.commit": [[scope]]},
            str(link / "projects" / "p" / "memory"), COMMIT_PATTERN,
        )
        assert ok, why

    def test_unresolvable_target_is_refused_with_the_scope_named(self, store):
        s, _, scope = store
        ok, why = unattended_grant_allows(
            "git.commit", "sh -c 'git commit -m x'",
            self._grants(scope), str(s), COMMIT_PATTERN,
        )
        assert not ok
        assert scope in why and "sh" in why

    def test_unscoped_grant_is_unaffected_by_directory(self, store):
        _, other, _ = store
        ok, _ = unattended_grant_allows(
            "git.commit", "git commit -m x", {"git.commit": [[]]}, str(other), COMMIT_PATTERN
        )
        assert ok

    def test_ungranted_rule_is_refused(self, store):
        s, _, scope = store
        ok, why = unattended_grant_allows(
            "other.rule", "git commit -m x", self._grants(scope), str(s), COMMIT_PATTERN
        )
        assert not ok
        assert "not on the unattended allowlist" in why

    def test_scoped_grant_refuses_when_there_is_no_filesystem_target(self, store):
        """The MCP hook's synthesized `agentwire email --to …` names no
        directory. Measuring a path scope against the session cwd there would
        allow on a coincidence."""
        s, _, scope = store
        ok, why = unattended_grant_allows(
            "outbound.agentwire-email", "agentwire email --to a@b.c",
            {"outbound.agentwire-email": [[scope]]}, str(s), scopeable=False,
        )
        assert not ok
        assert "no filesystem target" in why

    def test_bare_environment_assignment_refuses(self, store):
        """`FOO=1 git commit` — an env var we do not model. CHOSEN: refuse.

        We cannot know whether an unmodelled variable redirects the tool, and
        refusing is consistent with the `env(1)` spelling, which was already
        refused for the same reason."""
        s, _, scope = store
        ok, why = unattended_grant_allows(
            "git.commit", "FOO=1 git commit -m x", self._grants(scope), str(s), COMMIT_PATTERN
        )
        assert not ok
        assert "FOO" in why

    def test_uv_run_is_scopeable_by_cwd(self, store):
        """The scope unit is the effective DIRECTORY, not an argv path — so it
        is not git-only. `uv run` names no path at all and still scopes."""
        s, other, scope = store
        grants = {"uv.run": [[scope]]}
        pattern = r"\buv\s+run\b"
        ok, _ = unattended_grant_allows("uv.run", "uv run pytest", grants, str(s), pattern)
        assert ok
        ok, why = unattended_grant_allows("uv.run", "uv run pytest", grants, str(other), pattern)
        assert not ok, "out-of-scope companion"
        assert str(other) in why


# ---------------------------------------------------------------------------
# End to end against the BUNDLED rule set (not a synthetic one-rule config)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bundled_config():
    cfg = load_config(BUNDLED_RULES, BUNDLED_TOOLDEFS)
    cfg["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return cfg


class TestAgainstBundledRules:
    def test_bundled_tooldefs_carry_every_default_grant_id(self, bundled_config):
        """The four missing `id:` lines behind #914's motivating failure. If
        these ever drop out of the bundled copy again, five of six built-in
        grants go silently inert."""
        from agentwire.safety.lint import unattended_defaults_missing
        assert unattended_defaults_missing(bundled_config) == []

    def test_git_commit_is_ask_tier_here(self, bundled_config):
        from agentwire.safety._core import check_command
        result = check_command("git commit -m 'x'", bundled_config)
        assert result["decision"] == "ask"
        assert result["id"] == "git.commit"

    def test_scoped_grant_end_to_end(self, bundled_config, store):
        """Real rule id, real pattern, real decision — in-scope allowed and the
        identical command out of scope refused."""
        from agentwire.safety._core import check_command
        s, other, scope = store
        result = check_command("git commit -m 'x'", bundled_config)
        grants = {"git.commit": [[scope]]}

        ok, _ = unattended_grant_allows(
            result["id"], result["command"], grants, str(s), result["pattern"]
        )
        assert ok

        ok, why = unattended_grant_allows(
            result["id"], result["command"], grants, str(other), result["pattern"]
        )
        assert not ok
        assert str(other) in why

    def test_acting_on_a_repo_outside_the_scope_is_refused_in_every_spelling(
        self, bundled_config, store
    ):
        """THE OPERATION, with spellings as cases underneath it.

        #913's lesson is "the rule matched a phrasing, not an operation". The
        first draft of this scope check repeated it exactly: it matched the
        `-C` phrasing and missed the environment-assignment spelling of the
        identical redirection, while the `env(1)` spelling was already refused
        for being an indirect runner. One spelling caught, its neighbour not,
        inside the fix for that very defect.

        So this test names the OPERATION — "the command acts on a repository
        other than the scoped one" — and enumerates spellings beneath it. A new
        spelling someone invents fails here instead of shipping.

        Asserts the rule id, not just the tier: #913 changed which rule these
        commands match, and a tier-only assertion sails through that kind of
        transition without noticing which rule produced the verdict.

        REBASED ONTO #913 (#918). Every spelling now reaches the rule — the six
        that read `False` while `git -C` was bypassed are enforced end to end
        from here on. They were asserted BOTH ways precisely so this flip would
        be forced by a red test rather than remembered.
        """
        from agentwire.safety._core import check_command
        s, other, scope = store
        grants = {"git.commit": [[scope]]}

        for command in [
            f"GIT_DIR={other}/.git git commit -m x",
            f"GIT_WORK_TREE={other} git commit -m x",
            f"GIT_DIR={other}/.git GIT_WORK_TREE={other} git commit -m x",
            f"GIT_INDEX_FILE={other}/idx git commit -m x",
            f"env GIT_DIR={other}/.git git commit -m x",
            f"cd {other} && git commit -m x",
            f"git --git-dir={other}/.git commit -m x",
            f"git --git-dir {other}/.git commit -m x",
            # Before #918 this one was the control for the `.git`-suffix
            # accident: a repo dir NOT named `.git`, which the old
            # `\bgit\s+commit\b` could not find. It matches now because the
            # normalizer strips the option rather than relying on the path.
            f"git --git-dir={other}/x commit -m x",
            f"git -C {other} commit -m x",
            f"git --work-tree={other} commit -m x",
            f"git --git-dir={s}/.git --work-tree={other} commit -m x",
            f"cd {s} && git -C {other} commit -m x",
            f"git -C {s}/../../.. commit -m x",
        ]:
            result = check_command(command, bundled_config)
            assert result["decision"] == "ask", (
                f"{command!r}: expected to reach the ask tier, got "
                f"decision={result['decision']} id={result.get('id')}"
            )
            assert result["id"] == "git.commit", command
            ok, why = unattended_grant_allows(
                result["id"], command, grants, str(s), result["pattern"]
            )
            assert not ok, f"{command!r} was GRANTED — it acts outside the scope"
            assert scope in why, f"{command!r}: refusal must name the scope it violated"

    def test_scoped_grant_does_not_weaken_the_force_push_block_behind_dash_c(
        self, bundled_config, store
    ):
        """Cross-PR case (#913 + #914): `git -C <dir> push --force` must BLOCK.

        Neither PR could assert this alone — before #918 the `-C` form matched
        nothing, and #914 cannot make it match. Asserts the RULE ID, because a
        bare BLOCK would document the guard going away just as happily as it
        catches it: any hard-block rule, or none at all plus a different
        failure, could produce that verdict.
        """
        from agentwire.safety._core import check_command
        result = check_command("git -C /repo push --force origin main", bundled_config)
        assert result["decision"] == "block"
        assert result["id"] == "git.git-push-force-use-force-with-lease"

        # A grant cannot downgrade this by construction: grants are not an
        # input to check_command at all, and the hook consults them only in the
        # `ask` branch, which a `block` never reaches. Pinned so a future
        # refactor that threads grants in earlier has to confront this test.
        assert "block" == check_command(
            "git -C /repo push --force origin main",
            {**bundled_config, "safety": {**bundled_config["safety"],
                                          "unattended_allow": [result["id"]]}},
        )["decision"]

    def test_scoped_grant_over_a_repo_subdirectory_does_not_leak_to_the_repo(self, tmp_path):
        """git resolves its repo by walking UP, so the directory a command runs
        in is not the repo it writes to. Scope `<repo>/inner` must not grant
        over `<repo>` — the memory stores are safe today only because they
        happen to be repo roots, and `git init ~/.claude` would end that."""
        repo = tmp_path / "outer"
        inner = repo / "inner"
        inner.mkdir(parents=True)
        (repo / ".git").mkdir()
        ok, why = unattended_grant_allows(
            "git.commit", "git commit -m x", {"git.commit": [[f"{inner}/"]]},
            str(inner), COMMIT_PATTERN,
        )
        assert not ok, "the enclosing repo root must be in scope too"
        assert str(repo) in why

    def test_scope_at_the_repo_root_still_works(self, tmp_path):
        """The companion: the root-scoped case must not be broken by the walk-up."""
        repo = tmp_path / "outer"
        repo.mkdir()
        (repo / ".git").mkdir()
        ok, why = unattended_grant_allows(
            "git.commit", "git commit -m x", {"git.commit": [[f"{repo}/"]]},
            str(repo), COMMIT_PATTERN,
        )
        assert ok, why

    def test_hard_block_rules_are_untouched_by_any_grant(self, bundled_config, store):
        """A grant resolves the `ask` tier only. `block` never reaches it."""
        from agentwire.safety._core import check_command
        s, _, _ = store
        for command in ("rm -rf /tmp/x", "git push --force origin main",
                        "git reset --hard origin/main"):
            assert check_command(command, bundled_config)["decision"] == "block", command
