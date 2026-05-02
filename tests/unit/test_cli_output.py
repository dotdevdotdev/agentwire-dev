"""Tests for __main__.py — _output_json and _output_result."""

import json
import sys

import pytest


# --- _output_json ---

class TestOutputJson:
    def test_produces_valid_json(self, capsys):
        from agentwire.__main__ import _output_json

        data = {"key": "value", "num": 42, "list": [1, 2, 3]}
        _output_json(data)

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed == data

    def test_empty_dict(self, capsys):
        from agentwire.__main__ import _output_json

        _output_json({})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {}


# --- _output_result ---

class TestOutputResult:
    def test_success_text_mode(self, capsys):
        from agentwire.__main__ import _output_result

        code = _output_result(success=True, json_mode=False, message="All good")
        captured = capsys.readouterr()
        assert code == 0
        assert "All good" in captured.out

    def test_failure_text_mode(self, capsys):
        from agentwire.__main__ import _output_result

        code = _output_result(success=False, json_mode=False, message="Something broke")
        captured = capsys.readouterr()
        assert code == 1
        assert "Something broke" in captured.err

    def test_success_json_mode(self, capsys):
        from agentwire.__main__ import _output_result

        code = _output_result(success=True, json_mode=True, message="ok", sessions=["a", "b"])
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["success"] is True
        assert parsed["sessions"] == ["a", "b"]
        assert code == 0

    def test_failure_json_mode(self, capsys):
        from agentwire.__main__ import _output_result

        code = _output_result(success=False, json_mode=True, message="broken")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["success"] is False
        assert parsed["error"] == "broken"
        assert code == 1

    def test_custom_exit_code(self, capsys):
        from agentwire.__main__ import _output_result

        code = _output_result(success=False, json_mode=True, message="x", exit_code=42)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert code == 42
        assert parsed["exit_code"] == 42
