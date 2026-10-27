"""Tests for the `agentwire rebuild` git-drift guard (#462).

`rebuild` is otherwise git-blind: it reinstalls whatever is checked out, so a
never-pulled local main silently ships stale code. The guard fetches origin and
refuses (unless --force) when the checkout is behind origin/main.
"""

import subprocess
import types
from pathlib import Path

import pytest

from agentwire.system_cli import cmd_rebuild


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



    def test_failed_install_never_uninstalls(self, monkeypatch, capsys):
        calls = self._capture_uv(monkeypatch, install_rc=1)
        rc = cmd_rebuild(_args(force=False))
        err = capsys.readouterr().err
        assert rc == 1
        assert not any(cmd[:3] == ["uv", "tool", "uninstall"] for cmd in calls)
        assert "left untouched" in err



