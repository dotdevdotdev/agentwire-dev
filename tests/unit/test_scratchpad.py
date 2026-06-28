"""Tests for agentwire/scratchpad.py — shared notes storage (#156 redesign)."""

import json

import pytest

from agentwire import scratchpad


@pytest.fixture(autouse=True)
def pad_file(tmp_path, monkeypatch):
    f = tmp_path / "scratchpad.json"
    monkeypatch.setattr(scratchpad, "SCRATCHPAD_FILE", f)
    return f


class TestStorage:
    def test_empty_when_missing(self):
        assert scratchpad.load_notes() == []

    def test_add_and_load(self):
        note = scratchpad.add_note("remember the milk", source="selection")
        notes = scratchpad.load_notes()
        assert len(notes) == 1
        assert notes[0]["id"] == note["id"]
        assert notes[0]["text"] == "remember the milk"
        assert notes[0]["source"] == "selection"
        assert notes[0]["created"] and notes[0]["updated"]

    def test_newest_first(self):
        scratchpad.add_note("first")
        scratchpad.add_note("second")
        assert [n["text"] for n in scratchpad.load_notes()] == ["second", "first"]

    def test_empty_text_rejected(self):
        with pytest.raises(ValueError):
            scratchpad.add_note("   ")

    def test_text_capped(self):
        note = scratchpad.add_note("x" * (scratchpad.MAX_NOTE_CHARS + 500))
        assert len(note["text"]) == scratchpad.MAX_NOTE_CHARS

    def test_max_notes_drops_oldest(self, monkeypatch):
        monkeypatch.setattr(scratchpad, "MAX_NOTES", 3)
        for i in range(5):
            scratchpad.add_note(f"note {i}")
        texts = [n["text"] for n in scratchpad.load_notes()]
        assert texts == ["note 4", "note 3", "note 2"]

    def test_update(self):
        note = scratchpad.add_note("draft")
        updated = scratchpad.update_note(note["id"], "final")
        assert updated["text"] == "final"
        assert scratchpad.load_notes()[0]["text"] == "final"

    def test_update_missing_returns_none(self):
        assert scratchpad.update_note("nope", "x") is None

    def test_remove(self):
        note = scratchpad.add_note("bye")
        assert scratchpad.remove_note(note["id"]) is True
        assert scratchpad.load_notes() == []
        assert scratchpad.remove_note(note["id"]) is False

    def test_clear(self):
        scratchpad.add_note("a")
        scratchpad.add_note("b")
        assert scratchpad.clear_notes() == 2
        assert scratchpad.load_notes() == []

    def test_corrupt_file_treated_as_empty(self, pad_file):
        pad_file.write_text("{broken json")
        assert scratchpad.load_notes() == []
        # And it heals on the next write
        scratchpad.add_note("fresh start")
        assert len(scratchpad.load_notes()) == 1


class TestCLI:
    @pytest.fixture(autouse=True)
    def no_portal_ping(self, monkeypatch):
        from agentwire import system_cli as sys_mod
        monkeypatch.setattr(sys_mod, "_ping_scratchpad_changed", lambda: None)

    def _args(self, **kw):
        import argparse
        defaults = {"json": True}
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_add_then_list(self, capsys):
        from agentwire.system_cli import cmd_scratchpad_add, cmd_scratchpad_list
        assert cmd_scratchpad_add(self._args(text="hello pad", source="test")) == 0
        capsys.readouterr()
        assert cmd_scratchpad_list(self._args()) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["notes"][0]["text"] == "hello pad"

    def test_remove(self, capsys):
        from agentwire.system_cli import cmd_scratchpad_add, cmd_scratchpad_remove
        cmd_scratchpad_add(self._args(text="to delete", source=None))
        note_id = json.loads(capsys.readouterr().out)["note"]["id"]
        assert cmd_scratchpad_remove(self._args(id=note_id)) == 0
        assert scratchpad.load_notes() == []

    def test_remove_unknown_fails(self, capsys):
        from agentwire.system_cli import cmd_scratchpad_remove
        assert cmd_scratchpad_remove(self._args(id="missing")) == 1

    def test_clear(self, capsys):
        from agentwire.system_cli import cmd_scratchpad_add, cmd_scratchpad_clear
        cmd_scratchpad_add(self._args(text="a", source=None))
        cmd_scratchpad_add(self._args(text="b", source=None))
        capsys.readouterr()
        assert cmd_scratchpad_clear(self._args()) == 0
        assert json.loads(capsys.readouterr().out)["count"] == 2
