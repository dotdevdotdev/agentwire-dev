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
* ``NOT_LOGGED_IN_ROW`` is the SECOND real outage (2026-07-07), verbatim: the
  same ``error`` value rendered as different user-facing text on a different
  Claude Code version. #867's own fixtures could never have produced it, and
  it is the direct evidence for keying on the structured field.
* ``RATE_LIMIT_ROW`` is a real rate-limit refusal, verbatim. It shares every
  structural property with an auth refusal, so it reaches the predicate —
  only the error-value check stops it. There are 16 of these on disk against
  6 auth rows, so this is the likelier of the two to be met in the wild.
"""

import json
from pathlib import Path

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

# The SECOND real outage, 2026-07-07 — same `error`, DIFFERENT user-facing
# text ("Not logged in", not "Login expired"). Verbatim off disk, Claude Code
# 2.1.201 vs 2.1.221 for the 08-04 rows. This is the fixture the 08-04
# incident could never have produced, and it is the whole argument for keying
# on the structured `error` field instead of the rendered string: a detector
# matching "Login expired" would silently stop working on a rewording, which
# has already happened once in this codebase's own history.
NOT_LOGGED_IN_ROW = {
    "type": "assistant",
    "uuid": "d102699e-c812-415c-9609-307fef8ef94f",
    "timestamp": "2026-07-07T21:34:28.006Z",
    "message": {
        "model": "<synthetic>",
        "role": "assistant",
        "stop_reason": "stop_sequence",
        "type": "message",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "content": [{"type": "text", "text": "Not logged in · Please run /login"}],
    },
    "error": "authentication_failed",
    "isApiErrorMessage": True,
    "cwd": "/Users/dotdev/projects/documentscribe",
    "sessionId": "02315590-969e-4621-832f-c63b6aa8211e",
    "version": "2.1.201",
}

# A REAL rate-limit refusal, verbatim. Structurally identical to the auth rows
# — ``type: assistant``, ``model: <synthetic>``, zero tokens,
# ``isApiErrorMessage: true`` — so it reaches the predicate and only the
# error-value check stops it. There are 16 of these on disk against 6 auth
# rows; treating them alike would gate the entire machine on a transient blip
# that the usage-limit subsystem already handles correctly.
RATE_LIMIT_ROW = {
    "type": "assistant",
    "uuid": "28754c4f-e6eb-45b4-ad96-2a2f1a89693b",
    "timestamp": "2026-07-18T18:00:40.382Z",
    "message": {
        "model": "<synthetic>",
        "role": "assistant",
        "stop_reason": "stop_sequence",
        "type": "message",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "content": [{
            "type": "text",
            "text": "You've hit your session limit · resets 4:40pm (America/Toronto)",
        }],
    },
    "error": "rate_limit",
    "isApiErrorMessage": True,
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
    monkeypatch.setattr("agentwire.history.claude_projects_dir", lambda: tmp_path / "projects")
    return tmp_path


# --------------------------------------------------------------------------
# Detection, against the real row
# --------------------------------------------------------------------------


class TestDetectsTheRealFailingState:
    def test_a_later_real_turn_means_recovered(self, tmp_path):
        p = write_transcript(tmp_path / "recovered.jsonl", HUNG_RUN + [HEALTHY_ROW])
        assert auth_expired.transcript_auth_failure(p) is False

    def test_the_real_08_04_transcript_fires(self, tmp_path):
        """The load-bearing assertion: the exact run that cost 7217s is seen."""
        p = write_transcript(tmp_path / "4f90262b.jsonl", HUNG_RUN)
        assert auth_expired.transcript_auth_failure(p) is True

    def test_an_agent_describing_the_error_is_not_the_error(self, tmp_path):
        """The false positive a pane/substring detector cannot avoid."""
        p = write_transcript(tmp_path / "talking.jsonl", [ASSISTANT_TALKING_ABOUT_IT])
        assert auth_expired.transcript_auth_failure(p) is False

    def test_a_real_rate_limit_refusal_does_not_gate_the_machine(self, tmp_path):
        """The near-miss: structurally identical, and 16 of them on disk.

        `type: assistant`, `model: <synthetic>`, zero tokens,
        `isApiErrorMessage: true` — every property except the error value is
        shared with an auth refusal, so this row DOES reach the predicate.
        Only the error check stops a transient rate limit (which usage-limit
        recovery already handles) from gating every scheduled task.
        """
        p = write_transcript(tmp_path / "ratelimit.jsonl", [RATE_LIMIT_ROW])
        assert auth_expired.transcript_auth_failure(p) is False


# --------------------------------------------------------------------------
# Locating the transcript — both routes, including the incident's own shape
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Outage state: escalate once, and never wedge the board
# --------------------------------------------------------------------------


