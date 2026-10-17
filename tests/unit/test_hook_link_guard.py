"""The machine-global hook symlinks can only point into the canonical install (#1118).

Twice in one day ``~/.claude/hooks/*`` were found aimed at a worker worktree.
The writer was a unit test: ``CLAUDE_HOOKS_DIR`` was frozen to the real
``~/.claude/hooks`` at import, so ``install_hooks(allow_foreign=True)`` — run
under a patched ``Path.home`` — unlinked and relinked the REAL symlinks at the
checkout's own hooks. These pin the layers that now stand in its way.
"""

from pathlib import Path

import pytest

from agentwire import doctor_cli, hooks_cli
from agentwire.safety import provenance as prov


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake home with a canonical install and a worktree checkout beside it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)

    canonical = tmp_path / "tool" / "agentwire"
    (canonical / "hooks").mkdir(parents=True)
    worktree = tmp_path / "wt" / "agentwire"
    (worktree / "hooks").mkdir(parents=True)
    (tmp_path / "wt" / ".git").write_text("gitdir: /elsewhere\n")  # a linked worktree
    for pkg in (canonical, worktree):
        (pkg / "hooks" / "idle-handler.sh").write_text("#!/bin/bash\n")

    monkeypatch.setattr(prov, "canonical_package_dir", lambda: canonical.resolve())
    monkeypatch.setattr(prov, "running_package_dir", lambda: canonical.resolve())
    monkeypatch.setattr(hooks_cli, "get_hooks_source", lambda: canonical / "hooks")
    (home / ".claude" / "hooks").mkdir(parents=True)
    return home, canonical, worktree


class TestLinkTargetRefusal:
    def test_accepts_canonical_and_refuses_everything_else(self, machine, tmp_path, monkeypatch):
        _home, canonical, worktree = machine
        stray = tmp_path / "scratch" / "hook.sh"
        stray.parent.mkdir()
        stray.write_text("x")

        assert prov.link_target_refusal(canonical / "hooks" / "idle-handler.sh") is None
        wrong = {
            "worktree": worktree / "hooks" / "idle-handler.sh",
            "temp checkout": stray,
        }
        bad = {k: str(v) for k, v in wrong.items() if prov.link_target_refusal(v) is None}
        assert not bad, f"accepted as a link target: {bad}"

        # Bootstrap (no canonical install): a plain dir is fine, a worktree is not.
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: None)
        assert prov.link_target_refusal(stray) is None
        assert prov.link_target_refusal(worktree / "hooks" / "idle-handler.sh") is not None

    def test_refusal_names_the_source_the_state_and_the_fix(self, machine):
        _home, _canonical, worktree = machine
        text = "\n".join(prov.link_target_refusal(worktree / "hooks" / "idle-handler.sh"))
        assert str(worktree.resolve()) in text
        assert "outside the canonical install" in text
        assert "agentwire hooks install" in text


class TestInstallRefusesAndPreservesTheSlot:
    def test_install_from_a_worktree_source_refuses_and_keeps_the_good_link(self, machine):
        home, canonical, worktree = machine
        target = home / ".claude" / "hooks" / "idle-handler.sh"
        target.symlink_to(canonical / "hooks" / "idle-handler.sh")

        with pytest.raises(hooks_cli.LinkRefusedError, match="outside the canonical install"):
            hooks_cli._install_managed_file(
                worktree / "hooks" / "idle-handler.sh", target, force=True
            )
        assert target.resolve() == (canonical / "hooks" / "idle-handler.sh").resolve()

    def test_explicit_override_is_the_only_way_through(self, machine):
        home, _canonical, worktree = machine
        target = home / ".claude" / "hooks" / "idle-handler.sh"
        hooks_cli._install_managed_file(
            worktree / "hooks" / "idle-handler.sh", target, allow_foreign=True
        )
        assert target.resolve() == (worktree / "hooks" / "idle-handler.sh").resolve()

    def test_hooks_install_from_a_worktree_package_exits_nonzero_with_the_fix(
        self, machine, monkeypatch, capsys
    ):
        home, _canonical, worktree = machine
        monkeypatch.setattr(prov, "running_package_dir", lambda: worktree.resolve())

        class Args:
            force = False
            copy = False
            allow_foreign_source = False

        assert hooks_cli.cmd_hooks_install(Args()) == 1
        out = capsys.readouterr().out
        assert "REFUSED" in out and "hooks install" in out
        assert not list((home / ".claude" / "hooks").iterdir()), "a refusal must write nothing"


class TestHooksDirFollowsHome:
    def test_dirs_are_resolved_under_the_current_home_not_frozen_at_import(self, machine):
        home, _c, _w = machine
        assert hooks_cli.claude_hooks_dir() == home / ".claude" / "hooks"
        assert hooks_cli.claude_commands_dir() == home / ".claude" / "commands"
        assert hooks_cli.claude_skills_dir() == home / ".claude" / "skills"


class TestDoctorFlagsBadLinks:
    def _render(self, capsys):
        issues = doctor_cli._render_hooks_section()
        return issues, capsys.readouterr().out

    def test_healthy_link_into_the_canonical_install_is_ok(self, machine, capsys):
        home, canonical, _w = machine
        (home / ".claude" / "hooks" / "idle-handler.sh").symlink_to(
            canonical / "hooks" / "idle-handler.sh"
        )
        _issues, out = self._render(capsys)
        assert "BAD LINK" not in out

    def test_link_into_a_worktree_or_dangling_is_flagged_and_named(
        self, machine, capsys, tmp_path
    ):
        home, _canonical, worktree = machine
        link = home / ".claude" / "hooks" / "idle-handler.sh"

        link.symlink_to(worktree / "hooks" / "idle-handler.sh")
        issues, out = self._render(capsys)
        assert issues >= 1
        assert "[!!] Idle notification hook: BAD LINK" in out
        assert str((worktree / "hooks" / "idle-handler.sh").resolve()) in out

        link.unlink()
        gone = tmp_path / "gone" / "idle-handler.sh"
        link.symlink_to(gone)
        issues, out = self._render(capsys)
        assert issues >= 1 and "dangling" in out and str(gone) in out
