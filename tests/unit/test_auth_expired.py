"""An expired Claude login is detected, named, escalated once, and gated (#906).

Every fixture here is built from the REAL 2026-08-04 incident transcripts, not
from a hand-written string that happens to contain "Login expired". That is
deliberate and it is the point: four bugs shipped past a fully green suite in
one day (#901, #898, #902, #905) because the FIXTURE decided what the suite
could see. The specific traps this file is built to fall into on purpose:

* ``AUTH_ROW`` is the verbatim shape of the assistant row that ended
  ``memory-manager``'s run — ``model: "<synthetic>"``, zero tokens,
  ``error: "authentication_failed"``, ``isApiErrorMessage: true``. A detector
  written against a prettier invented row would pass and still miss it.
* ``MINIMAL_METADATA`` is the verbatim shape of ``memory-manager``'s session
  record: ``created_by`` / ``created_at`` / ``created_via`` / ``role`` and
  NOTHING else. It predates #871's enrichment, so it has no
  ``conversation_ids`` — a detector tested only against modern metadata could
  not have seen the very incident it was written for.
* ``ASSISTANT_TALKING_ABOUT_IT`` is an ordinary assistant turn whose TEXT
  contains the rendered phrase. Any pane-text or substring detector matches
  it. It must not match here, because a false positive gates the whole fleet.
"""

import json
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from agentwire import auth_expired

# --------------------------------------------------------------------------
# Fixtures taken from the real incident
# --------------------------------------------------------------------------

# Verbatim shape of the row that ended memory-manager's 2026-08-04 run
# (~/.claude/projects/-Users-dotdev-projects-agentwire-dev/4f90262b-….jsonl).
AUTH_ROW = {
    "parentUuid": "1cc75620-341b-46e1-8ec0-44a27660a339",
    "isSidechain": False,
    "type": "assistant",
    "uuid": "558c5a0c-eb20-4744-8bca-d77928f1bb35",
    "timestamp": "2026-08-04T08:00:20.800Z",
    "message": {
        "id": "f61807b3-3fb2-4e49-b82f-9e1acb9f135c",
        "model": "<synthetic>",
        "role": "assistant",
        "stop_reason": "stop_sequence",
        "type": "message",
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "content": [{"type": "text", "text": "Login expired · Please run /login"}],
    },
    "error": "authentication_failed",
    "isApiErrorMessage": True,
    "cwd": "/Users/dotdev/projects/agentwire-dev",
    "sessionId": "4f90262b-da59-4ba5-925a-859cdf5f7a30",
    "version": "2.1.221",
    "gitBranch": "main",
}

USER_ROW = {
    "type": "user",
    "timestamp": "2026-08-04T08:00:20.785Z",
    "cwd": "/Users/dotdev/projects/agentwire-dev",
    "message": {"role": "user", "content": "You are the nightly memory manager. …"},
}

TURN_DURATION_ROW = {
    "type": "system",
    "subtype": "turn_duration",
    "durationMs": 16,
    "messageCount": 5,
    "timestamp": "2026-08-04T08:00:20.801Z",
}

HEALTHY_ROW = {
    "type": "assistant",
    "timestamp": "2026-08-04T09:00:00.000Z",
    "message": {
        "model": "claude-opus-5",
        "role": "assistant",
        "content": [{"type": "text", "text": "Reading the audit output now."}],
        "usage": {"input_tokens": 1200, "output_tokens": 40},
    },
}

# The false-positive class a pane-text detector buys: an agent REPORTING on the
# incident. The rendered phrase is right there in the text.
ASSISTANT_TALKING_ABOUT_IT = {
    "type": "assistant",
    "timestamp": "2026-08-04T19:00:00.000Z",
    "message": {
        "model": "claude-opus-5",
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": ("The run died because Claude answered "
                     "'Login expired · Please run /login' with "
                     "error: authentication_failed."),
        }],
    },
}

# A transient upstream error. Retryable — must NOT be treated as an expired
# login, or a blip would gate the whole fleet for OUTAGE_TTL.
OVERLOADED_ROW = {
    "type": "assistant",
    "timestamp": "2026-08-04T08:00:20.800Z",
    "message": {"model": "<synthetic>", "role": "assistant",
                "content": [{"type": "text", "text": "API Error: overloaded"}]},
    "error": "overloaded",
    "isApiErrorMessage": True,
}

