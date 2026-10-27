"""Tests for __main__.py — _output_json and _output_result."""

import json

# --- _output_json ---



# --- _output_result ---

class TestOutputResult:



    def test_failure_json_mode(self, capsys):
        from agentwire.__main__ import _output_result

        code = _output_result(success=False, json_mode=True, message="broken")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["success"] is False
        assert parsed["error"] == "broken"
        assert code == 1

