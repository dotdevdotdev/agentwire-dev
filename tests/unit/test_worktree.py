"""Tests for agentwire/worktree.py — Session name parsing, paths."""

import subprocess

import pytest

from agentwire.worktree import (
    apply_naming,
    default_base_branch,
    ensure_worktree,
    get_project_type,
    git_common_dir,
    git_root,
    is_git_repo,
    is_valid_branch_name,
    parse_session_name,
    slugify,
)


def _make_repo(tmp_path, name="repo", default_branch="main"):
    """Init a git repo with one commit on `default_branch`."""
    repo = tmp_path / name
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True)

    git("init", "-q", "-b", default_branch)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return repo, git


# --- parse_session_name ---

class TestParseSessionName:
    def test_simple(self):
        assert parse_session_name("myapp") == ("myapp", None, None)

    def test_with_branch(self):
        assert parse_session_name("myapp/feature") == ("myapp", "feature", None)

    def test_with_machine(self):
        assert parse_session_name("myapp@server") == ("myapp", None, "server")

    def test_with_branch_and_machine(self):
        assert parse_session_name("myapp/feature@server") == ("myapp", "feature", "server")

    def test_deep_branch(self):
        # "myapp/feat/sub" — first / splits project from branch
        project, branch, machine = parse_session_name("myapp/feat/sub")
        assert project == "myapp"
        assert branch == "feat/sub"
        assert machine is None


# --- is_git_repo ---

class TestIsGitRepo:
    def test_with_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert is_git_repo(tmp_path) is True

    def test_without_git_dir(self, tmp_path):
        assert is_git_repo(tmp_path) is False


# --- get_project_type ---

class TestGetProjectType:
    def test_full_with_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert get_project_type(tmp_path) == "full"

    def test_scratch_without_git(self, tmp_path):
        assert get_project_type(tmp_path) == "scratch"


# --- slugify / apply_naming ---

class TestNaming:
    def test_slugify_basic(self):
        assert slugify("Fix Bug") == "fix-bug"
        assert slugify("feature/auth-v2") == "feature-auth-v2"
        assert slugify("  Hello  World  ") == "hello-world"

    def test_slugify_empty_fallback(self):
        assert slugify("!!!") == "wt"

    def test_apply_naming_none_is_verbatim(self):
        assert apply_naming(None, "my-branch") == "my-branch"
        assert apply_naming("", "my-branch") == "my-branch"

    def test_apply_naming_user_slug_template(self):
        import getpass
        out = apply_naming("{user}/{slug}", "Fix Bug")
        assert out == f"{getpass.getuser()}/fix-bug"

    def test_apply_naming_literal_prefix(self):
        assert apply_naming("feature-{slug}", "auth") == "feature-auth"

    def test_apply_naming_name_verbatim_placeholder(self):
        assert apply_naming("wip/{name}", "Keep As Is") == "wip/Keep As Is"

    def test_apply_naming_unknown_placeholder_left_literal(self):
        # Hand-edited config shouldn't crash on an unsupported placeholder.
        assert apply_naming("{bogus}-{slug}", "x") == "{bogus}-x"


# --- is_valid_branch_name ---

class TestValidBranchName:
    @pytest.mark.parametrize("name", ["fix-bug", "feature/auth", "jordan/fix-bug", "v2.0-rc1"])
    def test_valid(self, name):
        assert is_valid_branch_name(name) is True

    @pytest.mark.parametrize("name", [
        "",            # empty
        "Auth V2",     # spaces
        "a..b",        # double dot
        "-foo",        # leading dash (git would read it as a flag)
        "foo/",        # trailing slash
        "foo.lock",    # reserved suffix
        "foo~bar",     # tilde
        "foo:bar",     # colon
    ])
    def test_invalid(self, name):
        assert is_valid_branch_name(name) is False


# --- git_root ---

