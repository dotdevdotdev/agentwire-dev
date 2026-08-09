"""Tests for the EXPERIMENTAL voice-layer spike (branch-only).

Three things carry real risk and are tested accordingly:

1. **The drain seam.** The adapter must intercept before the gone gate (or the
   buddy's mail dead-letters in ~5 ticks) and after the cohort hold (or it
   consumes a report ``wait --children`` is about to collect). Both orderings
   are asserted, plus the inertness guarantee for every session that hasn't
   opted in.
2. **The tool allowlist.** Voice adds mis-transcription as a failure mode, so
   the model must never be able to name a command — only pick a tool. A garbled
   session name has to fail closed.
3. **The realtime request shape**, against what the current docs actually say.
"""

import json
from types import SimpleNamespace

import pytest

from agentwire import core, inbox
from agentwire.voice_layer import delivery, identity, instructions, realtime, tools


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """Throwaway config root + inbox, and tmux reported as reachable-but-empty.

    ``live_sessions`` returning a real set (not None) is what arms the gone
    gate — the precise condition under which an unadapted buddy loses its mail.
    """
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: {"some-other-session"})
    return tmp_path


# =============================================================================
# Identity
# =============================================================================


class TestIdentity:
    def test_register_creates_addressable_identity(self, isolate):
        identity.register("buddy")
        status = identity.status("buddy")
        assert status["registered"] is True
        assert status["role"] == identity.ROLE
        assert status["delivery"] == delivery.VOICE_ADAPTER
        assert identity.inbox_dir("buddy").is_dir()

    def test_register_records_no_conversation_or_git_identity(self, isolate):
        """The buddy has no Claude conversation and no checkout — say nothing."""
        metadata = identity.register("buddy")
        for key in ("conversation_ids", "repo", "branch", "worktree_path",
                    "posture", "role_prompt_path"):
            assert key not in metadata

    def test_register_is_idempotent_and_merge_preserving(self, isolate):
        first = identity.register("buddy")
        again = identity.register("buddy", model="gpt-realtime-2.1")
        assert again["created_at"] == first["created_at"]
        assert again["realtime_model"] == "gpt-realtime-2.1"

    def test_refuses_to_overwrite_a_real_session_record(self, isolate):
        core.store_session_metadata("orchestrator", {"posture": "bypass", "roles": []})
        with pytest.raises(identity.BuddyError, match="refusing to overwrite"):
            identity.register("orchestrator")

    def test_unregister_refuses_a_non_voice_record(self, isolate):
        core.store_session_metadata("worker", {"posture": "bypass"})
        with pytest.raises(identity.BuddyError, match="not a voice-layer record"):
            identity.unregister("worker")

    def test_unregister_without_purge_keeps_mail(self, isolate):
        identity.register("buddy")
        inbox.enqueue("buddy", "report", kind="done", sender="worker-1")
        identity.unregister("buddy")
        assert list(identity.inbox_dir("buddy").glob("*.json"))

    def test_unregister_with_purge_drops_mail(self, isolate):
        identity.register("buddy")
        inbox.enqueue("buddy", "report", kind="done", sender="worker-1")
        removed = identity.unregister("buddy", purge=True)
        assert removed["pending"] == 1
        assert not list(identity.inbox_dir("buddy").glob("*.json"))

    @pytest.mark.parametrize("bad", ["", "../escape", "a/b", "buddy@host", ".hidden"])
    def test_rejects_path_traversal_and_nesting(self, isolate, bad):
        with pytest.raises(identity.BuddyError):
            identity.validate_name(bad)

    def test_list_buddies_ignores_agent_sessions(self, isolate):
        identity.register("buddy")
        core.store_session_metadata("real-worker", {"posture": "bypass"})
        assert [b["name"] for b in identity.list_buddies()] == ["buddy"]


# =============================================================================
# Delivery adapter — the drain seam
# =============================================================================


class TestAdapterResolution:
    def test_inert_for_sessions_that_did_not_opt_in(self, isolate):
        """The guarantee that this spike changes nothing shipped."""
        core.store_session_metadata("worker-1", {"posture": "bypass", "roles": []})
        assert delivery.adapter_for("worker-1") is None
        assert delivery.adapter_for("never-seen") is None

    def test_unknown_adapter_falls_through_rather_than_swallowing(self, isolate):
        """A typo must not become a black hole — fall back to tmux delivery."""
        core.store_session_metadata("buddy", {"kind": "voice_layer", "delivery": "typo"})
        assert delivery.adapter_for("buddy") is None