# memory-manager's actual metadata.json on 2026-08-05 — no conversation_ids.
MINIMAL_METADATA = {
    "created_by": "agentwire-scheduler",
    "created_at": "2026-08-05T08:00:10.648562+00:00",
    "created_via": "new",
    "role": "orchestrator",
}

HUNG_RUN = [
    {"type": "mode", "mode": "normal"},
    {"type": "permission-mode", "permissionMode": "bypassPermissions"},
    {"type": "file-history-snapshot"},
    USER_ROW,
    {"type": "attachment", "attachment": {"type": "deferred_tools_delta"}},
    AUTH_ROW,
    TURN_DURATION_ROW,
    {"type": "last-prompt", "lastPrompt": "You are the nightly memory manager. …"},
]


def write_transcript(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate CONFIG_DIR and Claude's projects dir into tmp_path."""
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    monkeypatch.setattr("agentwire.history.PROJECTS_DIR", tmp_path / "projects")
    return tmp_path


# --------------------------------------------------------------------------
# Detection, against the real row
# --------------------------------------------------------------------------


class TestDetectsTheRealFailingState:
    def test_the_real_08_04_transcript_fires(self, tmp_path):
        """The load-bearing assertion: the exact run that cost 7217s is seen."""
        p = write_transcript(tmp_path / "4f90262b.jsonl", HUNG_RUN)
        assert auth_expired.transcript_auth_failure(p) is True

    def test_recovery_needs_no_reset_mechanism(self, tmp_path):
        """A later real turn makes it healthy — the documentscribe shape.

        Two of the four 08-04 transcripts auth-failed MID-file and recovered.
        Keying on 'does the file contain an auth failure' would gate the fleet
        on every one of them, forever.
        """
        p = write_transcript(tmp_path / "recovered.jsonl", HUNG_RUN + [HEALTHY_ROW])
        assert auth_expired.transcript_auth_failure(p) is False

    def test_an_agent_describing_the_error_is_not_the_error(self, tmp_path):
        """The false positive a pane/substring detector cannot avoid."""
        p = write_transcript(tmp_path / "talking.jsonl", [ASSISTANT_TALKING_ABOUT_IT])
        assert auth_expired.transcript_auth_failure(p) is False

    def test_other_api_errors_are_not_widened_into_this_one(self, tmp_path):
        p = write_transcript(tmp_path / "overloaded.jsonl", [OVERLOADED_ROW])
        assert auth_expired.transcript_auth_failure(p) is False

    def test_no_assistant_turn_at_all(self, tmp_path):
        p = write_transcript(tmp_path / "empty.jsonl", [USER_ROW])
        assert auth_expired.transcript_auth_failure(p) is False

    def test_missing_file_is_not_an_outage(self, tmp_path):
        assert auth_expired.transcript_auth_failure(tmp_path / "nope.jsonl") is False

    def test_tail_read_survives_a_multi_megabyte_transcript(self, tmp_path):
        """The real files are 2-11MB; only the tail can hold the last turn.

        Also proves the truncated first line of a mid-file read is dropped
        rather than exploding the scan.
        """
        filler = {"type": "assistant", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "x" * 400}]}}
        rows = [filler] * 3000 + [AUTH_ROW]
        p = write_transcript(tmp_path / "big.jsonl", rows)
        assert p.stat().st_size > auth_expired.TAIL_BYTES
        assert auth_expired.transcript_auth_failure(p) is True


# --------------------------------------------------------------------------
# Locating the transcript — both routes, including the incident's own shape
# --------------------------------------------------------------------------


