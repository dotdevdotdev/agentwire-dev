"""Tests for agentwire/history.py — cwd -> history-directory encoding.

The expectations here are transcribed from measurements against real Claude
Code, not from a reading of the docs (#871) — see
:func:`agentwire.history.encode_project_path` for how they were taken.
``decode_project_path`` used to live alongside it and has been deleted: the
mapping is many-to-one, so an inverse cannot exist, and the old round-trip
tests only passed by choosing paths that dodged the ambiguity.
"""

import pytest

from agentwire.history import encode_project_path, locate_conversation


class TestLocateConversation:
    """``resumable(id, cwd) == exists(<encoded_cwd>/<id>.jsonl)`` (#871)."""

    @pytest.fixture
    def projects(self, tmp_path):
        d = tmp_path / "projects"
        d.mkdir()
        return d

    #: A transcript counts only if it holds a TURN — a metadata-only stub is
    #: a dead id (see TestStubTranscripts below).
    TURN = '{"type":"user","message":{"role":"user","content":"hi"}}\n'

    def _write(self, projects, cwd, cid, body=None):
        d = projects / encode_project_path(str(cwd))
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.jsonl").write_text(body or self.TURN)

    def test_orphaned_when_it_lives_under_another_cwd_key(self, projects, tmp_path):
        """A moved directory: intact and unreachable at the same time."""
        self._write(projects, tmp_path / "old", "cid")
        loc = locate_conversation("cid", tmp_path / "new", projects_dir=projects)
        assert loc.status == "orphaned"
        assert not loc.resumable
        assert loc.elsewhere[0].parent.name == encode_project_path(str(tmp_path / "old"))


class TestStubTranscripts:
    """A file that exists but holds no turns is a DEAD id, not a hit (#871).

    Measured on real Claude Code 2.1.222, in the exact state a restart of a
    moved session leaves behind — a 5-line metadata file at the new key while
    the conversation sits under the old one:

        claude --resume <id>      -> "No conversation found with session ID"
        claude --session-id <id>  -> "Session ID <id> is already in use."

    Neither flag will take it. Treating the file as a hit would have made
    `restart` pass it to --resume, claude refuse to start, and the pane drop
    to a bare shell — while doctor reported the orphan as healed.
    """

    STUB = ('{"type":"last-prompt"}\n{"type":"ai-title"}\n'
            '{"type":"mode","mode":"normal"}\n')
    TURN = '{"type":"user","message":{"role":"user"}}\n'

    def test_a_metadata_stub_is_not_a_conversation(self, tmp_path):
        from agentwire.history import holds_a_conversation

        f = tmp_path / "stub.jsonl"
        f.write_text(self.STUB)
        assert holds_a_conversation(f) is False


