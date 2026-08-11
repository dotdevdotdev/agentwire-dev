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

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from agentwire import buddy_cli, core, inbox
from agentwire.voice_layer import client, identity, realtime, server
from tests.page_slice import page_slice

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the client's own JS"
)


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    return tmp_path


# =============================================================================
# 1. The enumeration
# =============================================================================


class TestTheVoicesAreEnumerated:
    def test_the_documented_ten_are_all_there(self):
        """Derived from the docs fetch (2026-08-11), asserted as a SET so the
        ordering stays free to be the reading order the owner sees."""
        assert set(realtime.VOICES) == {
            "alloy", "ash", "ballad", "cedar", "coral",
            "echo", "marin", "sage", "shimmer", "verse",
        }

    def test_the_default_is_one_of_them(self):
        assert realtime.DEFAULT_VOICE in realtime.VOICES

    def test_the_newer_pair_leads_the_list(self):
        """The order is a recommendation, and the picker renders it verbatim."""
        assert realtime.VOICES[:2] == ("cedar", "marin")

    @pytest.mark.parametrize("voice", realtime.VOICES)
    def test_every_listed_voice_validates(self, voice):
        assert realtime.validate_voice(voice) == voice

    def test_an_unknown_voice_refuses_with_the_whole_list(self):
        with pytest.raises(realtime.RealtimeError) as exc:
            realtime.validate_voice("cedarr")
        message = str(exc.value)
        assert "cedarr" in message
        for voice in realtime.VOICES:
            assert voice in message

    def test_case_is_folded_rather_than_refused(self):
        """A false reject here costs a real setting for a spelling nobody
        would call wrong."""
        assert realtime.validate_voice("Cedar") == "cedar"
        assert realtime.validate_voice(" MARIN ") == "marin"

    def test_empty_means_unspecified_not_invalid(self):
        assert realtime.validate_voice("") == ""

    def test_the_help_text_lists_every_voice(self):
        for voice in realtime.VOICES:
            assert voice in buddy_cli._VOICE_HELP
        assert realtime.DEFAULT_VOICE in buddy_cli._VOICE_HELP

    def test_the_help_text_carries_no_percent_sign(self):
        """argparse interpolates help printf-style; a lone `percent` raises at
        parse time, which would take out `--help` for the whole CLI."""
        assert "%" not in buddy_cli._VOICE_HELP

    def test_every_voice_flag_carries_the_help(self):
        """All three verbs, or the list is discoverable from whichever one the
        owner did not happen to run."""
        import argparse

        # argparse exits the process on --help, so the help string is read off
        # the parser's actions rather than by rendering it.
        parser = argparse.ArgumentParser()
        buddy_cli.register_buddy_parser(parser.add_subparsers(dest="command"))
        verbs = parser._subparsers._group_actions[0].choices["buddy"] \
            ._subparsers._group_actions[0].choices
        found = {
            verb: action.help
            for verb in ("register", "mint", "serve")
            for action in verbs[verb]._actions
            if action.dest == "voice"
        }
        assert found == {verb: buddy_cli._VOICE_HELP
                         for verb in ("register", "mint", "serve")}


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

    def test_serve_refuses_before_binding_a_socket(self, isolate, monkeypatch):
        identity.register("buddy")
        monkeypatch.setattr(
            server, "serve", lambda *a, **k: pytest.fail("bound a socket anyway")
        )
        assert buddy_cli.cmd_buddy_serve(self._args("nope")) == 1

    def test_the_json_caller_gets_a_json_error(self, isolate, capsys):
        """The reason `choices=` was not used: argparse would exit with a usage
        dump on stderr and no parseable object at all."""
        args = self._args("nope")
        args.json = True
        assert buddy_cli.cmd_buddy_register(args) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert "nope" in payload["error"]

    def test_identity_raises_its_own_error_type(self, isolate):
        """One failure contract per module. ``register`` is caught as a
        ``BuddyError`` at its only call site, so a ``RealtimeError`` escaping
        it would be an uncaught second contract — unreachable today only
        because the CLI pre-validates, which is not a guarantee."""
        for call in (
            lambda: identity.register("buddy", voice="cedarr"),
            lambda: identity.registration_delta("buddy", voice="cedarr"),
            lambda: identity.set_voice("buddy", "cedarr"),
            lambda: identity.resolve_voice("buddy", "cedarr"),
        ):
            with pytest.raises(identity.BuddyError):
                call()

    def test_mint_session_itself_refuses_too(self):
        """The last line before the key is spent, pinned independently of its
        callers — a new call site must not be able to route around them."""
        with pytest.raises(realtime.RealtimeError):
            realtime.mint_session(instructions="", tools=[], voice="nope")


