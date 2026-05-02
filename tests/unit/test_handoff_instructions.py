"""Tests for agentwire/handoff/instructions.py."""

from pathlib import Path
from unittest import mock

import pytest

from agentwire.handoff import instructions


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.claude to a tmp dir so tests don't read real user files."""
    fake_claude = tmp_path / ".claude"
    fake_claude.mkdir()
    monkeypatch.setattr(instructions, "HOME", tmp_path)
    monkeypatch.setattr(instructions, "CLAUDE_DIR", fake_claude)
    return tmp_path


class TestGlobalCLAUDE:
    def test_no_global_returns_empty_user(self, fake_home: Path, tmp_path: Path):
        out = instructions.collect(cwd=tmp_path / "project")
        # cwd doesn't exist; chain is empty
        assert all(i.kind != "claude_md" for i in out)

    def test_global_present(self, fake_home: Path, tmp_path: Path):
        (fake_home / ".claude" / "CLAUDE.md").write_text("# global rules\n")
        project = tmp_path / "project"
        project.mkdir()
        out = instructions.collect(cwd=project)
        kinds = [i.kind for i in out]
        assert "claude_md" in kinds
        global_instr = next(i for i in out if i.kind == "claude_md")
        assert "# global rules" in global_instr.content


class TestRules:
    def test_rules_directory_loaded(self, fake_home: Path, tmp_path: Path):
        rules_dir = fake_home / ".claude" / "rules"
        rules_dir.mkdir()
        (rules_dir / "a.md").write_text("rule A\n")
        (rules_dir / "b.md").write_text("rule B\n")
        project = tmp_path / "project"
        project.mkdir()
        out = instructions.collect(cwd=project)
        rule_paths = [i.path for i in out if i.kind == "rule"]
        assert any("a.md" in p for p in rule_paths)
        assert any("b.md" in p for p in rule_paths)


class TestProjectChain:
    def test_project_claude_md_picked_up(self, fake_home: Path, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# project rules\n")
        out = instructions.collect(cwd=project)
        project_instr = [i for i in out if i.kind == "project_claude_md"]
        assert len(project_instr) == 1
        assert "# project rules" in project_instr[0].content

    def test_nested_project_walks_up(self, fake_home: Path, tmp_path: Path):
        project = tmp_path / "project"
        nested = project / "sub"
        nested.mkdir(parents=True)
        (project / "CLAUDE.md").write_text("# parent\n")
        (nested / "CLAUDE.md").write_text("# child\n")
        out = instructions.collect(cwd=nested)
        project_instr = [i for i in out if i.kind == "project_claude_md"]
        # Should have both, ordered root-most first.
        assert len(project_instr) == 2
        assert "# parent" in project_instr[0].content
        assert "# child" in project_instr[1].content


class TestMemory:
    def test_memory_dir_loaded(self, fake_home: Path, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        encoded = instructions.encode_project_path(str(project.resolve()))
        memory_dir = fake_home / ".claude" / "projects" / encoded / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text(
            "# memory\n\nSee [topic](topic.md) for details.\n"
        )
        (memory_dir / "topic.md").write_text("topic body\n")

        out = instructions.collect(cwd=project)
        memory_instr = [i for i in out if i.kind == "memory"]
        assert len(memory_instr) == 2
        memory_contents = "\n".join(i.content for i in memory_instr)
        assert "topic body" in memory_contents

    def test_memory_links_outside_dir_skipped(self, fake_home: Path, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        encoded = instructions.encode_project_path(str(project.resolve()))
        memory_dir = fake_home / ".claude" / "projects" / encoded / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text(
            "# memory\n\nSee [escape](../../../etc/passwd) — should be skipped.\n"
        )

        out = instructions.collect(cwd=project)
        memory_instr = [i for i in out if i.kind == "memory"]
        # Only MEMORY.md, no traversal.
        assert len(memory_instr) == 1
