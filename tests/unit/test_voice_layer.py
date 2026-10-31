"""Tests for the EXPERIMENTAL voice-layer spike (branch-only).

Three things carry real risk and are tested accordingly:

1. **The drain seam.** The adapter must intercept before the gone gate (or the
   buddy's mail dead-letters in ~5 ticks). That ordering is asserted, plus the inertness guarantee for every session that hasn't
   opted in.
2. **The tool allowlist.** Voice adds mis-transcription as a failure mode, so
   the model must never be able to name a command — only pick a tool. A garbled
   session name has to fail closed.
3. **The realtime request shape**, against what the current docs actually say.
"""


import pytest

from agentwire import core, inbox
from agentwire.voice_layer import delivery, identity, realtime, tools


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


    def test_refuses_to_overwrite_a_real_session_record(self, isolate):
        core.store_session_metadata("orchestrator", {"posture": "bypass", "roles": []})
        with pytest.raises(identity.BuddyError, match="refusing to overwrite"):
            identity.register("orchestrator")


# =============================================================================
# Delivery adapter — the drain seam
# =============================================================================


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


    @pytest.mark.parametrize(
        "bad", ["worker one", "worker;rm -rf /", "--help", "../etc/passwd", "", None, 7]
    )
    def test_garbled_session_name_fails_closed(self, bad):
        """Mis-transcription must refuse, never fuzzy-match onto a real session."""
        result = tools.dispatch("fleet_session_output", {"session": bad}, "buddy")
        assert result["success"] is False
        assert "valid session name" in result["error"]


# =============================================================================
# Realtime session shape
# =============================================================================


class TestRealtime:

    def test_request_body_matches_the_documented_shape(self):
        body = realtime.build_session_request(
            instructions="be brief", tools=tools.realtime_tool_defs()
        )
        session = body["session"]
        assert session["type"] == "realtime"
        assert session["model"] == realtime.DEFAULT_MODEL
        assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert session["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
        # The server-side half of barge-in (#1041): stated, not defaulted — a
        # silent default flip would present as "she talks through me".
        assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is True
        assert session["audio"]["output"]["voice"] == realtime.DEFAULT_VOICE
        assert session["tool_choice"] == "auto"
        # Reads plus the one gated write (Slice 1) — every allowlisted tool.
        assert len(session["tools"]) == len(tools.all_tools())
        assert len(session["tools"]) > len(tools.READ_ONLY_TOOLS)


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