class TestDrainSeam:
    def test_spools_instead_of_pasting(self, isolate, monkeypatch):
        identity.register("buddy")
        inbox.enqueue("buddy", "PR #900 is up", kind="done", sender="worker-1")

        def _never(*a, **k):
            raise AssertionError("safe_deliver must not be reached for an adapter target")

        monkeypatch.setattr("agentwire.prompt_router.safe_deliver", _never)

        result = inbox.flush_session("buddy")
        assert result["delivered"] == 1
        assert result["deferred"] is False
        spooled = delivery.read_spool("buddy")
        assert spooled[0]["text"] == "PR #900 is up"
        assert spooled[0]["kind"] == "done"

    def test_delivered_mail_leaves_the_pending_queue(self, isolate):
        """Never both spooled and pending — that would redeliver forever."""
        identity.register("buddy")
        inbox.enqueue("buddy", "hello", kind="note", sender="worker-1")
        inbox.flush_session("buddy")
        assert inbox.pending_files("buddy") == []

    def test_intercepts_before_the_gone_gate(self, isolate):
        """The bug this seam exists to prevent.

        The buddy is legitimately absent from tmux, so without the adapter the
        gone gate burns ``GONE_MAX_ATTEMPTS`` and dead-letters real reports.
        """
        identity.register("buddy")
        inbox.enqueue("buddy", "worker done", kind="done", sender="worker-1")
        for _ in range(inbox.GONE_MAX_ATTEMPTS + 2):
            result = inbox.flush_session("buddy")
            assert result["reason"] != "target_gone"
        assert inbox.list_dead("buddy") == []
        assert len(delivery.read_spool("buddy", unread_only=False)) == 1

    def test_unadapted_session_still_dead_letters(self, isolate, monkeypatch):
        """Control: the gone gate is untouched for ordinary sessions."""
        monkeypatch.setattr("agentwire.inbox._escalate_dead_letters", lambda *a, **k: None)
        inbox.enqueue("ghost-session", "hello", kind="note", sender="worker-1")
        result = inbox.flush_session("ghost-session")
        assert result["reason"] == "target_gone"

    def test_cohort_hold_still_wins(self, isolate, monkeypatch):
        """``wait --children`` reads reports off disk — never spool them first."""
        identity.register("buddy")
        messages = inbox.enqueue("buddy", "child report", kind="done", sender="child-1")
        monkeypatch.setattr(inbox, "_cohort_held", lambda session, msgs: list(msgs))

        result = inbox.flush_session("buddy")
        assert result["reason"] == "cohort_held"
        assert result["delivered"] == 0
        assert delivery.read_spool("buddy") == []
        assert messages[0].path.exists()

    def test_write_failure_defers_without_losing_mail(self, isolate, monkeypatch):
        identity.register("buddy")
        inbox.enqueue("buddy", "keep me", kind="done", sender="worker-1")
        monkeypatch.setattr(
            delivery, "deliver", lambda s, m: (False, "spool_write_failed: disk full")
        )
        result = inbox.flush_session("buddy")
        assert result["deferred"] is True
        assert len(inbox.pending_files("buddy")) == 1


class TestSpoolCursor:
    def test_unread_advances_only_on_ack(self, isolate):
        identity.register("buddy")
        inbox.enqueue("buddy", "one", kind="note", sender="w")
        inbox.flush_session("buddy")

        assert len(delivery.read_spool("buddy")) == 1
        assert len(delivery.read_spool("buddy")) == 1  # a plain read is not a receipt
        assert len(delivery.read_spool("buddy", ack=True)) == 1
        assert delivery.read_spool("buddy") == []

    def test_ack_does_not_hide_mail_that_arrived_meanwhile(self, isolate):
        identity.register("buddy")
        inbox.enqueue("buddy", "first", kind="note", sender="w")
        inbox.flush_session("buddy")
        delivery.read_spool("buddy", ack=True)

        inbox.enqueue("buddy", "second", kind="note", sender="w")
        inbox.flush_session("buddy")
        assert [m["text"] for m in delivery.read_spool("buddy")] == ["second"]

    def test_truncated_spool_does_not_strand_the_cursor(self, isolate):
        identity.register("buddy")
        inbox.enqueue("buddy", "one", kind="note", sender="w")
        inbox.flush_session("buddy")
        delivery.read_spool("buddy", ack=True)

        delivery.spool_path("buddy").write_text("")  # rotated away
        inbox.enqueue("buddy", "after rotation", kind="note", sender="w")
        inbox.flush_session("buddy")
        assert [m["text"] for m in delivery.read_spool("buddy")] == ["after rotation"]


