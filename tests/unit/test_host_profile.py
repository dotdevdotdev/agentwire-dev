"""The profile seam (#1209 M3): what a host adds, what it can never undo, and how sources merge."""

import ast
import contextlib
import textwrap
from pathlib import Path

import pytest

from agentwire.safety import _core
from agentwire.safety.profile import GENERIC, GroupSpec, McpCommandTemplate

REPO = Path(__file__).resolve().parent.parent.parent
HOOKS_DIR = REPO / "agentwire" / "hooks" / "damage-control"


@contextlib.contextmanager
def installed(profile):
    previous = _core.PROFILE
    _core.install_profile(profile)
    try:
        yield
    finally:
        _core.install_profile(previous)


def host(**fields):
    return GENERIC._replace(name="test-host", **fields)


def test_the_generic_profile_names_no_host_and_grants_nothing():
    with installed(GENERIC):
        paths = _core.PROTECTED_CONTROL_PLANE_PATHS
        assert not [p for p in paths if "agentwire" in p.lower()], paths
        assert (_core.DEFAULT_UNATTENDED_ALLOW, _core.PROTECTED_COMMAND_PATTERNS) == ([], [])
        assert "agentwire" not in _core._BENIGN_PATH_RE.pattern + "".join(_core._CONTROL_PLANE_WORDS)


def test_a_profile_only_adds_to_the_kernel_never_removes_or_reorders_it():
    kernel = list(_core._KERNEL_PROTECTED_PATHS)
    with installed(host(protected_paths=("~/.host/*",), control_plane_words=(".host",))):
        paths = _core.PROTECTED_CONTROL_PLANE_PATHS
        assert paths[1:1 + len(kernel)] == kernel  # [0] is the profile's own policy file
        assert paths[-1] == "~/.host/*"
        assert ".host" in _core._CONTROL_PLANE_WORDS and "damagecontrol" in _core._CONTROL_PLANE_WORDS
    # ...and the kernel is enforced with no profile content at all.
    with installed(GENERIC):
        assert _core.is_protected_control_plane("/work/.damagecontrol.yml")
        assert _core.is_protected_control_plane(str(Path.home() / ".claude" / "settings.json"))


def test_each_profile_field_reaches_the_behaviour_it_names(monkeypatch):
    profile = host(
        config_dir="~/.host",
        unattended_env="HOST_UNATTENDED",
        unattended_allow_env="HOST_ALLOW",
        default_unattended_allow=("git.commit",),
        protected_commands=((r"\bhost-admin\b", "host-admin is host-only"),),
        protected_paths=("~/.host/secret.yml",),
    )
    monkeypatch.setenv("HOST_UNATTENDED", "1")
    monkeypatch.setenv("HOST_ALLOW", "git.push, gh.pr-create")
    monkeypatch.setenv("DAMAGE_CONTROL_UNATTENDED", "")
    with installed(profile):
        assert _core.is_unattended()
        assert _core.policy_file_path() == Path.home() / ".host" / "damagecontrol.yml"
        assert _core.is_protected_control_plane(str(Path.home() / ".host" / "damagecontrol.yml"))
        assert _core.is_protected_control_plane(str(Path.home() / ".host" / "secret.yml"))
        assert _core.check_protected_bash_command("host-admin now") == (True, "host-admin is host-only")
        assert _core.check_protected_bash_command('git commit -m "x host-admin y"') == (False, "")
        grants = _core.resolve_unattended_grants({"safety": {}})
        assert sorted(grants) == ["gh.pr-create", "git.commit", "git.push"]
    with installed(GENERIC):
        assert not _core.is_unattended()  # the host's env var means nothing to the generic engine


def test_a_profile_can_relocate_overrides_but_not_the_policy_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOST_HOME", str(tmp_path / "elsewhere"))
    with installed(host(config_dir="~/.host", config_dir_env="HOST_HOME")):
        assert _core.config_override_dir() == tmp_path / "elsewhere"
        assert _core.policy_file_path() == Path.home() / ".host" / "damagecontrol.yml"


def write_rules(directory, name, body):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(textwrap.dedent(body))


def rule(rid, pattern="zzz", ask="false"):
    return f"""
        bashToolPatterns:
          - id: {rid}
            pattern: '{pattern}'
            reason: r
            ask: {ask}
    """


def test_sources_merge_additively_in_precedence_order(tmp_path):
    write_rules(tmp_path / "generic", "a.yaml", rule("a.one"))
    write_rules(tmp_path / "layer", "b.yaml", rule("b.one"))
    write_rules(tmp_path / "overlay", "c.yaml", rule("c.one"))
    cfg = _core.load_config([tmp_path / "generic", tmp_path / "layer", tmp_path / "overlay"])
    assert [p["id"] for p in cfg["bashToolPatterns"]] == ["a.one", "b.one", "c.one"]
    assert "_duplicate_rule_ids" not in cfg


def test_a_rule_id_repeated_across_sources_is_a_load_error_and_both_rules_stay_live(tmp_path):
    write_rules(tmp_path / "generic", "a.yaml", rule("x.same", "first"))
    write_rules(tmp_path / "layer", "b.yaml", rule("x.same", "second"))
    cfg = _core.load_config([tmp_path / "generic", tmp_path / "layer"])
    assert cfg["_duplicate_rule_ids"] == ["x.same"]
    assert [p["pattern"] for p in cfg["bashToolPatterns"]] == ["first", "second"]