# =============================================================================
# 2. A recorded voice is a voice that is USED
# =============================================================================


class TestTheRecordedVoiceIsRead:
    """`register --voice` used to write a key nothing ever read."""

    def test_resolution_is_explicit_then_recorded_then_default(self, isolate):
        identity.register("buddy", voice="marin")
        assert identity.resolve_voice("buddy", "ash") == "ash"
        assert identity.resolve_voice("buddy") == "marin"
        assert identity.resolve_voice("nobody") == realtime.DEFAULT_VOICE

    def test_a_retired_recorded_voice_falls_back_instead_of_wedging(self, isolate):
        """An id retired upstream must cost the default, never the bridge."""
        identity.register("buddy")
        meta = core.load_session_metadata("buddy")
        meta["realtime_voice"] = "voice-that-openai-retired"
        core.store_session_metadata("buddy", meta)
        assert identity.resolve_voice("buddy") == realtime.DEFAULT_VOICE

    def test_the_bridge_picks_the_recorded_voice_up(self, isolate):
        identity.register("buddy", voice="ballad")
        assert server.BuddyBridge("buddy", "tok").voice == "ballad"

    def test_an_explicit_flag_still_wins_at_the_bridge(self, isolate):
        identity.register("buddy", voice="ballad")
        assert server.BuddyBridge("buddy", "tok", voice="echo").voice == "echo"

    def test_status_reports_it(self, isolate):
        identity.register("buddy", voice="sage")
        assert identity.status("buddy")["realtime_voice"] == "sage"


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
        return server.BuddyBridge("buddy", "tok"), minted

    def test_a_mint_with_no_voice_uses_the_bridges(self, bridge):
        bridge_obj, minted = bridge
        result = bridge_obj.mint({})
        assert result["success"] is True
        assert minted[0]["voice"] == realtime.DEFAULT_VOICE
        assert result["voice_changed"] is False

    def test_a_mint_with_a_voice_switches_and_reports_it(self, bridge):
        bridge_obj, minted = bridge
        result = bridge_obj.mint({"voice": "marin"})
        assert minted[0]["voice"] == "marin"
        assert result["voice"] == "marin"
        assert result["voice_changed"] is True

    def test_the_switch_sticks_for_the_next_serve(self, bridge):
        bridge_obj, _ = bridge
        bridge_obj.mint({"voice": "marin"})
        assert identity.registered_voice("buddy") == "marin"
        assert server.BuddyBridge("buddy", "tok").voice == "marin"

    def test_an_unknown_voice_refuses_without_spending_the_key(self, bridge):
        bridge_obj, minted = bridge
        result = bridge_obj.mint({"voice": "nope"})
        assert result["success"] is False
        assert "nope" in result["error"]
        assert minted == []

    def test_a_refused_voice_does_not_burn_a_sequence_epoch(self, bridge):
        """The epoch is reserved per page load and the space is finite —
        a rejected picker value must not advance it."""
        bridge_obj, _ = bridge
        bridge_obj.mint({"voice": "nope"})
        assert bridge_obj.ring.high_seq == 0

    def test_a_failed_mint_adopts_nothing(self, bridge, monkeypatch):
        """A voice sticks once it has been MINTED with, and not before.

        Adopting first left an upstream 500 with the bridge and the record both
        moved to a voice that was never spoken — the page got its 502, the call
        never happened, and a reload came back showing a setting the owner has
        no evidence for.
        """
        bridge_obj, _ = bridge
        monkeypatch.setattr(
            realtime, "mint_session",
            lambda **k: (_ for _ in ()).throw(realtime.RealtimeError("upstream 500")),
        )
        with pytest.raises(realtime.RealtimeError):
            bridge_obj.mint({"voice": "marin"})
        assert bridge_obj.voice == realtime.DEFAULT_VOICE
        assert identity.registered_voice("buddy") == ""

    def test_the_failed_mint_was_still_attempted_on_the_new_voice(self, bridge, monkeypatch):
        """The other half: not adopting must not mean not honouring. Without
        this, "nothing moved" is equally satisfied by ignoring the picker."""
        seen = []
        bridge_obj, _ = bridge
        monkeypatch.setattr(
            realtime, "mint_session",
            lambda **k: seen.append(k["voice"]) or (_ for _ in ()).throw(
                realtime.RealtimeError("upstream 500")
            ),
        )
        with pytest.raises(realtime.RealtimeError):
            bridge_obj.mint({"voice": "marin"})
        assert seen == ["marin"]

    @pytest.mark.parametrize("body", [None, [1], "x", 7])
    def test_a_non_dict_body_is_treated_as_no_body(self, bridge, body):
        """`/mint` ignored its payload entirely until this argument existed —
        a bridge that 500s on `[1]` where it used to answer is a regression."""
        bridge_obj, minted = bridge
        assert bridge_obj.mint(body)["success"] is True
        assert minted[0]["voice"] == realtime.DEFAULT_VOICE

    def test_a_failed_persist_is_reported_not_swallowed(self, bridge, monkeypatch):
        """The live call must survive an unwritable record — the owner loses
        stickiness, and is told so rather than finding out next serve."""
        bridge_obj, minted = bridge
        monkeypatch.setattr(
            identity, "set_voice",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
        )
        result = bridge_obj.mint({"voice": "marin"})
        assert result["success"] is True
        assert minted[0]["voice"] == "marin"
        assert result["voice_persisted"] is False
        assert "read-only" in result["voice_persist_error"]


