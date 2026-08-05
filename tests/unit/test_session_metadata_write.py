"""The session-record write must never fail silently (#885).

``store_session_metadata`` used to end in ``except (IOError, TypeError): pass``,
so a failed write was indistinguishable from a successful one. Since #871 the
record holds the conversation id — the one piece of session identity that is
NOT otherwise recoverable — so a dropped write means a session that can never
be resumed, reported at the time as success.
"""

import json

import pytest

from agentwire import core


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path / "aw")


def _agent(conversation_id="conv-1"):
    return core.AgentCommand(
        command="claude", posture="bypass", conversation_id=conversation_id)


def _metadata_path(session):
    return core.CONFIG_DIR / "sessions" / session / "metadata.json"


def _block_the_write(session):
    """Make the metadata write fail the way a real one does — at open time.

    The directory creation above it already raised before #885 (it sits
    outside the try), so a test that blocks THAT proves nothing about the
    swallow. Blocking the file open is the case the bare ``except: pass``
    actually hid.
    """
    _metadata_path(session).mkdir(parents=True)


class TestStoreRaises:
    def test_unwritable_destination_raises_oserror(self):
        """A write that can't land must raise, not report success by silence."""
        _block_the_write("sess")
        with pytest.raises(OSError):
            core.store_session_metadata("sess", {"a": 1})

    def test_unserializable_metadata_raises_typeerror(self):
        """A non-JSON-able value is a CODE bug and must surface as one."""
        with pytest.raises(TypeError):
            core.store_session_metadata("sess", {"bad": {1, 2, 3}})

    def test_failed_write_leaves_the_previous_record_intact(self):
        """The old record survives a failed write — no truncate-then-fail.

        The pre-#885 implementation opened the destination with ``"w"``
        (truncating it) BEFORE serializing, so an unserializable value emptied
        a good record and ``load_session_metadata`` then read it back as ``{}``
        via its ``JSONDecodeError`` catch: the same silent loss by a second
        route.
        """
        core.store_session_metadata("sess", {"conversation_ids": ["conv-1"]})

        with pytest.raises(TypeError):
            core.store_session_metadata("sess", {"bad": {1, 2, 3}})

        assert core.load_session_metadata("sess") == {"conversation_ids": ["conv-1"]}
        assert json.loads(_metadata_path("sess").read_text())["conversation_ids"] \
            == ["conv-1"]

    def test_no_temp_files_left_behind_on_success(self):
        core.store_session_metadata("sess", {"a": 1})
        leftovers = [p.name for p in _metadata_path("sess").parent.iterdir()
                     if p.name != "metadata.json"]
        assert leftovers == []


class TestRecordSessionLaunchReportsFailure:
    def test_warns_loudly_on_stderr_when_the_record_cannot_be_written(
            self, tmp_path, capsys, monkeypatch):
        """The launch already succeeded, so warn — but never stay silent."""
        _block_the_write("proj-fix")

        meta = core.record_session_launch(
            "proj-fix", _agent(), tmp_path, created_via="new")

        err = capsys.readouterr().err
        assert "proj-fix" in err
        assert "conv-1" in err
        # Names the consequence, not just the errno.
        assert "not recorded" in err.lower() or "could not" in err.lower()
        # The dict is still returned — the session itself is up.
        assert meta["conversation_ids"] == ["conv-1"]

    def test_silent_on_success(self, tmp_path, capsys):
        core.record_session_launch("proj-fix", _agent(), tmp_path, created_via="new")
        assert capsys.readouterr().err == ""

    def test_unserializable_record_warns_without_crashing_a_live_session(
            self, tmp_path, capsys):
        """A code bug is named on stderr — but never becomes a traceback here.

        The session is already running in tmux by the time this is called, so
        raising would report a failed command for a creation that succeeded.
        ``store_session_metadata`` itself still raises for every other caller.
        """
        agent = _agent()
        agent.posture = object()  # never JSON-able — only reachable via a bug
        core.record_session_launch("proj-fix", agent, tmp_path)
        err = capsys.readouterr().err
        assert "TypeError" in err
        assert "proj-fix" in err