def test_an_extension_source_cannot_loosen_the_control_plane_but_the_first_source_is_unchanged(tmp_path):
    allow = """
        allowedPaths:
          - path: ~/.claude/settings.json
            allow: all
          - path: /tmp/*
            allow: all
    """
    write_rules(tmp_path / "generic", "a.yaml", allow)
    write_rules(tmp_path / "layer", "b.yaml", allow)
    cfg = _core.load_config([tmp_path / "generic", tmp_path / "layer"])
    kept = [e["path"] for e in cfg["allowedPaths"]]
    # The generic (first) source keeps both; the extension keeps only the harmless one.
    assert kept == ["~/.claude/settings.json", "/tmp/*", "/tmp/*"]
    assert [f["file"] for f in cfg["_parse_failures"]] == [str(tmp_path / "layer" / "b.yaml")]
    assert "re-permit the protected control plane" in cfg["_parse_failures"][0]["error"]


def test_a_layer_directory_is_found_relative_to_the_bundle(tmp_path):
    with installed(host(extra_rules_dirs=("host-rules",))):
        assert _core.bundled_rule_sources(tmp_path) == [tmp_path / "rules", tmp_path / "host-rules"]
        assert _core.resolve_rule_sources(tmp_path)[-1] == tmp_path / "host-rules"


def test_a_host_override_dir_replaces_the_bundle_wholesale(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_HOME", str(tmp_path / "home"))
    write_rules(tmp_path / "home" / "damage-control", "mine.yaml", rule("mine.one"))
    with installed(host(config_dir_env="HOST_HOME", extra_rules_dirs=("host-rules",))):
        assert _core.resolve_rule_sources(tmp_path / "bundle") == [tmp_path / "home" / "damage-control"]
        bundled = tmp_path / "bundle" / "tooldefs"
        assert _core.resolve_tooldef_sources(bundled) == []  # absent => no source, not a crash
        bundled.mkdir(parents=True)
        assert _core.resolve_tooldef_sources(bundled) == [bundled]


def test_mcp_command_templates_synthesize_the_shell_command_a_tool_runs():
    templates = (
        McpCommandTemplate("send", ("host", "send"), (("to", "--to", True), ("subject", "--subject", False), ("cc", "--cc", False))),
    )
    with installed(host(mcp_command_templates=templates)):
        synth = _core.synthesize_mcp_command
        assert synth("mcp__h__send", {"to": ["a@x", "b@x"], "subject": "hi", "body": "SECRET"}) == (
            "host send --to a@x --to b@x --subject hi"
        )
        assert synth("mcp__h__send", {"to": "a@x"}) == "host send --to a@x"
        assert synth("mcp__h__send", {}) == "host send"
        # A non-repeating flag renders its value as ONE str(), a list included (the shipped quo_send shape).
        assert synth("mcp__h__send", {"cc": ["a", "b"]}) == "host send --cc ['a', 'b']"
        assert synth("mcp__h__other", {"to": "a@x"}) is None
    with installed(GENERIC):
        assert synth("mcp__h__send", {"to": "a@x"}) is None


def test_the_agentwire_profile_reproduces_the_hard_coded_outbound_commands():
    assert _core.PROFILE.name == "agentwire"
    synth = _core.synthesize_mcp_command
    assert synth("mcp__agentwire__email_send", {"to": ["a@x", "b@x"], "subject": "S", "body": "B"}) == (
        "agentwire email --to a@x --to b@x --subject S"
    )
    assert synth("mcp__agentwire__quo_send", {"to": "+1555", "text": "T"}) == "agentwire quo --to +1555"
    assert synth("mcp__agentwire__quo_send", {"to": ["a", "b"]}) == "agentwire quo --to ['a', 'b']"


def test_groups_registry_comes_from_the_profile():
    from agentwire.safety import groups

    generic = groups._with_profile(groups._GENERIC_ENTRIES, GENERIC)
    assert "agentwire" not in {e.name for e in generic}
    assert not [e for e in generic if e.layer_rules]

    profile = host(
        groups=(GroupSpec("host", "group", "host stuff", layer_rules=("host.yaml",)),),
        group_layer_rules=(("core", ("host-core.yaml",)),),
    )
    merged = {e.name: e for e in groups._with_profile(groups._GENERIC_ENTRIES, profile)}
    assert merged["host"].files("rules") == ("host.yaml",)
    assert merged["core"].files("rules") == ("core.yaml", "host-core.yaml") and merged["core"].kernel
    with pytest.raises(ValueError):
        groups._with_profile(groups._GENERIC_ENTRIES, host(group_layer_rules=(("nope", ("x.yaml",)),)))


def test_every_hook_main_is_parse_decide_render_in_thirty_lines():
    long = {}
    for path in sorted(HOOKS_DIR.glob("*-tool-damage-control.py")):
        main = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == "main"][-1]
        if main.end_lineno - main.lineno + 1 > 30:
            long[path.name] = main.end_lineno - main.lineno + 1
        calls = {c.func.id for c in ast.walk(main) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert {"parse_payload", "render"} <= calls, path.name
    assert not long, long
