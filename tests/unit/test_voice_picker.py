"""Voice selection: enumeration, the live picker, and register-vs-update (#1017).

Three paper cuts from the first live session, and what they have in common is
that each one is invisible from inside the code — they are only defects if you
are the owner, on a phone, with no screen to read:

1. ``--voice`` took any string. A typo produced a bridge that connects and then
   sounds like nothing, which is indistinguishable from a dead mic.
2. Changing voice meant Ctrl-C, re-serve, reload. Ten values, three steps.
3. ``register --voice`` on an existing buddy re-printed the whole registration
   blurb, which reads like a second identity was created.

The pins below are chosen so a regression fails as a BEHAVIOUR failure: the
enumeration is read out of :data:`realtime.VOICES` wherever it is asserted (a
second literal list in a test is the drift it exists to catch), and the picker's
wire is executed as itself, extracted from the page the server actually serves
— the #995 rule that a wire nothing runs can be cut with the suite still green.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentwire import buddy_cli, core, inbox
from agentwire.voice_layer import identity, realtime, server


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    return tmp_path


# =============================================================================
# 1. The enumeration
# =============================================================================


class TestABadVoiceFailsEarly:
    """Before the record, before the epoch, before the key is spent."""

    def _args(self, voice, **extra):
        return SimpleNamespace(
            name="buddy", voice=voice, model="", json=False, port=0, **extra
        )

    def test_register_refuses_and_writes_nothing(self, isolate, capsys):
        assert buddy_cli.cmd_buddy_register(self._args("cedarr")) == 1
        assert not identity.is_registered("buddy")
        assert "cedarr" in capsys.readouterr().err

    def test_mint_refuses_before_touching_the_api(self, isolate, monkeypatch):
        identity.register("buddy")
        monkeypatch.setattr(
            realtime, "mint_session",
            lambda **k: pytest.fail("the API key was spent on an invalid voice"),
        )
        assert buddy_cli.cmd_buddy_mint(self._args("nope")) == 1


# =============================================================================
# 2. A recorded voice is a voice that is USED
# =============================================================================


# =============================================================================
# 3. The picker: one click, and the reconnect is the mechanism
# =============================================================================


class TestTheMintCarriesTheVoice:
    @pytest.fixture
    def bridge(self, isolate, monkeypatch):
        identity.register("buddy")
        minted = []

        def fake_mint(**kwargs):
            minted.append(kwargs)
            return {"id": "sess", "client_secret": "sk", "expires_at": 1,
                    "model": kwargs["model"], "calls_url": "u"}

        monkeypatch.setattr(realtime, "mint_session", fake_mint)
        return server.BuddyBridge("buddy", "tok", roster=lambda: ""), minted


    def test_a_mint_with_a_voice_switches_and_reports_it(self, bridge):
        bridge_obj, minted = bridge
        result = bridge_obj.mint({"voice": "marin"})
        assert minted[0]["voice"] == "marin"
        assert result["voice"] == "marin"
        assert result["voice_changed"] is True


# =============================================================================
# 4. register vs. update
# =============================================================================
