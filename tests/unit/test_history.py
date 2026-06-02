"""Tests for agentwire/history.py — Path encoding/decoding."""

from agentwire.history import encode_project_path, decode_project_path


class TestPathEncoding:
    def test_encode(self):
        assert encode_project_path("/home/user/projects/myapp") == "-home-user-projects-myapp"

    def test_decode(self):
        assert decode_project_path("-home-user-projects-myapp") == "/home/user/projects/myapp"

    def test_round_trip_no_hyphens(self):
        """Round-trip works for paths without hyphens (encode uses /, decode uses -)."""
        original = "/home/user/projects/myapp"
        encoded = encode_project_path(original)
        decoded = decode_project_path(encoded)
        assert decoded == original

    def test_round_trip_lossy_with_hyphens(self):
        """Paths with hyphens lose information (known Claude Code limitation)."""
        original = "/home/user/projects/my-app"
        encoded = encode_project_path(original)
        assert encoded == "-home-user-projects-my-app"
        # decode converts ALL dashes to slashes — can't distinguish original hyphens
        decoded = decode_project_path(encoded)
        assert decoded != original  # Lossy

    def test_encode_root(self):
        assert encode_project_path("/") == "-"

    def test_encode_home(self):
        result = encode_project_path("/home/user")
        assert result == "-home-user"
