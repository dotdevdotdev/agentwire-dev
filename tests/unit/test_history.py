"""Tests for agentwire/history.py — cwd -> history-directory encoding.

The expectations here are transcribed from measurements against real Claude
Code, not from a reading of the docs (#871) — see
:func:`agentwire.history.encode_project_path` for how they were taken.
``decode_project_path`` used to live alongside it and has been deleted: the
mapping is many-to-one, so an inverse cannot exist, and the old round-trip
tests only passed by choosing paths that dodged the ambiguity.
"""

from agentwire.history import encode_project_path


class TestPathEncoding:
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