class TestGitRoot:
    def test_returns_repo_root_from_subdir(self, tmp_path):
        repo, _ = _make_repo(tmp_path)
        sub = repo / "packages" / "app"
        sub.mkdir(parents=True)
        assert git_root(sub) == repo.resolve()

    def test_none_outside_repo(self, tmp_path):
        assert git_root(tmp_path) is None


# --- git_common_dir (the "same repo" signal that survives linked worktrees) ---

class TestGitCommonDir:
    def test_none_outside_repo(self, tmp_path):
        assert git_common_dir(tmp_path) is None

    def test_matches_main_repo_git_dir(self, tmp_path):
        repo, _ = _make_repo(tmp_path)
        assert git_common_dir(repo) == (repo / ".git").resolve()

    def test_linked_worktree_shares_common_dir_with_main_repo(self, tmp_path):
        # A worktree's own git_root differs from the main repo's, but
        # git_common_dir must agree — that's the whole point of #715's
        # same-project check (a caller running from a worktree of project X
        # spawning into project X's main checkout is still "same project").
        repo, git = _make_repo(tmp_path)
        wt = tmp_path / "wt"
        git("worktree", "add", "-q", "-b", "side", str(wt))
        assert git_root(wt) != repo.resolve()
        assert git_common_dir(wt) == git_common_dir(repo) == (repo / ".git").resolve()

    def test_different_repos_have_different_common_dirs(self, tmp_path):
        repo_a, _ = _make_repo(tmp_path, name="repo-a")
        repo_b, _ = _make_repo(tmp_path, name="repo-b")
        assert git_common_dir(repo_a) != git_common_dir(repo_b)


# --- default_base_branch (repo-derived, no hardcoded main) ---

class TestDefaultBaseBranch:
    def test_falls_back_to_current_branch(self, tmp_path):
        # No origin/HEAD set → uses the repo's current branch.
        repo, _ = _make_repo(tmp_path, default_branch="develop")
        assert default_base_branch(repo) == "develop"

    def test_reads_origin_head(self, tmp_path):
        # A clone with origin/HEAD pointing at the remote's default branch.
        origin, ogit = _make_repo(tmp_path, name="origin", default_branch="trunk")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                       capture_output=True, text=True)
        # Switch the clone off the default so current-branch fallback would differ.
        subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "side"],
                       capture_output=True, text=True)
        assert default_base_branch(clone) == "trunk"


# --- ensure_worktree seeding (gitignored files like .env) ---

class TestWorktreeSeeding:
    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*a):
            return subprocess.run(["git", "-C", str(repo), *a],
                                  capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (repo / ".gitignore").write_text("secret.env\n")
        (repo / "README.md").write_text("hi\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        return repo, git

    def test_seeds_listed_file_and_keeps_it_ignored(self, tmp_path):
        repo, git = self._repo(tmp_path)
        (repo / "secret.env").write_text("API_KEY=abc\n")  # gitignored, in main only

        wt = tmp_path / "repo-worktrees" / "feature"
        assert ensure_worktree(repo, "feature", wt, copy_files=["secret.env"])

        # Seeded into the worktree...
        assert (wt / "secret.env").read_text() == "API_KEY=abc\n"
        # ...and still gitignored there, so it's never committed.
        status = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        assert "secret.env" not in status

    def test_only_listed_files_copied(self, tmp_path):
        repo, git = self._repo(tmp_path)
        (repo / "secret.env").write_text("k\n")
        (repo / "other.local").write_text("nope\n")  # untracked, not listed

        wt = tmp_path / "repo-worktrees" / "f2"
        ensure_worktree(repo, "f2", wt, copy_files=["secret.env"])
        assert (wt / "secret.env").exists()
        assert not (wt / "other.local").exists()

    def test_missing_seed_file_is_noop(self, tmp_path):
        repo, _ = self._repo(tmp_path)
        wt = tmp_path / "repo-worktrees" / "f3"
        # Listed file doesn't exist — creation still succeeds.
        assert ensure_worktree(repo, "f3", wt, copy_files=["secret.env"])
        assert not (wt / "secret.env").exists()
