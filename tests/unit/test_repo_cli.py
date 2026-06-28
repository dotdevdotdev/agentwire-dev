"""Tests for ``agentwire repo-info`` / ``agentwire branches`` (#495 Phase 2).

These commands hold the git logic the portal's ``api_check_path`` /
``api_check_branches`` endpoints used to embed inline. The tests pin the JSON
shape + local/remote parity with that old inline behavior. Real git is used for
the local cases (against a tmp repo); the remote path mocks ``core._run_remote``.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from agentwire import repo_cli


def _ns(**kw):
    base = dict(path="", machine="local", prefix="", json=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _capture(capsys):
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True)
    (repo / "f.txt").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, env={**env})
    subprocess.run(["git", "branch", "feature/a"], cwd=repo, capture_output=True)
    subprocess.run(["git", "branch", "feature/b"], cwd=repo, capture_output=True)
    subprocess.run(["git", "branch", "other"], cwd=repo, capture_output=True)
    return repo


class TestRepoInfoLocal:
    def test_git_repo(self, git_repo, capsys):
        assert repo_cli.cmd_repo_info(_ns(path=str(git_repo))) == 0
        out = _capture(capsys)
        assert out == {"exists": True, "is_git": True, "current_branch": "main"}

    def test_nonexistent(self, tmp_path, capsys):
        repo_cli.cmd_repo_info(_ns(path=str(tmp_path / "nope")))
        assert _capture(capsys) == {"exists": False, "is_git": False, "current_branch": None}

    def test_non_git_dir(self, tmp_path, capsys):
        repo_cli.cmd_repo_info(_ns(path=str(tmp_path)))
        assert _capture(capsys) == {"exists": True, "is_git": False, "current_branch": None}

    def test_empty_path(self, capsys):
        repo_cli.cmd_repo_info(_ns(path=""))
        assert _capture(capsys) == {"exists": False, "is_git": False, "current_branch": None}


class TestBranchesLocal:
    def test_all(self, git_repo, capsys):
        repo_cli.cmd_branches(_ns(path=str(git_repo), prefix=""))
        assert set(_capture(capsys)["existing"]) == {"main", "feature/a", "feature/b", "other"}

    def test_prefix(self, git_repo, capsys):
        repo_cli.cmd_branches(_ns(path=str(git_repo), prefix="feature/"))
        assert sorted(_capture(capsys)["existing"]) == ["feature/a", "feature/b"]

    def test_nonexistent(self, tmp_path, capsys):
        repo_cli.cmd_branches(_ns(path=str(tmp_path / "nope")))
        assert _capture(capsys) == {"existing": []}

    def test_empty_path(self, capsys):
        repo_cli.cmd_branches(_ns(path=""))
        assert _capture(capsys) == {"existing": []}


class TestRemote:
    def test_repo_info_remote(self, monkeypatch, capsys):
        calls = []

        def fake_remote(machine, cmd):
            calls.append(cmd)
            if "echo exists" in cmd:
                out = "exists\n"
            elif "echo git" in cmd:
                out = "git\n"
            else:  # rev-parse
                out = "develop\n"
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(repo_cli, "_run_remote", fake_remote)
        repo_cli.cmd_repo_info(_ns(path="/srv/app", machine="gpu"))
        assert _capture(capsys) == {"exists": True, "is_git": True, "current_branch": "develop"}

    def test_repo_info_remote_failure_is_empty(self, monkeypatch, capsys):
        # Non-zero remote exit → stdout dropped → reads as absent (parity with portal).
        def fake_remote(machine, cmd):
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="exists\n", stderr="boom")

        monkeypatch.setattr(repo_cli, "_run_remote", fake_remote)
        repo_cli.cmd_repo_info(_ns(path="/srv/app", machine="gpu"))
        assert _capture(capsys) == {"exists": False, "is_git": False, "current_branch": None}

    def test_branches_remote(self, monkeypatch, capsys):
        def fake_remote(machine, cmd):
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="feature/a\nfeature/b\n", stderr=""
            )

        monkeypatch.setattr(repo_cli, "_run_remote", fake_remote)
        repo_cli.cmd_branches(_ns(path="/srv/app", machine="gpu", prefix="feature/"))
        assert _capture(capsys) == {"existing": ["feature/a", "feature/b"]}