class TestThePageRendersThePicker:
    def test_every_voice_is_an_option(self):
        page = client.page("buddy", "tok")
        for voice in realtime.VOICES:
            assert f'value="{voice}"' in page

    def test_the_current_voice_is_the_selected_one(self):
        page = client.page("buddy", "tok", voice="marin")
        assert '<option value="marin" selected>' in page
        assert '<option value="cedar">' in page

    def test_the_newer_pair_is_labelled_and_nothing_else_is(self):
        """The label derives from ``VOICES[:2]``, and that derivation needs its
        own pin: swapping it for a hard-coded ``("alloy", "ash")`` left the
        whole suite green, which makes the structural win unenforced. Both
        halves — the two that carry it, and the eight that must not.
        """
        page = client.page("buddy", "tok")
        for voice in realtime.VOICES[:2]:
            assert f">{voice} (newer)<" in page, voice
        for voice in realtime.VOICES[2:]:
            assert f">{voice}<" in page, voice

    def test_the_page_defaults_to_the_default_voice(self):
        assert '<option value="cedar" selected>' in client.page("buddy", "tok")

    def test_the_served_page_shows_the_bridges_current_voice(self, isolate):
        """A switch made in one tab is what a RELOAD must come back showing."""
        identity.register("buddy")
        bridge = server.BuddyBridge("buddy", "tok")
        bridge.switch_voice("verse")
        page = client.page(bridge.buddy, bridge.token, voice=bridge.voice)
        assert '<option value="verse" selected>' in page


