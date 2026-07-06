"""Control-plane lockdown (#466).

The damage-control control plane — the kill-switch/rule files, the hook scripts,
the rule YAMLs, and the Claude Code hook registration — must be unwritable by the
policed agent, EVEN with the ``# allow:`` escape hatch and EVEN when the kill
switch is off. Only the user's host-side ``allowedPaths`` allowlist re-permits a
path. Loosening is always a human, host-side act.
"""

import os

import pytest

from agentwire.safety._core import (
    check_command,
    check_path,
    is_protected_control_plane,
    load_allowed_paths,
    load_safety_config,
)
from agentwire.safety_commands import load_patterns

CONTROL_PLANE_FILES = [
    os.path.expanduser("~/.agentwire/damagecontrol.yml"),
    "/some/repo/.damagecontrol.yml",
    os.path.expanduser("~/.claude/settings.json"),
    os.path.expanduser("~/.agentwire/hooks/damage-control/bash-tool-damage-control.py"),
    os.path.expanduser("~/.claude/hooks/idle-handler.sh"),
    os.path.expanduser("~/.agentwire/damage-control/core.yaml"),
    # Execution-plane configs whose strings agentwire runs via shell=True
    # (scheduler gate commands, service healthchecks, per-project task commands)
    # — these never traverse the Claude Code hook, so they are control plane too.
    os.path.expanduser("~/.agentwire/scheduler.yaml"),
    os.path.expanduser("~/.agentwire/config.yaml"),
    # Per-project task-execution config (#720) — NOT .agentwire.yml, which was
    # split out and is agent-writable again (see test_agentwire_yml_is_no_longer_protected).
    "/some/repo/.agentwire.tasks.yml",
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
    # A regular source file in a repo is not control plane even if the repo
    # carries an .agentwire.yml.
    assert is_protected_control_plane(os.path.expanduser("~/projects/foo/app/config.yaml")) is False


def test_agentwire_yml_is_no_longer_protected():
    """#720: .agentwire.yml was split out — it's pure declarative session config
    (type/roles/voice/parent/worktree) now, agent-writable again."""
    assert is_protected_control_plane("/some/repo/.agentwire.yml") is False


def test_agentwire_tasks_proposed_yml_is_not_protected():
    """The unprotected staging file an agent drafts to (#720 propose-and-promote)."""
    assert is_protected_control_plane("/some/repo/.agentwire.tasks.proposed.yml") is False


BASH_EXECUTION_PLANE_WRITES = [
    "echo 'x' > ~/.agentwire/scheduler.yaml",
    "echo 'x' > ~/.agentwire/config.yaml",
    "echo 'x' > .agentwire.tasks.yml",
    "sed -i 's/a/b/' ~/.agentwire/scheduler.yaml",
]

# #720: .agentwire.yml carries no execution vector anymore — writes to it must
# succeed (not be blocked as control plane), while the split-out task-exec
# file and staging draft behave as expected.
BASH_AGENTWIRE_YML_WRITES_ALLOWED = [
    "echo 'roles: [agentwire]' > .agentwire.yml",
    "echo 'roles: [agentwire]' >> /some/repo/.agentwire.yml",
]

BASH_PROPOSED_TASKS_WRITES_ALLOWED = [
    "echo 'tasks: {}' > .agentwire.tasks.proposed.yml",
]


@pytest.mark.parametrize("command", BASH_AGENTWIRE_YML_WRITES_ALLOWED)
def test_bash_write_to_agentwire_yml_not_blocked(cfg, command):
    result = check_command(command, cfg)
    assert result.get("protected") is not True
    assert result["decision"] != "block"


@pytest.mark.parametrize("command", BASH_PROPOSED_TASKS_WRITES_ALLOWED)
def test_bash_write_to_proposed_tasks_not_blocked(cfg, command):
    result = check_command(command, cfg)
    assert result.get("protected") is not True
    assert result["decision"] != "block"


def test_agentwire_tasks_promote_is_hard_blocked_for_agent(cfg):
    """The propose-and-promote CLI escape (#720): an agent must not be able to
    just run `agentwire tasks promote` itself from its Bash tool — that would
    write the protected file on the agent's behalf (confused-deputy)."""
    result = check_command("agentwire tasks promote", cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True


def test_agentwire_tasks_review_is_not_blocked(cfg):
    """`review` is read-only — no reason to block the agent from checking its
    own draft before asking a human to promote it."""
    result = check_command("agentwire tasks review", cfg)
    assert result["decision"] != "block"


# --------------------------------------------------------------------------
# #721 review: `agentwire tasks promote` gets the SAME escape-hatch- and
# kill-switch-EXEMPT tier as the protected-control-plane path check — it's a
# PROTECTED_COMMAND_PATTERNS entry (safety/_core.py), not an ordinary
# bashToolPatterns rule, specifically so `# allow:` and `enabled: false`
# can't reopen the confused-deputy escape the file protection exists to
# close. This is the "step 3 alone isn't enough" gap the review found: an
# ordinary bashToolPatterns block IS overridable by the escape hatch, which
# would have let `agentwire tasks promote --yes  # allow: x` straight through.
# --------------------------------------------------------------------------

PROMOTE_COMMANDS = [
    "agentwire tasks promote",
    "agentwire tasks promote --yes",
    "uv run agentwire tasks promote --yes",
    "python3 -m agentwire tasks promote --yes",
]


@pytest.mark.parametrize("command", PROMOTE_COMMANDS)
def test_promote_blocked_regardless_of_invocation_wrapper(cfg, command):
    result = check_command(command, cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True


@pytest.mark.parametrize("command", PROMOTE_COMMANDS)
def test_escape_hatch_cannot_override_promote_block(cfg, command):
    result = check_command(command + "  # allow: I really want to", cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True
    assert result.get("escape") is not True


@pytest.mark.parametrize("command", PROMOTE_COMMANDS)
def test_kill_switch_cannot_reopen_promote_block(command):
    cfg = load_patterns()
    cfg["safety"] = {"enabled": False, "disabled_rules": []}
    result = check_command(command, cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True


def test_promote_block_has_no_allowlist_override():
    """Unlike protected PATHS, a protected COMMAND has no allowlist escape
    valve at all — there's no legitimate reason for an agent to ever run
    `agentwire tasks promote`, so nothing should be able to re-permit it."""
    cfg = load_patterns()
    cfg["safety"] = {"enabled": True}
    cfg["allowedPaths"] = [{"path": "*", "allow": "all"}]
    result = check_command("agentwire tasks promote", cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True


PROMOTE_MENTIONS_IN_CONTENT = [
    'git commit -m "docs: mention agentwire tasks promote in the README"',
    'echo "run agentwire tasks promote when ready"',
    'gh issue comment 1 --body "see agentwire tasks promote for details"',
]


@pytest.mark.parametrize("command", PROMOTE_MENTIONS_IN_CONTENT)
def test_promote_block_does_not_false_match_quoted_content(cfg, command):
    """The command-prefix pattern must only match the command ITSELF, not a
    mere textual mention inside a commit message / echo string / PR comment
    (mirrors the #675 anchored-rule masking — this check uses
    masked_subcommands for exactly this reason)."""
    result = check_command(command, cfg)
    assert result["decision"] != "block"
    assert result.get("protected") is not True


@pytest.mark.parametrize("command", BASH_EXECUTION_PLANE_WRITES)
def test_bash_write_to_execution_plane_blocked(cfg, command):
    """Gate/healthcheck/task config files run via agentwire's own shell=True —
    the agent must not be able to write them (confused-deputy escape)."""
    result = check_command(command, cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True


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
    # #678 — absolute-path targets: basename globs (*.agentwire.tasks.yml) must
    # match through a directory prefix, not just the bare relative form.
    "echo x >> /some/repo/.agentwire.tasks.yml",
    "echo x > /some/repo/.agentwire.tasks.yml",
    'sed -i "s/a/b/" /some/repo/.agentwire.tasks.yml',
    "cp /tmp/x /some/repo/.agentwire.tasks.yml",
    "mv /tmp/x /some/repo/.agentwire.tasks.yml",
    'tee "/some/repo/.agentwire.tasks.yml" < /tmp/x',
    # #678 — interpreter programs are opaque; fail closed when an inline/
    # stdin/piped interpreter invocation mentions a protected path.
    "python3 -c 'open(\".agentwire.tasks.yml\", \"w\").write(\"x\")'",
    "python3 -c 'open(\"/some/repo/.agentwire.tasks.yml\", \"a\").write(\"x\")'",
    "perl -e 'open(F, \">\", \".agentwire.tasks.yml\")'",
    "python3 - <<'EOF'\nopen('/some/repo/.agentwire.tasks.yml', 'w').write('x')\nEOF",
    "echo 'open(\".agentwire.tasks.yml\",\"w\")' | python3",
]

# Write-shaped-looking but innocent: mentioning the filename in quoted
# CONTENT (an issue body, a commit message) must NOT block (#675 posture),
# and plain reads stay allowed.
BASH_INNOCENT = [
    'gh issue create --title "bug" --body "edit your .agentwire.tasks.yml to fix"',
    'git commit -m "docs: mention .agentwire.tasks.yml"',
    "cat .agentwire.tasks.yml",
    "grep roles /some/repo/.agentwire.tasks.yml",
    "python3 -m pytest tests/unit",
]


@pytest.mark.parametrize("command", BASH_INNOCENT)
def test_innocent_mention_or_read_not_blocked(cfg, command):
    result = check_command(command, cfg)
    # Some of these hit ordinary ask-tier rules (gh/git) — that's fine; the
    # assertion is that the PROTECTED control-plane gate doesn't fire.
    assert result["decision"] != "block", command
    assert result.get("protected") is not True, command


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


# --------------------------------------------------------------------------
# The per-project allowlist lives in the PROTECTED .damagecontrol.yml, NOT the
# agent-writable .agentwire.yml (#467 — the residual one-step bypass).
#
# These read the project file from DISK via _find_project_config()'s PWD walk,
# so they exercise the real source of the allowlist — injecting a merged dict
# would hide exactly the bug being closed.
# --------------------------------------------------------------------------

PROTECTED_TARGET = os.path.expanduser("~/.agentwire/damagecontrol.yml")  # a protected path to (try to) re-permit


def _base_cfg():
    c = load_patterns()
    c["safety"] = {"enabled": True}
    c["allowedPaths"] = []  # no host-side global allowlist for these
    return c


def test_agentwire_yml_allowlist_does_NOT_repermit_protected(tmp_path, monkeypatch):  # noqa: N802  # caps emphasize the negative assertion
    """BUG REPRODUCER: .agentwire.yml safety.allowed_paths must NOT re-permit a
    protected path — otherwise an agent edits .agentwire.yml to free itself."""
    (tmp_path / ".agentwire.yml").write_text(
        "type: claude-bypass\n"
        "safety:\n"
        "  allowed_paths:\n"
        f"    - path: {PROTECTED_TARGET}\n"
        "      allow: all\n"
    )
    monkeypatch.setenv("PWD", str(tmp_path))
    cfg = _base_cfg()

    blocked, _ = check_path(PROTECTED_TARGET, cfg)
    assert blocked is True
    result = check_command(f"echo 'enabled: false' > {PROTECTED_TARGET}", cfg)
    assert result["decision"] == "block"
    assert result.get("protected") is True


def test_damagecontrol_yml_allowlist_DOES_repermit_protected(tmp_path, monkeypatch):  # noqa: N802  # caps emphasize the positive assertion
    """Host-side opt-in works: an allowed_paths entry in the PROTECTED
    .damagecontrol.yml re-permits the agent to edit that path."""
    (tmp_path / ".damagecontrol.yml").write_text(
        "enabled: true\n"
        "allowed_paths:\n"
        f"  - path: {PROTECTED_TARGET}\n"
        "    allow: all\n"
    )
    monkeypatch.setenv("PWD", str(tmp_path))
    cfg = _base_cfg()

    blocked, _ = check_path(PROTECTED_TARGET, cfg)
    assert blocked is False
    result = check_command(f"echo 'enabled: false' > {PROTECTED_TARGET}", cfg)
    assert result["decision"] != "block"


def test_global_host_allowlist_still_repermits(tmp_path, monkeypatch):
    """The host-owned global allowedPaths (from the protected rule YAMLs) still
    overrides, as before."""
    monkeypatch.setenv("PWD", str(tmp_path))  # no project file present
    cfg = _base_cfg()
    cfg["allowedPaths"] = [{"path": PROTECTED_TARGET, "allow": "all"}]
    blocked, _ = check_path(PROTECTED_TARGET, cfg)
    assert blocked is False


def test_load_allowed_paths_sources_from_damagecontrol_not_agentwire(tmp_path, monkeypatch):
    """The per-project allowlist comes from .damagecontrol.yml; an .agentwire.yml
    safety block contributes nothing."""
    (tmp_path / ".agentwire.yml").write_text(
        "type: claude-bypass\n"
        "safety:\n"
        "  allowed_paths:\n"
        "    - path: /from/agentwire\n"
        "      allow: all\n"
    )
    (tmp_path / ".damagecontrol.yml").write_text(
        "allowed_paths:\n"
        "  - path: /from/damagecontrol\n"
        "    allow: all\n"
    )
    monkeypatch.setenv("PWD", str(tmp_path))
    paths = [e["path"] for e in load_allowed_paths({"allowedPaths": []})]
    assert "/from/damagecontrol" in paths
    assert "/from/agentwire" not in paths


def test_agentwire_yml_alone_contributes_no_allowlist(tmp_path, monkeypatch):
    (tmp_path / ".agentwire.yml").write_text(
        "type: claude-bypass\n"
        "safety:\n"
        "  allowed_paths:\n"
        "    - path: /from/agentwire\n"
        "      allow: all\n"
    )
    monkeypatch.setenv("PWD", str(tmp_path))
    paths = [e["path"] for e in load_allowed_paths({"allowedPaths": []})]
    assert paths == []


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
