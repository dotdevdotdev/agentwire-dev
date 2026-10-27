"""Tests for agentwire/handoff/instructions.py."""

from pathlib import Path

import pytest

from agentwire.handoff import instructions


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.claude to a tmp dir so tests don't read real user files."""
    fake_claude = tmp_path / ".claude"
    fake_claude.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(instructions, "claude_dir", lambda: fake_claude)
    return tmp_path








class TestMemory:

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