class TestSendTimeWarning:
    def test_no_false_gone_warning_for_a_buddy(self, isolate, capsys):
        """``msg send`` must not predict a dead-letter that cannot happen."""
        from agentwire import msg_cli

        identity.register("buddy")
        args = SimpleNamespace(to="buddy", text="hi", kind="note", sender="w",
                               ref="", json=True, session=None)
        msg_cli.cmd_msg_send(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload["missing"] == []
        assert payload["warnings"] == []


# =============================================================================
# Tool surface
# =============================================================================


class TestToolAllowlist:
    def test_every_tool_is_read_only_by_construction(self):
        """No tool takes a command, and none names a mutating CLI verb."""
        forbidden = {"kill", "send", "spawn", "new", "worktree_create", "merge",
                     "remove", "prune", "run", "recreate", "fork", "answer"}
        for tool in tools.READ_ONLY_TOOLS:
            props = set(tool.parameters.get("properties", {}))
            assert not props & {"command", "cmd", "args", "argv", "shell"}
            assert not (set(tool.name.split("_")) & forbidden)

    def test_unknown_tool_is_data_not_an_exception(self):
        """A stalled function call kills the conversation; an error can be spoken.

        Slice 1 tightened this: the error is now phrased AS speech and carries
        ``must_speak``, because a refusal the owner never hears is
        indistinguishable from not having been heard at all.
        """
        result = tools.dispatch("rm_rf_everything", {}, "buddy")
        assert result["success"] is False
        assert "don't have a tool called rm_rf_everything" in result["error"]
        assert result["must_speak"] is True
        assert result["say"].strip()

    @pytest.mark.parametrize(
        "bad", ["worker one", "worker;rm -rf /", "--help", "../etc/passwd", "", None, 7]
    )
    def test_garbled_session_name_fails_closed(self, bad):
        """Mis-transcription must refuse, never fuzzy-match onto a real session."""
        result = tools.dispatch("fleet_session_output", {"session": bad}, "buddy")
        assert result["success"] is False
        assert "valid session name" in result["error"]

    def test_valid_session_name_builds_its_own_argv(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "agentwire.voice_layer.tools.run_agentwire_cmd",
            lambda args, **kw: seen.setdefault("args", args) or {"success": True},
        )
        tools.dispatch("fleet_session_output", {"session": "agentwire-spike", "lines": 10}, "b")
        assert seen["args"] == ["output", "-s", "agentwire-spike", "-n", "10"]

    def test_line_count_is_clamped_not_trusted(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "agentwire.voice_layer.tools.run_agentwire_cmd",
            lambda args, **kw: seen.setdefault("args", args) or {"success": True},
        )
        tools.dispatch("fleet_session_output", {"session": "s", "lines": 10 ** 9}, "b")
        assert seen["args"][-1] == str(tools._MAX_OUTPUT_LINES)

    @pytest.mark.parametrize("bad", ["not-a-repo", "a/b/c", "owner/name;id", "--repo x"])
    def test_pr_repo_must_be_owner_slash_name(self, bad):
        result = tools.dispatch("fleet_pull_requests", {"repo": bad}, "buddy")
        assert result["success"] is False

    def test_buddy_inbox_reads_its_own_spool(self, isolate):
        identity.register("buddy")
        inbox.enqueue("buddy", "PR is up", kind="done", sender="worker-1")
        inbox.flush_session("buddy")
        result = tools.dispatch("buddy_inbox", {}, "buddy")
        assert result["count"] == 1
        assert result["messages"][0]["text"] == "PR is up"

    def test_tool_defs_are_valid_realtime_function_entries(self):
        for entry in tools.realtime_tool_defs():
            assert entry["type"] == "function"
            assert entry["name"] and entry["description"]
            assert entry["parameters"]["type"] == "object"


# =============================================================================
# Realtime session shape
# =============================================================================


class TestRealtime:
    def test_model_id_is_the_real_one(self):
        """The owner called it "gpt-voice-2"; the API does not."""
        assert realtime.DEFAULT_MODEL == "gpt-realtime-2.1"

    def test_request_body_matches_the_documented_shape(self):
        body = realtime.build_session_request(
            instructions="be brief", tools=tools.realtime_tool_defs()
        )
        session = body["session"]
        assert session["type"] == "realtime"
        assert session["model"] == realtime.DEFAULT_MODEL
        assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert session["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
        assert session["audio"]["output"]["voice"] == realtime.DEFAULT_VOICE
        assert session["tool_choice"] == "auto"
        # Reads plus the one gated write (Slice 1) — every allowlisted tool.
        assert len(session["tools"]) == len(tools.all_tools())
        assert len(session["tools"]) > len(tools.READ_ONLY_TOOLS)

    def test_client_secret_is_top_level_not_nested(self):
        """The field placement that a prose-docs reading gets wrong."""
        parsed = realtime.parse_session_response(
            {"value": "ek_abc", "expires_at": 123, "session": {"id": "sess_1"}},
            "gpt-realtime-2.1",
        )
        assert parsed["client_secret"] == "ek_abc"
        assert parsed["id"] == "sess_1"

    @pytest.mark.parametrize(
        "payload",
        [{"session": {"id": "sess_1"}}, {"value": "ek_abc"}, {}],
    )
    def test_incomplete_mint_response_raises(self, payload):
        with pytest.raises(realtime.RealtimeError):
            realtime.parse_session_response(payload, "gpt-realtime-2.1")

    def test_missing_api_key_names_the_blessed_location(self, monkeypatch):
        monkeypatch.delenv(realtime.API_KEY_ENV, raising=False)
        with pytest.raises(realtime.RealtimeError, match=r"~/\.agentwire"):
            realtime.api_key()


class TestInstructions:
    def test_states_the_harness_boundary(self):
        text = instructions.build_instructions().lower()
        assert "not one of those agents" in text
        assert "do not write code" in text

    def test_states_its_limits_truthfully_and_the_freshness_rule(self):
        """Slice 1 gave the buddy one write, so "you can only look" became a
        lie. The persona must state the NEW boundary, not the old one."""
        text = instructions.build_instructions()
        assert "LIMITS." in text
        assert "only look" not in text, "stale claim: the buddy can write now"
        assert "cannot start a session" in text
        assert "FRESHNESS." in text
        assert "IDENTITY." in text

    def test_says_queued_never_sent(self):
        """``msg send`` queues; claiming delivery is worse than silence."""
        text = instructions.build_instructions()
        assert 'SAY "QUEUED", NEVER "SENT".' in text

    def test_tells_the_persona_to_speak_every_refusal(self):
        text = instructions.build_instructions()
        assert "NEVER GO SILENT." in text
        assert "must_speak" in text
        assert "owner_should_wait" in text

    def test_volunteers_replies_only_and_still_never_interrupts(self):
        """#962: the one thing raised unprompted is a reply to something the
        buddy sent. The old absolute ("you speak when spoken to") is now false
        — the announcer's clock speaks replies — but general unprompted status
        and interruption stay banned."""
        text = instructions.build_instructions()
        flowed = " ".join(text.split())
        assert "VOLUNTEERING." in text
        assert "You speak when spoken to." not in flowed, (
            "stale absolute: contradicts the reply announcer"
        )
        assert "do not volunteer status the owner did not ask for" in flowed
        assert "never interrupt" in flowed

    def test_a_volunteered_report_is_shape_not_recital(self):
        """The owner's example is 'looks like there are four main options', not
        four paragraphs of options. A monologue the owner did not ask for and
        cannot skim is worse than silence."""
        flowed = " ".join(instructions.build_instructions().split())
        assert "shape of the answer" in flowed
        assert "hand the conversation back" in flowed

    def test_a_volunteered_claim_is_grounded_in_output_actually_read(self):
        """Same confabulation failure the BOUNDARY section closes, and a
        proactive channel is the more dangerous place for it: the owner did
        not ask, so they have no prior to check the claim against. The buddy
        HAS real reading tools (buddy_inbox, fleet_session_output) — which
        makes a plausible unread summary more tempting, not less, so the
        persona must name both the instruments and the ban."""
        flowed = " ".join(instructions.build_instructions().split())
        assert "never what you expect the answer to be" in flowed
        assert "buddy_inbox" in flowed
        assert "fleet_session_output" in flowed
        assert "say only that a reply arrived" in flowed


# =============================================================================
# Bridge server
# =============================================================================


class TestBridge:
    def test_tool_route_dispatches_through_the_allowlist(self, isolate):
        from agentwire.voice_layer import server

        identity.register("buddy")
        bridge = server.BuddyBridge("buddy", "tok")
        assert bridge.tool_call({"name": "nope", "arguments": {}})["success"] is False

    def test_malformed_arguments_do_not_raise(self, isolate):
        from agentwire.voice_layer import server

        bridge = server.BuddyBridge("buddy", "tok")
        assert bridge.tool_call({"name": "fleet_sessions", "arguments": "{{"})[
            "success"
        ] is False

    def test_page_embeds_the_token_as_json(self):
        from agentwire.voice_layer import client

        page = client.page("buddy", 'tok"with-quote')
        assert 'const TOKEN = "tok\\"with-quote"' in page

    def test_default_port_avoids_the_portal(self):
        from agentwire.voice_layer import server

        assert server.DEFAULT_PORT not in (8100, 8765, 8101)
