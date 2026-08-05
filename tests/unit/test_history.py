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


class TestPathEncoding:
    """The mapping cwd -> ``~/.claude/projects/<key>``, measured not assumed.

    Every expectation below was checked against the installed Claude Code
    (2.1.222) by running a real one-shot in the directory and reading back the
    key it created — the same discipline #878 used on tmux's name mapping.
    """

    def test_encode(self):
        assert encode_project_path("/home/user/projects/myapp") == "-home-user-projects-myapp"

    def test_encode_root(self):
        assert encode_project_path("/") == "-"

    def test_encode_home(self):
        assert encode_project_path("/home/user") == "-home-user"

    def test_hyphens_are_preserved_as_hyphens(self):
        assert encode_project_path("/home/user/my-app") == "-home-user-my-app"

    def test_dot_becomes_a_dash(self):
        """The bug class this repo keeps hitting (#865 -> #868 -> #870 -> #878).

        Ground truth: ``/Users/dotdev/.claude`` really is stored under
        ``-Users-dotdev--claude`` locally. The old encoder, which replaced only
        ``/``, produced ``-Users-dotdev-.claude`` and found nothing.
        """
        assert encode_project_path("/Users/dotdev/.claude") == "-Users-dotdev--claude"
        assert (
            encode_project_path("/Users/dotdev/.agentwire/council/craps/workspace")
            == "-Users-dotdev--agentwire-council-craps-workspace"
        )

    def test_dotted_project_directory(self):
        assert encode_project_path("/Users/dotdev/projects/dotdev.dev") == "-Users-dotdev-projects-dotdev-dev"

    def test_every_punctuation_char_maps_one_to_one(self):
        """Measured by sweeping a real ``claude`` run; nothing is dropped."""
        assert encode_project_path("a_b.c+d~e@f,g=h!i#j%k^l&m n o'p") == "a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p"

    def test_non_ascii_letters_are_not_alphanumeric(self):
        """The class is ASCII ``[A-Za-z0-9]``, not ``str.isalnum()``.

        ``str.isalnum()`` is True for é/日/Ω and would have preserved them;
        real Claude Code replaces them.
        """
        assert encode_project_path("café-日本-Ωx") == "caf------x"

    def test_case_and_digits_survive(self):
        assert encode_project_path("/Users/Dev01/Repo9") == "-Users-Dev01-Repo9"

    def test_encoding_is_many_to_one(self):
        """Why there is no inverse: distinct cwds share a directory name."""
        assert encode_project_path("/a/b") == encode_project_path("/a-b") == encode_project_path("/a.b")

    def test_no_decode_function_is_exported(self):
        import agentwire.history as history

        assert not hasattr(history, "decode_project_path")


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

    def test_resumable(self, projects, tmp_path):
        self._write(projects, tmp_path / "wt", "cid")
        loc = locate_conversation("cid", tmp_path / "wt", projects_dir=projects)
        assert loc.status == "resumable" and loc.resumable
        assert loc.found_at.name == "cid.jsonl"
        assert loc.elsewhere == ()

    def test_orphaned_when_it_lives_under_another_cwd_key(self, projects, tmp_path):
        """A moved directory: intact and unreachable at the same time."""
        self._write(projects, tmp_path / "old", "cid")
        loc = locate_conversation("cid", tmp_path / "new", projects_dir=projects)
        assert loc.status == "orphaned"
        assert not loc.resumable
        assert loc.elsewhere[0].parent.name == encode_project_path(str(tmp_path / "old"))

    def test_gone_when_nothing_holds_it(self, projects, tmp_path):
        """Never prompted (lazy transcript creation) or evicted — same shape."""
        loc = locate_conversation("cid", tmp_path / "wt", projects_dir=projects)
        assert loc.status == "gone"
        assert loc.found_at is None and loc.elsewhere == ()

    def test_expected_dir_is_the_cwd_key(self, projects, tmp_path):
        loc = locate_conversation("cid", tmp_path / "wt", projects_dir=projects)
        assert loc.expected_dir.name == encode_project_path(str(tmp_path / "wt"))

    def test_missing_projects_dir_is_gone_not_an_error(self, tmp_path):
        loc = locate_conversation("cid", tmp_path, projects_dir=tmp_path / "nope")
        assert loc.status == "gone"


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

    def test_a_transcript_with_a_turn_is(self, tmp_path):
        from agentwire.history import holds_a_conversation

        f = tmp_path / "real.jsonl"
        f.write_text(self.STUB + self.TURN)
        assert holds_a_conversation(f) is True

    def test_unreadable_reads_as_no_conversation(self, tmp_path):
        """Safe direction: start fresh with the role, never hand claude an id
        it will reject."""
        from agentwire.history import holds_a_conversation

        assert holds_a_conversation(tmp_path / "does-not-exist.jsonl") is False

    def test_a_stub_at_the_expected_key_does_not_mask_the_real_orphan(self, tmp_path):
        """The measured sequence: restart a moved session, and Claude leaves a
        stub at the NEW key while the conversation stays at the old one."""
        projects = tmp_path / "projects"
        old = projects / encode_project_path(str(tmp_path / "old"))
        new = projects / encode_project_path(str(tmp_path / "new"))
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "cid.jsonl").write_text(self.TURN)
        (new / "cid.jsonl").write_text(self.STUB)

        loc = locate_conversation("cid", tmp_path / "new", projects_dir=projects)
        assert loc.status == "orphaned"
        assert loc.found_at is None
        assert loc.elsewhere[0].parent == old
