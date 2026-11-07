"""Packaged subagent definitions: frontmatter contract and install/drift (#1335).

Everything runs against a temp dir; the real ~/.claude/agents is never touched.
"""

import yaml
import pytest

import agentwire.hooks_cli as m
from agentwire.doctor_cli import _render_subagent_section
from agentwire.hooks_cli import (
    get_subagents_source,
    install_subagents,
    subagent_drift,
)

EXPECTED = {
    "aw-reviewer.md", "aw-verifier.md", "aw-researcher.md",
    "aw-implementer.md", "aw-test-runner.md", "aw-browser.md",
}
READ_ONLY = {"aw-reviewer.md", "aw-verifier.md", "aw-researcher.md", "aw-test-runner.md", "aw-browser.md"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
# The parent session owns spawning, reporting and merging: no definition may
# be handed the tools to do any of it.
FORBIDDEN_TOOL_PREFIXES = ("Agent", "Workflow", "mcp__agentwire__")


def _frontmatter(path):
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name}: no frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1])


def _tools(fm):
    raw = fm.get("tools", [])
    return {t.strip() for t in (raw.split(",") if isinstance(raw, str) else raw)}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    target = tmp_path / "claude" / "agents"
    monkeypatch.setattr(m, "claude_agents_dir", lambda: target)
    return target


def test_packaged_definitions_meet_the_contract():
    source = get_subagents_source()
    assert {p.name for p in source.glob("*.md")} == EXPECTED
    bad = []
    for path in sorted(source.glob("*.md")):
        fm = _frontmatter(path)
        if fm.get("name") != path.stem:
            bad.append(f"{path.name}: name {fm.get('name')!r} != filename stem")
        for field in ("description", "tools", "model"):
            if not fm.get(field):
                bad.append(f"{path.name}: missing {field}")
        if "hooks" in fm:
            bad.append(f"{path.name}: frontmatter hooks do not fire for subagents; do not rely on them")
        tools = _tools(fm)
        if path.name in READ_ONLY and tools & WRITE_TOOLS:
            bad.append(f"{path.name}: read-only agent holds {sorted(tools & WRITE_TOOLS)}")
        spawners = {t for t in tools if t.startswith(FORBIDDEN_TOOL_PREFIXES)}
        if spawners:
            bad.append(f"{path.name}: may spawn or report via {sorted(spawners)}")
        if (fm.get("isolation") == "worktree") != (path.name == "aw-implementer.md"):
            bad.append(f"{path.name}: isolation must be worktree for the implementer only")
    assert not bad, bad


def test_install_then_drift_is_ok(agents_dir):
    results = install_subagents(allow_foreign=True)
    assert set(results.values()) == {"installed"} and set(results) == EXPECTED
    assert set(subagent_drift().values()) == {"ok"}
    assert set(install_subagents(allow_foreign=True).values()) == {"current"}


def test_drifted_copy_is_flagged_and_healed(agents_dir):
    install_subagents(copy=True, allow_foreign=True)
    (agents_dir / "aw-reviewer.md").write_text("edited by hand\n")
    (agents_dir / "aw-browser.md").unlink()
    assert subagent_drift() == {
        **{n: "ok" for n in EXPECTED},
        "aw-reviewer.md": "stale",
        "aw-browser.md": "missing",
    }
    assert _render_subagent_section() == 2

    install_subagents(copy=True, allow_foreign=True)
    assert set(subagent_drift().values()) == {"ok"}
    assert _render_subagent_section() == 0


def test_dangling_link_is_stale(agents_dir, tmp_path):
    install_subagents(allow_foreign=True)
    link = agents_dir / "aw-verifier.md"
    link.unlink()
    link.symlink_to(tmp_path / "gone.md")
    assert subagent_drift()["aw-verifier.md"] == "stale"
