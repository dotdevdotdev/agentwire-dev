"""Control-plane lockdown (#466).

The damage-control control plane — the kill-switch/rule files, the hook scripts,
the rule YAMLs, and the Claude Code hook registration — must be unwritable by the
policed agent, EVEN with the ``# allow:`` escape hatch and EVEN when the kill
switch is off. Only the user's host-side ``allowedPaths`` allowlist re-permits a
path. Loosening is always a human, host-side act.
"""

import os

import pytest

from agentwire.cli_safety import load_patterns
from agentwire.safety._core import (
    check_command,
    check_path,
    is_protected_control_plane,
    load_safety_config,
)


CONTROL_PLANE_FILES = [
    os.path.expanduser("~/.agentwire/damagecontrol.yml"),
    "/some/repo/.damagecontrol.yml",
    os.path.expanduser("~/.claude/settings.json"),
    os.path.expanduser("~/.agentwire/hooks/damage-control/bash-tool-damage-control.py"),
    os.path.expanduser("~/.claude/hooks/idle-handler.sh"),
    os.path.expanduser("~/.agentwire/damage-control/core.yaml"),
]


@pytest.fixture
def cfg():
    c = load_patterns()
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


# --------------------------------------------------------------------------
# is_protected_control_plane
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", CONTROL_PLANE_FILES)
def test_every_control_plane_file_is_protected(path):
    assert is_protected_control_plane(path) is True


def test_unrelated_file_is_not_protected():
    assert is_protected_control_plane(os.path.expanduser("~/projects/foo/main.py")) is False
    # ``config.yaml`` / ``.agentwire.yml`` are NOT control plane (no knobs there now)
    assert is_protected_control_plane(os.path.expanduser("~/.agentwire/config.yaml")) is False


# --------------------------------------------------------------------------
# Edit/Write hook (check_path)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", CONTROL_PLANE_FILES)
def test_edit_write_to_control_plane_blocked(cfg, path):
    blocked, reason = check_path(path, cfg)
    assert blocked is True
    assert "control-plane" in reason


@pytest.mark.parametrize("path", CONTROL_PLANE_FILES)
def test_edit_write_blocked_even_when_kill_switch_off(path):
    cfg = load_patterns()
    cfg["safety"] = {"enabled": False, "disabled_rules": []}
    blocked, _ = check_path(path, cfg)
    assert blocked is True  # absolute: kill switch does NOT re-open the control plane


def test_unregistering_hook_via_settings_blocked(cfg):
    blocked, _ = check_path(os.path.expanduser("~/.claude/settings.json"), cfg)
    assert blocked is True


# --------------------------------------------------------------------------
# Bash hook (check_command) — escape hatch must NOT override
# --------------------------------------------------------------------------

BASH_WRITES = [
    "echo 'enabled: false' > ~/.agentwire/damagecontrol.yml",
    "echo '{}' > ~/.claude/settings.json",
    "rm ~/.agentwire/damage-control/core.yaml",
    "sed -i 's/x/y/' ~/.agentwire/hooks/damage-control/bash-tool-damage-control.py",
    "echo 'enabled: false' > .damagecontrol.yml",
]


