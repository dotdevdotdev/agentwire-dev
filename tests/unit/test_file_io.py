"""Tests for agentwire/utils/file_io.py — JSON/YAML load/save utilities."""

import json

import pytest

from agentwire.utils.file_io import load_json, load_yaml, save_json, save_yaml

# --- load/save JSON ---

class TestJSON:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "test.json"
        data = {"key": "value", "num": 42, "nested": {"a": 1}}
        save_json(path, data)
        loaded = load_json(path)
        assert loaded == data

    def test_missing_with_default(self, tmp_path):
        result = load_json(tmp_path / "no.json", default={"default": True})
        assert result == {"default": True}

    def test_missing_raises_without_default(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_json(tmp_path / "no.json")

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json {{{")
        with pytest.raises(json.JSONDecodeError):
            load_json(path)

    def test_atomic_write(self, tmp_path):
        path = tmp_path / "atomic.json"
        save_json(path, {"x": 1}, atomic=True)
        assert load_json(path) == {"x": 1}

    def test_non_atomic_write(self, tmp_path):
        path = tmp_path / "direct.json"
        save_json(path, {"y": 2}, atomic=False)
        assert load_json(path) == {"y": 2}


# --- load/save YAML ---

class TestYAML:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "test.yaml"
        data = {"server": {"port": 8765}, "enabled": True}
        save_yaml(path, data)
        loaded = load_yaml(path)
        assert loaded == data

    def test_missing_with_default(self, tmp_path):
        result = load_yaml(tmp_path / "no.yaml", default={})
        assert result == {}

    def test_missing_raises_without_default(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_yaml(tmp_path / "no.yaml")

    def test_empty_file_returns_empty_dict(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        result = load_yaml(path)
        assert result == {}

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "sub" / "dir" / "test.yaml"
        save_yaml(path, {"a": 1})
        assert load_yaml(path) == {"a": 1}
