"""Tests for the `agentwire rebuild` git-drift guard (#462).

`rebuild` is otherwise git-blind: it reinstalls whatever is checked out, so a
never-pulled local main silently ships stale code. The guard fetches origin and
refuses (unless --force) when the checkout is behind origin/main.
"""

import subprocess
import types
from pathlib import Path

import pytest

from agentwire.core import _git_behind_origin
from agentwire.system_cli import cmd_rebuild, cmd_uninstall


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env={**env},
    ).stdout.strip()


@pytest.fixture
def behind_checkout(tmp_path):
    """A clone whose local main is 2 commits behind its origin/main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "f.txt").write_text("0\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "c0")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))

    # Advance origin by 2 commits the clone hasn't pulled.
    for i in (1, 2):
        (origin / "f.txt").write_text(f"{i}\n")
        _git(origin, "commit", "-qam", f"c{i}")
    return clone, origin


class TestGitBehindOrigin:
    def test_up_to_date_returns_zero(self, tmp_path):
        origin = tmp_path / "origin"
        origin.mkdir()
        _git(origin, "init", "-q", "-b", "main")
        (origin / "f.txt").write_text("0\n")
        _git(origin, "add", ".")
        _git(origin, "commit", "-qm", "c0")
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", str(origin), str(clone))

        behind, err = _git_behind_origin(clone)
        assert err is None
        assert behind == 0

    def test_behind_returns_count(self, behind_checkout):
        clone, _ = behind_checkout
        behind, err = _git_behind_origin(clone)
        assert err is None
        assert behind == 2

    def test_not_a_repo_returns_error(self, tmp_path):
        behind, err = _git_behind_origin(tmp_path)
        assert behind is None
        assert "not a git checkout" in err

    def test_no_fetch_uses_local_remote_ref(self, behind_checkout):
        clone, _ = behind_checkout
        behind, err = _git_behind_origin(clone, do_fetch=False)
        # Without fetch the clone's stale origin/main ref shows up to date.
        assert err is None
        assert behind == 0


def _args(**kw):
    return types.SimpleNamespace(**kw)


class TestRebuildGuard:
    @pytest.fixture(autouse=True)
    def _patch_root(self, behind_checkout, monkeypatch):
        clone, _ = behind_checkout
        # Make a pyproject so cmd_rebuild treats the clone as the source root.
        (clone / "pyproject.toml").write_text("[project]\nname='x'\n")
        import agentwire.system_cli as sys_mod
        # __file__.parent.parent must resolve to the clone.
        fake_file = clone / "agentwire" / "system_cli.py"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_text("")
        monkeypatch.setattr(sys_mod, "__file__", str(fake_file))
        self.clone = clone

    def test_refuses_when_behind(self, monkeypatch, capsys):
        # Let the real git fetch/rev-list run (so behind is computed), but fail
        # loudly if rebuild ever reaches the uv install path.
        real_run = subprocess.run
        reached_install = {"hit": False}

        def fake_run(cmd, *a, **k):
            if cmd[:2] in (["git", "fetch"], ["git", "rev-list"]):
                return real_run(cmd, *a, **k)
            reached_install["hit"] = True
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc = cmd_rebuild(_args(force=False))
        out = capsys.readouterr().out
        assert rc == 1
        assert "behind origin/main" in out
        assert reached_install["hit"] is False

    def test_force_overrides(self, monkeypatch, capsys):
        # With --force the guard prints a warning and proceeds past it. Stub the
        # uv subprocess calls so the real install isn't attempted.
        real_run = subprocess.run

        def fake_run(cmd, *a, **k):
            if cmd[:2] == ["git", "fetch"] or cmd[:2] == ["git", "rev-list"]:
                return real_run(cmd, *a, **k)
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc = cmd_rebuild(_args(force=True))
        out = capsys.readouterr().out
        assert "--force given" in out
        assert rc == 0


@pytest.fixture
def up_to_date_checkout(tmp_path):
    """A clone that is level with its origin/main, laid out as a source root."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "f.txt").write_text("0\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "c0")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "pyproject.toml").write_text("[project]\nname='x'\n")
    fake_file = clone / "agentwire" / "system_cli.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text("")
    return clone, fake_file


class TestRebuildSafety:
    """#643: rebuild must never nuke the shared uv cache or uninstall-before-install."""

    @pytest.fixture(autouse=True)
    def _patch_root(self, up_to_date_checkout, monkeypatch):
        clone, fake_file = up_to_date_checkout
        import agentwire.system_cli as sys_mod
        monkeypatch.setattr(sys_mod, "__file__", str(fake_file))
        self.clone = clone

    def _capture_uv(self, monkeypatch, install_rc=0):
        real_run = subprocess.run
        calls = []

        def fake_run(cmd, *a, **k):
            if cmd[0] == "git":
                return real_run(cmd, *a, **k)
            calls.append(cmd)
            rc = install_rc if cmd[:3] == ["uv", "tool", "install"] else 0
            return subprocess.CompletedProcess(cmd, rc, "", "boom" if rc else "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_targeted_cache_clean_and_no_rmtree(self, monkeypatch):
        calls = self._capture_uv(monkeypatch)
        rmtree_calls = []
        monkeypatch.setattr("shutil.rmtree", lambda *a, **k: rmtree_calls.append(a))
        rc = cmd_rebuild(_args(force=False))
        assert rc == 0
        assert rmtree_calls == []
        assert ["uv", "cache", "clean", "agentwire-dev"] in calls

    def test_no_uninstall_step_atomic_replace(self, monkeypatch):
        calls = self._capture_uv(monkeypatch)
        rc = cmd_rebuild(_args(force=False))
        assert rc == 0
        assert not any(cmd[:3] == ["uv", "tool", "uninstall"] for cmd in calls)
        assert ["uv", "tool", "install", ".", "--force", "--reinstall"] in calls

    def test_failed_install_never_uninstalls(self, monkeypatch, capsys):
        calls = self._capture_uv(monkeypatch, install_rc=1)
        rc = cmd_rebuild(_args(force=False))
        err = capsys.readouterr().err
        assert rc == 1
        assert not any(cmd[:3] == ["uv", "tool", "uninstall"] for cmd in calls)
        assert "left untouched" in err

    def test_missing_checkout_fails_before_any_uv_call(self, monkeypatch, capsys, tmp_path):
        import agentwire.system_cli as sys_mod
        fake_file = tmp_path / "nocheckout" / "agentwire" / "system_cli.py"
        fake_file.parent.mkdir(parents=True)
        fake_file.write_text("")
        monkeypatch.setattr(sys_mod, "__file__", str(fake_file))
        monkeypatch.setattr(sys_mod, "get_source_dir", lambda: tmp_path / "also-missing")
        calls = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, *a, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
        )
        rc = cmd_rebuild(_args(force=False))
        err = capsys.readouterr().err
        assert rc == 1
        assert "pyproject.toml missing" in err
        assert not any(cmd[0] == "uv" for cmd in calls)


class TestUninstallSafety:
    def test_targeted_cache_clean_and_no_rmtree(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, *a, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
        )
        rmtree_calls = []
        monkeypatch.setattr("shutil.rmtree", lambda *a, **k: rmtree_calls.append(a))
        rc = cmd_uninstall(_args())
        assert rc == 0
        assert rmtree_calls == []
        assert ["uv", "cache", "clean", "agentwire-dev"] in calls
        assert ["uv", "tool", "uninstall", "agentwire-dev"] in calls