@needs_node
class TestTheVoicePickerWire:
    """The wire, executed as itself (#995).

    A picker that renders and does nothing is the worst of the three outcomes
    here: the owner sees the voice change on screen and hears the old one, and
    has no way to tell which half lied.
    """

    def _program(self, *, live: bool, pick: str, current: str = "cedar") -> str:
        page = client.page("buddy", "tok")
        switch = page_slice(
            page, r"async function switchVoice\(\) \{", r"\n\}\n",
            "the switchVoice body",
            shape=r"\$voice\.value[\s\S]*await start\(\)[\s\S]*\}$",
        )
        wire = page_slice(
            page, r'\$voice\.addEventListener\("change"', r";", "the voice change wire",
            shape=r'^\$voice\.addEventListener\("change",\s*\w+\);$',
        )
        return "\n".join([
            "const events = [];",
            f"let currentVoice = {json.dumps(current)};",
            "let greeted = true;",
            f"let pc = {'{}' if live else 'null'};",
            f"const $voice = {{ value: {json.dumps(pick)}, disabled: false }};",
            "const handlers = {};",
            "$voice.addEventListener = (n, fn) => { handlers[n] = fn; };",
            'function log(who, text) { events.push("log:" + text); }',
            'function setStatus(text) { events.push("status:" + text); }',
            'function stop() { events.push("stop"); pc = null; }',
            "async function start() {",
            '  events.push("start:" + currentVoice + ":greeted=" + greeted);',
            "  pc = {};",
            "}",
            switch,
            wire,
            'await handlers["change"]();',
            "console.log(JSON.stringify({ events, currentVoice, greeted, "
            "disabled: $voice.disabled, wired: Object.keys(handlers) }));",
        ])

    def _run(self, program: str) -> dict:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", program],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr.strip()}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_change_event_is_wired_to_switch_voice(self):
        report = self._run(self._program(live=True, pick="marin"))
        assert report["wired"] == ["change"]

    def test_a_live_call_is_torn_down_and_rebuilt_on_the_new_voice(self):
        report = self._run(self._program(live=True, pick="marin"))
        assert report["events"] == [
            "log:voice → marin", "stop", "start:marin:greeted=false",
        ]
        assert report["currentVoice"] == "marin"

    def test_the_switch_re_greets_so_the_owner_hears_the_new_voice(self):
        """Deliberately against the #963 quiet-reconnect rule: this reconnect
        was ASKED for, and its only observable result is how the buddy sounds."""
        report = self._run(self._program(live=True, pick="ash"))
        assert "start:ash:greeted=false" in report["events"]

    def test_picking_the_same_voice_does_nothing_at_all(self):
        report = self._run(self._program(live=True, pick="cedar"))
        assert report["events"] == []

    def test_an_idle_page_records_the_choice_without_connecting(self):
        report = self._run(self._program(live=False, pick="marin"))
        assert "stop" not in report["events"]
        assert not [e for e in report["events"] if e.startswith("start:")]
        assert report["currentVoice"] == "marin"

    def test_an_idle_switch_also_releases_the_greet_latch(self):
        """The calmer route to the same silent switch, and it is the ORDINARY
        one: talk (greeted), press Stop, pick a voice, press Start.

        ``stop()`` deliberately leaves ``greeted`` set (#963), so a release
        that lives only in the live branch means that sequence connects on the
        new voice and says nothing — the exact failure the live branch's
        re-greet exists to prevent. The fixture starts from ``greeted = true``
        precisely because that is the state the sequence arrives in; asserting
        the events without asserting the latch is how the first version of
        this harness built the bug and looked away.
        """
        report = self._run(self._program(live=False, pick="marin"))
        assert report["greeted"] is False

    def test_a_live_switch_releases_it_too(self):
        report = self._run(self._program(live=True, pick="marin"))
        assert report["greeted"] is False

    def test_declining_to_switch_leaves_the_latch_alone(self):
        """The release is scoped to an actual change — picking the voice you
        are already on must not re-greet you."""
        report = self._run(self._program(live=True, pick="cedar"))
        assert report["greeted"] is True

    def test_the_picker_is_re_enabled_after_the_reconnect(self):
        """Locked for the round trip, released in a `finally` — a picker left
        disabled by a failed reconnect is a setting the owner cannot retry."""
        report = self._run(self._program(live=True, pick="marin"))
        assert report["disabled"] is False