@pytest.mark.parametrize("command", BASH_WRITES)
def test_bash_write_to_control_plane_blocked(cfg, command):
    result = check_command(command, cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True


@pytest.mark.parametrize("command", BASH_WRITES)
def test_escape_hatch_cannot_override_control_plane(cfg, command):
    result = check_command(command + "  # allow: I really want to", cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True
    assert result.get("escape") is not True


@pytest.mark.parametrize("command", BASH_WRITES)
def test_kill_switch_cannot_reopen_control_plane(command):
    cfg = load_patterns()
    cfg["safety"] = {"enabled": False, "disabled_rules": []}
    result = check_command(command, cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True


def test_reading_control_plane_is_allowed(cfg):
    result = check_command("cat ~/.agentwire/damagecontrol.yml", cfg)
    assert result["decision"] == "allow"


# --------------------------------------------------------------------------
# Allowlist (the human opt-in) DOES re-permit
# --------------------------------------------------------------------------


def test_allowlisted_project_file_repermits_agent_edit():
    cfg = load_patterns()
    cfg["safety"] = {"enabled": True}
    project_file = "/repo/.damagecontrol.yml"
    cfg["allowedPaths"] = [{"path": project_file, "allow": "all"}]
    blocked, _ = check_path(project_file, cfg)
    assert blocked is False


def test_allowlisted_path_repermits_bash_write():
    cfg = load_patterns()
    cfg["safety"] = {"enabled": True}
    cfg["allowedPaths"] = [{"path": "/repo/.damagecontrol.yml", "allow": "all"}]
    result = check_command("echo 'enabled: true' > /repo/.damagecontrol.yml", cfg)
    assert result["decision"] != "block"


# --------------------------------------------------------------------------
# Loader reads the relocated knobs from damagecontrol.yml / .damagecontrol.yml
# --------------------------------------------------------------------------


def _write(p, text):
    p.write_text(text)


def test_loader_reads_knobs_from_global_file(tmp_path):
    g = tmp_path / "damagecontrol.yml"
    _write(g, "enabled: true\ndisabled_rules: [git.push]\nunattended_allow: [gh.pr-merge]\n")
    out = load_safety_config(global_config_path=g, cwd=str(tmp_path))
    assert out["enabled"] is True
    assert out["disabled_rules"] == ["git.push"]
    assert out["unattended_allow"] == ["gh.pr-merge"]


def test_missing_global_file_is_enabled_true(tmp_path):
    g = tmp_path / "does-not-exist.yml"
    out = load_safety_config(global_config_path=g, cwd=str(tmp_path))
    assert out["enabled"] is True


def test_project_file_can_tighten(tmp_path):
    # global enabled, project sets enabled: false → tightened off for that tree
    g = tmp_path / "damagecontrol.yml"
    _write(g, "enabled: true\n")
    proj = tmp_path / "repo"
    proj.mkdir()
    _write(proj / ".damagecontrol.yml", "enabled: false\n")
    out = load_safety_config(global_config_path=g, cwd=str(proj))
    assert out["enabled"] is False


def test_project_file_can_loosen(tmp_path):
    # global disabled (host choice), project sets enabled: true → re-enabled
    g = tmp_path / "damagecontrol.yml"
    _write(g, "enabled: false\n")
    proj = tmp_path / "repo"
    proj.mkdir()
    _write(proj / ".damagecontrol.yml", "enabled: true\n")
    out = load_safety_config(global_config_path=g, cwd=str(proj))
    assert out["enabled"] is True


def test_project_merges_rule_knobs(tmp_path):
    g = tmp_path / "damagecontrol.yml"
    _write(g, "disabled_rules: [git.push]\nunattended_allow: [a]\n")
    proj = tmp_path / "repo"
    proj.mkdir()
    _write(proj / ".damagecontrol.yml", "disabled_rules: [gh.pr-create]\nunattended_allow: [b]\n")
    out = load_safety_config(global_config_path=g, cwd=str(proj))
    assert set(out["disabled_rules"]) == {"git.push", "gh.pr-create"}
    assert set(out["unattended_allow"]) == {"a", "b"}


def test_host_side_edit_is_honored_by_loader(tmp_path):
    """A host-side edit (writing the file directly, not via the hooks) is read.

    The hooks block the AGENT from writing these files; the host/owner edits them
    freely and the loader picks the change up on next load.
    """
    g = tmp_path / "damagecontrol.yml"
    _write(g, "enabled: true\n")
    assert load_safety_config(global_config_path=g, cwd=str(tmp_path))["enabled"] is True
    _write(g, "enabled: false\n")  # owner flips the kill switch on the host
    assert load_safety_config(global_config_path=g, cwd=str(tmp_path))["enabled"] is False