class TestLocatingTheTranscript:
    def _place(self, env, cwd: str, conv_id: str, rows: list[dict]) -> Path:
        from agentwire.history import encode_project_path

        return write_transcript(
            env / "projects" / encode_project_path(cwd) / f"{conv_id}.jsonl", rows
        )

    def test_recorded_conversation_id_addresses_the_file(self, env):
        from agentwire.core import store_session_metadata

        cwd = "/Users/dotdev/projects/agentwire-dev"
        self._place(env, cwd, "conv-1", HUNG_RUN)
        store_session_metadata("memory-manager", {
            "cwd_at_launch": cwd, "conversation_ids": ["conv-1"],
        })
        from agentwire.history import encode_project_path

        expected = env / "projects" / encode_project_path(cwd) / "conv-1.jsonl"
        assert auth_expired.detect("memory-manager") == {
            "session": "memory-manager",
            "transcript": str(expected),
            "source": "recorded",
        }

    def test_memory_manager_shaped_record_still_detects(self, env):
        """The incident's OWN metadata shape: no conversation_ids at all.

        `memory-manager`'s record was written by a build that predated #871's
        enrichment. A detector that only handled the recorded path would have
        been blind to the exact run it exists for — the fixture trap, in the
        implementation rather than the test.
        """
        from agentwire.core import store_session_metadata

        cwd = "/Users/dotdev/projects/agentwire-dev"
        store_session_metadata("memory-manager", dict(MINIMAL_METADATA))
        assert auth_expired.recorded_transcripts("memory-manager") == []

        self._place(env, cwd, "conv-unknown", HUNG_RUN)
        detail = auth_expired.detect("memory-manager", project_path=cwd, since=0)
        assert detail is not None
        assert detail["source"] == "touched"

    def test_no_session_record_at_all(self, env):
        assert auth_expired.recorded_transcripts("never-existed") == []
        assert auth_expired.detect("never-existed") is None

    def test_a_transcript_older_than_the_run_does_not_count(self, env):
        """Scoped to files written DURING this run, not 'the newest one'.

        Otherwise last week's resolved outage gates today's dispatch.
        """
        cwd = "/Users/dotdev/projects/agentwire-dev"
        p = self._place(env, cwd, "stale", HUNG_RUN)
        import os
        old = time.time() - 86400
        os.utime(p, (old, old))
        assert auth_expired.detect("s", project_path=cwd, since=time.time() - 60) is None
        assert auth_expired.detect("s", project_path=cwd, since=0) is not None


# --------------------------------------------------------------------------
# Outage state: escalate once, and never wedge the board
# --------------------------------------------------------------------------


class TestOutageState:
    def test_first_detection_escalates_and_repeats_do_not(self, env):
        ok = type("R", (), {"success": True, "error": None})()
        with patch("agentwire.channels.email.send_email", return_value=ok) as mail:
            auth_expired.record_outage({"session": "a", "transcript": "/t"})
            auth_expired.record_outage({"session": "b", "transcript": "/t"})
        assert mail.call_count == 1, "one outage, one email — not one per task"
        state = auth_expired.read_state()
        assert state["sessions"] == ["a", "b"]

    def test_escalation_resumes_after_the_ttl(self, env):
        ok = type("R", (), {"success": True, "error": None})()
        with patch("agentwire.channels.email.send_email", return_value=ok) as mail:
            auth_expired.record_outage({"session": "a"})
            state = auth_expired.read_state()
            stale = auth_expired._now() - auth_expired.ESCALATE_TTL - timedelta(minutes=1)
            state["escalated_at"] = stale.isoformat()
            auth_expired.write_state(state)
            auth_expired.record_outage({"session": "a"})
        assert mail.call_count == 2

    def test_detected_at_is_carried_forward_not_refreshed(self, env):
        """A four-hour outage must not read as seconds old (the #905 defect)."""
        ok = type("R", (), {"success": True, "error": None})()
        with patch("agentwire.channels.email.send_email", return_value=ok):
            first = auth_expired.record_outage({"session": "a"})
            auth_expired.record_outage({"session": "a"})
        assert auth_expired.read_state()["detected_at"] == first["detected_at"]

    def test_a_failing_escalation_still_records_the_outage(self, env):
        """The gate must work with or without the email."""
        with patch("agentwire.channels.email.send_email", side_effect=RuntimeError("no key")):
            auth_expired.record_outage({"session": "a"})
        assert auth_expired.read_state() is not None
        assert auth_expired.outage_active() is not None

    def test_a_stale_outage_stops_gating(self, env):
        """Bounded on purpose: a flag that gated forever takes the fleet down."""
        ok = type("R", (), {"success": True, "error": None})()
        with patch("agentwire.channels.email.send_email", return_value=ok):
            auth_expired.record_outage({"session": "a"})
        assert auth_expired.outage_active() is not None
        state = auth_expired.read_state()
        state["last_seen"] = (
            auth_expired._now() - auth_expired.OUTAGE_TTL - timedelta(minutes=1)
        ).isoformat()
        auth_expired.write_state(state)
        assert auth_expired.outage_active() is None, "must reopen for a probe"

    def test_a_corrupt_state_file_does_not_gate(self, env):
        auth_expired.state_path().parent.mkdir(parents=True, exist_ok=True)
        auth_expired.state_path().write_text("{ not json")
        assert auth_expired.read_state() is None
        assert auth_expired.outage_active() is None

    def test_no_state_is_no_outage(self, env):
        assert auth_expired.outage_active() is None
