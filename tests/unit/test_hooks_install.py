"""Tests for `agentwire hooks install` — managed hook files and drift handling.

Issue #238: `hooks install` previously only deployed the permission hook, so
installed copies of idle-handler.sh and queue-processor.sh silently drifted
stale. Now all agentwire-owned files are installed/refreshed whenever they
differ from the packaged source, and doctor/status report drift.
"""

import json
from pathlib import Path

import pytest

from agentwire.hooks_cli import (
    _install_managed_file,
    _managed_hook_files,
    install_hooks,
    register_hook_in_settings,
)


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src" / "hook.sh"
    src.parent.mkdir()
    src.write_text("#!/bin/bash\necho current\n")
    return src


@pytest.fixture
def target_dir(tmp_path):
    d = tmp_path / "installed"
    d.mkdir()
    return d




class TestInstallManagedFile:


    def test_symlink_install_never_chmods_the_source(self, source, target_dir):
        """#947: chmod follows symlinks, so a chmod aimed at the installed
        link lands on the SOURCE — which, in a dev checkout, is a tracked
        file. The suite itself was the reproducer: every run flipped
        ``agentwire/hooks/queue-processor.sh`` to 755 in every dev's tree.
        The symlink path must leave the source's mode alone entirely."""
        import os

        os.chmod(source, 0o644)
        target = target_dir / "hook.sh"
        assert _install_managed_file(source, target) is True
        assert target.is_symlink()
        assert (source.stat().st_mode & 0o777) == 0o644







class TestInstallHooks:
    """End-to-end: fake packaged source + fake home, all three files deployed."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        from agentwire import hooks_cli as main_mod

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        # Machine-global installs are refused from a non-canonical package
        # (#936) and a symlink may only target the canonical install (#1118).
        # Make a fake package AND pin it as both the running and the canonical
        # one, so these measure the install and not the guard, and behave
        # identically in a worktree and in CI's plain clone.
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        from agentwire.safety import provenance as _prov
        pkg = tmp_path / "pkg"
        monkeypatch.setattr(_prov, "canonical_package_dir", lambda: pkg.resolve())
        monkeypatch.setattr(_prov, "running_package_dir", lambda: pkg.resolve())

        hooks_src = pkg / "hooks"
        hooks_src.mkdir(parents=True)
        for name, *_ in _managed_hook_files():
            (hooks_src / name).write_text(f"#!/bin/bash\n# {name}\n")
        monkeypatch.setattr(main_mod, "get_hooks_source", lambda: hooks_src)
        return home, hooks_src

    def test_fresh_install_deploys_all_and_registers(self, env):
        home, _src = env
        results = install_hooks()
        dc = results.pop("damage-control")
        assert set(results.values()) == {"installed"}
        assert dc == "updated"  # the package hooks were proven and registered (#1209 M4)
        assert install_hooks()["damage-control"] == "current"
        assert (home / ".claude" / "hooks" / "agentwire-permission.sh").exists()
        assert (home / ".claude" / "hooks" / "idle-handler.sh").exists()
        assert (home / ".agentwire" / "queue-processor.sh").exists()

        settings = json.loads((home / ".claude" / "settings.json").read_text())
        events = settings["hooks"]
        assert any(h["command"].endswith("agentwire-permission.sh")
                   for e in events["PermissionRequest"] for h in e["hooks"])
        assert any(h["command"].endswith("idle-handler.sh")
                   for e in events["Notification"] for h in e["hooks"])


    def test_stale_installed_copy_refreshed(self, env):
        # The exact #238 scenario: an old regular-file copy must be replaced.
        home, _src = env
        install_hooks()
        stale = home / ".claude" / "hooks" / "idle-handler.sh"
        stale.unlink()
        stale.write_text("#!/bin/bash\n# ancient pre-loop-mode hook\n")
        results = install_hooks()
        assert results["idle-handler.sh"] == "updated"
        assert "ancient" not in stale.resolve().read_text()



class TestSettingsRegistration:
    @pytest.fixture(autouse=True)
    def fake_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        return home





    def _settings(self, home):
        import json

        return json.loads((home / ".claude" / "settings.json").read_text())



    def test_a_hand_set_matcher_on_a_default_hook_is_left_alone(self, fake_home):
        import json

        path = fake_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hooks": {"Notification": [{
            "matcher": "idle_prompt",
            "hooks": [{"type": "command", "command": "~/.claude/hooks/idle-handler.sh"}],
        }]}}))
        assert register_hook_in_settings("Notification", "idle-handler.sh") is False
        assert self._settings(fake_home)["hooks"]["Notification"][0]["matcher"] == "idle_prompt"






def test_tracked_hook_scripts_stay_executable():
    """Hooks are symlinked into ~/.claude/hooks and exec'd directly; a lost
    exec bit (e.g. a tool rewriting the file) silently stops them running."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        ["git", "ls-files", "-s", "agentwire/hooks"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout
    bad = [
        line.split("\t", 1)[1] for line in out.splitlines()
        if line.split("\t", 1)[1].endswith(".sh") and not line.startswith("100755")
    ]
    assert not bad, bad
