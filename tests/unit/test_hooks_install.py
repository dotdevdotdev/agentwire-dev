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
    _managed_file_state,
    _managed_hook_files,
    install_hooks,
    is_hook_registered,
    register_hook_in_settings,
    unregister_hook_from_settings,
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


class TestManagedFileState:
    def test_missing(self, source, target_dir):
        assert _managed_file_state(target_dir / "hook.sh", source) == "missing"

    def test_copy_matching_content_ok(self, source, target_dir):
        target = target_dir / "hook.sh"
        target.write_text(source.read_text())
        assert _managed_file_state(target, source) == "ok"

    def test_copy_drifted_content_stale(self, source, target_dir):
        target = target_dir / "hook.sh"
        target.write_text("#!/bin/bash\necho old\n")
        assert _managed_file_state(target, source) == "stale"

    def test_symlink_to_source_ok(self, source, target_dir):
        target = target_dir / "hook.sh"
        target.symlink_to(source)
        assert _managed_file_state(target, source) == "ok"

    def test_symlink_to_wrong_file_stale(self, source, target_dir, tmp_path):
        other = tmp_path / "other.sh"
        other.write_text("nope")
        target = target_dir / "hook.sh"
        target.symlink_to(other)
        assert _managed_file_state(target, source) == "stale"

    def test_dangling_symlink_stale(self, source, target_dir, tmp_path):
        target = target_dir / "hook.sh"
        target.symlink_to(tmp_path / "deleted.sh")
        assert _managed_file_state(target, source) == "stale"


class TestInstallManagedFile:
    def test_installs_symlink_by_default(self, source, target_dir):
        target = target_dir / "hook.sh"
        assert _install_managed_file(source, target) is True
        assert target.is_symlink() and target.resolve() == source.resolve()

    def test_installs_copy_when_requested(self, source, target_dir):
        target = target_dir / "hook.sh"
        assert _install_managed_file(source, target, copy=True) is True
        assert not target.is_symlink()
        assert target.read_text() == source.read_text()
        assert target.stat().st_mode & 0o111  # executable

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

    def test_current_file_untouched(self, source, target_dir):
        target = target_dir / "hook.sh"
        _install_managed_file(source, target, copy=True)
        assert _install_managed_file(source, target, copy=True) is False

    def test_stale_copy_replaced(self, source, target_dir):
        # The #238 failure mode: a drifted regular file was skipped forever.
        target = target_dir / "hook.sh"
        target.write_text("#!/bin/bash\necho ancient\n")
        assert _install_managed_file(source, target, copy=True) is True
        assert target.read_text() == source.read_text()

    def test_stale_symlink_relinked(self, source, target_dir, tmp_path):
        other = tmp_path / "other.sh"
        other.write_text("nope")
        target = target_dir / "hook.sh"
        target.symlink_to(other)
        assert _install_managed_file(source, target) is True
        assert target.resolve() == source.resolve()

    def test_force_reinstalls_current(self, source, target_dir):
        target = target_dir / "hook.sh"
        _install_managed_file(source, target, copy=True)
        assert _install_managed_file(source, target, force=True) is True
        assert target.is_symlink()  # copy converted to symlink

    def test_creates_target_dir(self, source, tmp_path):
        target = tmp_path / "deep" / "nested" / "hook.sh"
        assert _install_managed_file(source, target) is True
        assert target.exists()


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
        assert set(results.values()) == {"installed"}
        assert (home / ".claude" / "hooks" / "agentwire-permission.sh").exists()
        assert (home / ".claude" / "hooks" / "idle-handler.sh").exists()
        assert (home / ".agentwire" / "queue-processor.sh").exists()

        settings = json.loads((home / ".claude" / "settings.json").read_text())
        events = settings["hooks"]
        assert any(h["command"].endswith("agentwire-permission.sh")
                   for e in events["PermissionRequest"] for h in e["hooks"])
        assert any(h["command"].endswith("idle-handler.sh")
                   for e in events["Notification"] for h in e["hooks"])

    def test_second_run_all_current(self, env):
        install_hooks()
        assert set(install_hooks().values()) == {"current"}

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

    def test_registration_idempotent(self, env):
        home, _src = env
        install_hooks()
        install_hooks()
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        assert len(settings["hooks"]["Notification"]) == 1
        assert len(settings["hooks"]["PermissionRequest"]) == 1


class TestSettingsRegistration:
    @pytest.fixture(autouse=True)
    def fake_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        return home

    def test_register_and_query(self):
        assert is_hook_registered("Notification", "idle-handler.sh") is False
        assert register_hook_in_settings("Notification", "idle-handler.sh") is True
        assert is_hook_registered("Notification", "idle-handler.sh") is True
        # Different event for the same file is independent.
        assert is_hook_registered("PermissionRequest", "idle-handler.sh") is False

    def test_register_twice_is_noop(self):
        register_hook_in_settings("Notification", "idle-handler.sh")
        assert register_hook_in_settings("Notification", "idle-handler.sh") is False

    def test_unregister_removes_only_target_event(self):
        register_hook_in_settings("Notification", "idle-handler.sh")
        register_hook_in_settings("PermissionRequest", "agentwire-permission.sh")
        assert unregister_hook_from_settings("Notification", "idle-handler.sh") is True
        assert is_hook_registered("Notification", "idle-handler.sh") is False
        assert is_hook_registered("PermissionRequest", "agentwire-permission.sh") is True

    def test_unregister_missing_returns_false(self):
        assert unregister_hook_from_settings("Notification", "idle-handler.sh") is False

    def _settings(self, home):
        import json

        return json.loads((home / ".claude" / "settings.json").read_text())

    def test_outcome_logger_matches_exactly_the_damage_control_tools(self, fake_home):
        from agentwire.safety_commands import DAMAGE_CONTROL_MATCHERS

        for event in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            assert register_hook_in_settings(event, "outcome_logger.py") is True
            (entry,) = self._settings(fake_home)["hooks"][event]
            assert entry["matcher"].split("|") == list(DAMAGE_CONTROL_MATCHERS), event

    def test_an_older_catch_all_registration_is_narrowed_in_place(self, fake_home):
        import json

        path = fake_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hooks": {"PostToolUse": [{
            "matcher": ".*",
            "hooks": [{"type": "command", "command": "~/.claude/hooks/outcome_logger.py"}],
        }]}}))
        assert register_hook_in_settings("PostToolUse", "outcome_logger.py") is True
        (entry,) = self._settings(fake_home)["hooks"]["PostToolUse"]
        assert entry["matcher"] != ".*" and "Bash" in entry["matcher"]
        assert register_hook_in_settings("PostToolUse", "outcome_logger.py") is False

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


class TestPackagedHooksPresent:
    """The managed-files table must match what actually ships in the package."""

    def test_all_managed_files_exist_in_source(self):
        from agentwire.hooks_cli import get_hooks_source
        hooks_source = get_hooks_source()
        for name, *_ in _managed_hook_files():
            assert (hooks_source / name).exists(), f"{name} missing from agentwire/hooks/"


def test_every_managed_hook_has_doctor_metadata():
    """#1082: doctor prints from the managed list itself, so none can lack a label."""
    bad = [h.name for h in _managed_hook_files() if not (h.label and h.why)]
    assert not bad, bad