class TestTheMintBodyReachesTheBridge:
    def test_the_page_posts_the_voice_it_is_showing(self):
        page = client.page("buddy", "tok")
        assert 'post("/mint", { voice: currentVoice })' in page

    def test_the_handler_forwards_the_body(self):
        """A `/mint` handler calling `bridge.mint()` with no payload is the
        one-line mutation that makes the whole picker inert."""
        import inspect

        source = inspect.getsource(server._handler_factory)
        assert "bridge.mint(payload)" in source


# =============================================================================
# 4. register vs. update
# =============================================================================


class TestRegisterIsIdempotentInWhatItSays:
    def _args(self, **extra):
        fields = {"name": "buddy", "voice": "", "model": "", "json": False}
        fields.update(extra)
        return SimpleNamespace(**fields)

    def test_the_first_registration_announces_itself(self, isolate, capsys):
        assert buddy_cli.cmd_buddy_register(self._args()) == 0
        out = capsys.readouterr().out
        assert "Registered voice buddy 'buddy'." in out
        assert "inbox:" in out

    def test_a_voice_change_says_updated_and_nothing_else(self, isolate, capsys):
        buddy_cli.cmd_buddy_register(self._args())
        capsys.readouterr()
        assert buddy_cli.cmd_buddy_register(self._args(voice="marin")) == 0
        out = capsys.readouterr().out
        assert "updated voice → marin" in out
        assert "Registered voice buddy" not in out
        # The blurb's contents, not just its first line: naming the inbox and
        # the "other sessions can now reach it" line is what read as a second
        # identity being created.
        assert "inbox:" not in out
        assert "msg send --to" not in out

    def test_the_previous_voice_is_named(self, isolate, capsys):
        buddy_cli.cmd_buddy_register(self._args(voice="marin"))
        capsys.readouterr()
        buddy_cli.cmd_buddy_register(self._args(voice="ash"))
        assert "was marin" in capsys.readouterr().out

    def test_re_registering_with_nothing_to_change_says_so(self, isolate, capsys):
        buddy_cli.cmd_buddy_register(self._args(voice="marin"))
        capsys.readouterr()
        buddy_cli.cmd_buddy_register(self._args(voice="marin"))
        out = capsys.readouterr().out
        assert "already registered" in out
        assert "nothing to change" in out

    def test_the_record_is_still_updated(self, isolate):
        buddy_cli.cmd_buddy_register(self._args())
        buddy_cli.cmd_buddy_register(self._args(voice="marin"))
        assert identity.registered_voice("buddy") == "marin"

    def test_json_says_created_and_names_the_change(self, isolate, capsys):
        args = self._args(json=True)
        buddy_cli.cmd_buddy_register(args)
        first = json.loads(capsys.readouterr().out)
        assert first["created"] is True and first["changes"] == {}

        buddy_cli.cmd_buddy_register(self._args(json=True, voice="marin"))
        second = json.loads(capsys.readouterr().out)
        assert second["created"] is False
        assert second["changes"] == {"voice": {"from": None, "to": "marin"}}

    def test_the_delta_is_read_before_the_write(self, isolate):
        """Against the record that WAS there — computing it after register()
        would report every change as no change, silently."""
        identity.register("buddy", voice="marin")
        delta = identity.registration_delta("buddy", voice="ash")
        assert delta == {
            "registered": True,
            "changes": {"voice": {"from": "marin", "to": "ash"}},
        }
        assert identity.registered_voice("buddy") == "marin"

    def test_an_unregistered_name_is_not_an_update(self, isolate):
        assert identity.registration_delta("buddy", voice="ash")["registered"] is False
