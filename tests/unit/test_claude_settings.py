"""#1220: the one writer of Claude's settings.json refuses, writes atomically, and survives concurrent edits."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentwire import claude_settings, hooks_cli
from agentwire.claude_settings import SettingsError, edit_claude_settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    return path


def _leftovers(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name != path.name)


def test_unparseable_settings_are_refused_byte_identical_by_every_writer(settings):
    broken = b'{"model": "opus", "env": {"A": "1"},}\n'  # stray comma
    settings.write_bytes(broken)
    writers = {
        "register_hook": lambda: hooks_cli.register_hook_in_settings("Notification", "idle-handler.sh"),
        "unregister_hook": lambda: hooks_cli.unregister_hook_from_settings("Notification", "idle-handler.sh"),
    }
    bad = {}
    for name, write in writers.items():
        try:
            write()
            bad[name] = "did not raise"
        except SettingsError:
            if settings.read_bytes() != broken or _leftovers(settings):
                bad[name] = "touched the file or left debris"
    assert not bad, bad


def test_non_object_settings_are_refused(settings):
    settings.write_text("[1, 2]")
    with pytest.raises(SettingsError):
        edit_claude_settings(lambda s: True)
    assert settings.read_text() == "[1, 2]"


def test_hooks_install_exits_nonzero_on_unparseable_settings(settings, capsys):
    broken = b"{nope"
    settings.write_bytes(broken)
    rc = hooks_cli.cmd_hooks_install(SimpleNamespace(force=False, copy=False, allow_foreign_source=True))
    assert rc != 0
    assert settings.read_bytes() == broken
    assert "not valid JSON" in capsys.readouterr().out


def test_interrupted_write_leaves_original_intact(settings, monkeypatch):
    original = json.dumps({"keep": "me"}, indent=2)
    settings.write_text(original)

    def boom(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        edit_claude_settings(lambda s: s.update(new=1) or True)
    assert settings.read_text() == original
    assert [n for n in _leftovers(settings) if n.endswith(".tmp")] == []


def test_concurrent_edit_is_not_lost(settings):
    settings.write_text(json.dumps({"a": 1}))
    calls = []

    def mutate(s):
        calls.append(dict(s))
        if len(calls) == 1:  # another process lands a write between our read and replace
            settings.write_text(json.dumps({"a": 1, "theirs": True}))
        s["ours"] = True
        return True

    assert edit_claude_settings(mutate) is True
    assert json.loads(settings.read_text()) == {"a": 1, "theirs": True, "ours": True}
    assert len(calls) == 2


def test_file_that_keeps_changing_is_refused_not_clobbered(settings):
    settings.write_text(json.dumps({"n": 0}))
    n = [0]

    def mutate(s):
        n[0] += 1
        settings.write_text(json.dumps({"n": n[0]}))
        s["ours"] = True
        return True

    with pytest.raises(SettingsError, match="kept changing"):
        edit_claude_settings(mutate)
    assert "ours" not in json.loads(settings.read_text())
    assert [x for x in _leftovers(settings) if x.endswith(".tmp")] == []


def test_backup_taken_once_before_the_first_change(settings):
    first = json.dumps({"v": 1})
    settings.write_text(first)
    edit_claude_settings(lambda s: s.update(v=2) or True)
    edit_claude_settings(lambda s: s.update(v=3) or True)
    backup = settings.with_name("settings.json" + claude_settings.BACKUP_SUFFIX)
    assert backup.read_text() == first
    assert json.loads(settings.read_text()) == {"v": 3}


def test_no_change_writes_nothing(settings):
    settings.write_text('{"a":1}')
    assert edit_claude_settings(lambda s: False) is False
    assert settings.read_text() == '{"a":1}'
    assert _leftovers(settings) == []


def test_missing_or_empty_settings_start_from_empty_object(settings):
    assert edit_claude_settings(lambda s: s.update(a=1) or True)
    assert json.loads(settings.read_text()) == {"a": 1}
    settings.write_text("  \n")
    assert edit_claude_settings(lambda s: s.update(b=2) or True)
    assert json.loads(settings.read_text()) == {"b": 2}


def test_symlinked_settings_are_edited_through_the_link(settings, tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    settings.symlink_to(real)
    edit_claude_settings(lambda s: s.update(a=1) or True)
    assert settings.is_symlink()
    assert json.loads(real.read_text()) == {"a": 1}
