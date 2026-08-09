"""#966: the tier audit, the declared-write mechanism, and the expanded reads.

Three properties, each asserted structurally rather than by inspection:

1. **The audit cannot drift.** Every ``@mcp.tool`` name in ``agentwire/mcp_*.py``
   must appear in exactly one tier in ``voice_layer.surface`` — parsed from the
   source at test time, so a new MCP tool fails this file until someone places
   it, and a removed one fails until its tier entry goes too.
2. **Tier 3 is unreachable BY NAME.** The harness boundary (#730) and the
   other exclusion clauses are asserted against the live realtime surface, not
   established by reading the diff.
3. **A write is a declaration.** ``gated_triple`` turns a :class:`WriteSpec`
   into a working propose/confirm/cancel path with the spine's invariants —
   proven here with a spec that exists only in this test, including the
   argv-only (``append_body=False``) shape no shipped spec uses yet.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from agentwire.voice_layer import confirm, surface, tools, transcript, write_tools
from agentwire.voice_layer.write_tools import FrozenWrite, WriteSpec, gated_triple

# =============================================================================
# Harness (mirrors test_voice_confirm's, minimally)
# =============================================================================


class RecordingRunner:
    def __init__(self, result=None):
        self.calls: list[list[str]] = []
        self._result = result if result is not None else {"success": True}

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self._result


class Conversation:
    _ids = itertools.count()

    def __init__(self, ring, spine):
        self.ring = ring
        self.spine = spine
        self.seq = 0

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def says(self, text):
        item_id = f"item_{next(self._ids)}"
        self.ring.speech_started(item_id, self._next())
        self.ring.commit(item_id, self._next())
        self.ring.transcribe(item_id, text)
        return item_id


def _spine():
    ring = transcript.TranscriptRing()
    runner = RecordingRunner()
    spine = confirm.ConfirmSpine(ring, wait_s=0.0, runner=runner)
    return Conversation(ring, spine), runner


# =============================================================================
# 1. The tier audit cannot drift
# =============================================================================

_MCP_TOOL_RE = re.compile(r"@mcp\.tool\([^)]*\)\s*\ndef\s+(\w+)")


def mcp_tool_names() -> set[str]:
    package_root = Path(tools.__file__).resolve().parents[1]
    names: set[str] = set()
    for module in package_root.glob("mcp_*.py"):
        names |= {m.group(1) for m in _MCP_TOOL_RE.finditer(module.read_text())}
    assert names, "found no @mcp.tool definitions — the parse itself broke"
    return names


class TestTierAudit:
    def test_every_mcp_tool_is_tiered(self):
        """A new MCP tool fails here until someone places it in a tier."""
        parsed = mcp_tool_names()
        tiered = frozenset().union(*surface.ALL_TIERS)
        assert parsed - tiered == set(), (
            f"untiered MCP tools — place them in voice_layer/surface.py: "
            f"{sorted(parsed - tiered)}"
        )

    def test_no_tier_entry_names_a_ghost(self):
        """The reverse direction: a tier entry for a deleted tool is drift too."""
        parsed = mcp_tool_names()
        tiered = frozenset().union(*surface.ALL_TIERS)
        assert tiered - parsed == set(), (
            f"tiered names with no MCP tool behind them: {sorted(tiered - parsed)}"
        )

    def test_tiers_are_disjoint(self):
        for a, b in itertools.combinations(surface.ALL_TIERS, 2):
            assert a & b == set(), f"names in two tiers: {sorted(a & b)}"

    def test_tier_of_covers_the_taxonomy(self):
        assert surface.tier_of("sessions_list") == "read"
        assert surface.tier_of("desktop_focus_window") == "write_light"
        assert surface.tier_of("msg_send") == "write_gated"
        assert surface.tier_of("session_create") == "excluded"
        assert surface.tier_of("no_such_capability") == "untiered"


class TestTierThreeIsUnreachableByName:
    """The design decision, asserted — not inferred from an absence."""

    def test_the_harness_boundary_names_are_excluded(self):
        for name in ("session_create", "worktree_create", "pane_spawn",
                     "session_fork", "session_send", "pane_send"):
            assert name in surface.TIER_EXCLUDED, name

    def test_no_excluded_capability_is_on_the_realtime_surface(self):
        exposed = {t["name"] for t in tools.realtime_tool_defs()}
        assert surface.TIER_EXCLUDED & exposed == set()
        # Aliased exposure counts too: a voice tool named after an excluded
        # capability (fleet_session_create, propose_worktree_create) is the
        # same hole with a prefix.
        for excluded in surface.TIER_EXCLUDED:
            for name in exposed:
                assert not name.endswith(excluded), (
                    f"{name} exposes excluded capability {excluded}"
                )

    def test_every_wired_write_is_tiered_gated(self):
        """A shipped WriteSpec must correspond to a TIER_WRITE_GATED capability."""
        capability_of = {"session_message": "msg_send"}
        for spec in write_tools.WRITE_SPECS:
            capability = capability_of.get(spec.name)
            assert capability is not None, (
                f"WriteSpec {spec.name} has no declared capability mapping — "
                "add it here so its tier is auditable"
            )
            assert capability in surface.TIER_WRITE_GATED

    def test_every_wired_read_observes_a_tiered_read(self):
        """No read tool may be named after a write or excluded capability."""
        writes_and_excluded = (
            surface.TIER_WRITE_LIGHT | surface.TIER_WRITE_GATED | surface.TIER_EXCLUDED
        )
        for tool in tools.READ_ONLY_TOOLS:
            for capability in writes_and_excluded:
                assert not tool.name.endswith(capability), (
                    f"read tool {tool.name} shadows non-read capability {capability}"
                )


# =============================================================================
# 3. A write is a declaration
# =============================================================================

PROBE_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def probe_spec(**overrides) -> WriteSpec:
    fields = dict(
        name="probe_action",
        action="poking the probe",
        params_schema=PROBE_SCHEMA,
        freeze=lambda args: FrozenWrite(
            session="target",
            instruction="poke target",
            argv_prefix=("info", "-s", "target"),
            append_body=False,
        ),
        announce_template="Ready to poke {session}. To approve, say {phrase}.",
        fallback_template=(
            "Ready to poke {session}. Ask me for the code word when you want it."
        ),
        success_say="Done — poked it.",
    )
    fields.update(overrides)
    return WriteSpec(**fields)


class TestDeclaredWriteMechanism:
    def _mint(self, spec):
        convo, runner = _spine()
        propose = gated_triple(spec)[0][3]
        result = propose({"_buddy": "buddy"}, convo.spine)
        convo.spine.announce(result["proposal_id"], convo._next())
        return result, convo, runner

    def test_a_spec_generates_a_complete_named_triple(self):
        triple = gated_triple(probe_spec())
        assert [t[0] for t in triple] == [
            "propose_probe_action", "send_probe_action", "cancel_probe_action",
        ]
        for _name, description, schema, fn in triple:
            assert description.strip() and callable(fn)
            assert schema["type"] == "object"

    def test_argv_only_write_executes_exactly_the_frozen_argv(self):
        """append_body=False: the prefix IS the argv — nothing appended, ever."""
        spec = probe_spec()
        result, convo, runner = self._mint(spec)
        convo.says(f"confirm {result['confirm_phrase'].split()[1]}")
        send = gated_triple(spec)[1][3]
        verdict = send({"confirm_token": result["confirm_token"]}, convo.spine)
        assert runner.calls == [["info", "-s", "target"]]
        assert verdict["success"] is True
        assert verdict["reason"] == "done"
        assert verdict["say"] == "Done — poked it."
        # The completes-now claim carries neither of the queue-shaped keys.
        assert "queued" not in verdict and "sent" not in verdict

    def test_the_msg_write_still_appends_the_rendered_body(self):
        """The default (append_body=True) path did not regress in the migration."""
        convo, runner = _spine()
        proposal = convo.spine.propose(
            tool="send_session_message",
            session="orchestrator",
            instruction="restart the portal",
            argv_prefix=["msg", "send", "--to", "orchestrator", "--from", "buddy",
                         "--kind", write_tools.WRITE_KIND],
        )
        argv = proposal.build_argv()
        assert argv[:2] == ["msg", "send"]
        assert argv[-1].startswith(confirm.VOICE_MARKER)

    def test_a_declared_write_is_single_use(self):
        spec = probe_spec()
        result, convo, runner = self._mint(spec)
        convo.says(f"confirm {result['confirm_phrase'].split()[1]}")
        send = gated_triple(spec)[1][3]
        send({"confirm_token": result["confirm_token"]}, convo.spine)
        replay = send({"confirm_token": result["confirm_token"]}, convo.spine)
        assert replay["success"] is False
        assert replay["reason"] == "replayed"
        assert runner.calls == [["info", "-s", "target"]]

    def test_cancel_retires_without_writing(self):
        spec = probe_spec()
        result, convo, runner = self._mint(spec)
        cancel = gated_triple(spec)[2][3]
        outcome = cancel({"confirm_token": result["confirm_token"]}, convo.spine)
        assert outcome["success"] is False
        assert runner.calls == []
        convo.says(f"confirm {result['confirm_phrase'].split()[1]}")
        send = gated_triple(spec)[1][3]
        assert send({"confirm_token": result["confirm_token"]}, convo.spine)[
            "success"] is False

    def test_a_fallback_template_carrying_the_phrase_cannot_be_declared(self):
        """The echo-safety property (#950) holds at declaration time."""
        with pytest.raises(ValueError, match="fallback"):
            probe_spec(
                fallback_template="Ready. To approve, say {phrase}."
            )

    def test_the_fallback_never_carries_the_nonce(self):
        result, _convo, _runner = self._mint(probe_spec())
        nonce_word = result["confirm_phrase"].split()[1]
        assert nonce_word not in result["fallback_say"]

    def test_shipped_specs_pass_the_same_declaration_guards(self):
        for spec in write_tools.WRITE_SPECS:
            assert "{phrase}" not in spec.fallback_template
            assert spec.params_schema.get("additionalProperties") is False

    def test_confirm_terminal_marks_exactly_the_handshake_enders(self):
        """The name-independent key the client's confirm gate can move to."""
        spec = probe_spec()
        result, convo, _runner = self._mint(spec)
        send = gated_triple(spec)[1][3]
        # No utterance at all → pending_transcript → the handshake stays open.
        waiting = send({"confirm_token": result["confirm_token"]}, convo.spine)
        assert waiting["reason"] == "pending_transcript"
        assert waiting["confirm_terminal"] is False
        convo.says(f"confirm {result['confirm_phrase'].split()[1]}")
        done = send({"confirm_token": result["confirm_token"]}, convo.spine)
        assert done["success"] is True
        assert done["confirm_terminal"] is True


# =============================================================================
# The expanded reads: each one builds exactly its own argv
# =============================================================================

ARGV_CASES = [
    ("fleet_session_info", {"session": "agentwire-dev"},
     ["info", "-s", "agentwire-dev"]),
    ("fleet_scheduler_status", {}, ["scheduler", "status"]),
    ("fleet_scheduler_history", {}, ["scheduler", "history", "--json"]),
    ("fleet_scheduler_live", {}, ["scheduler", "live", "--json"]),
    ("fleet_tasks", {}, ["task", "list"]),
    ("fleet_tasks", {"session": "proj"}, ["task", "list", "proj"]),
    ("fleet_machines", {}, ["machine", "list"]),
    ("fleet_services", {}, ["services", "status"]),
    ("fleet_history", {}, ["history", "list", "-n", "20"]),
    ("fleet_locks", {}, ["lock", "list"]),
    ("fleet_portal", {}, ["portal", "status"]),
    ("fleet_councils", {}, ["council", "list"]),
    ("fleet_wiki_search", {"query": "tmux rename"},
     ["wiki", "query", "tmux rename"]),
    ("fleet_session_inbox", {"session": "worker-1"},
     ["msg", "inbox", "-s", "worker-1"]),
    ("fleet_roles", {}, ["roles", "list"]),
    ("fleet_network", {}, ["network", "status"]),
]


class TestExpandedReads:
    @pytest.fixture
    def seen(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "agentwire.voice_layer.tools.run_agentwire_cmd",
            lambda argv, **kw: calls.append(list(argv)) or {"success": True},
        )
        return calls

    @pytest.mark.parametrize("name,args,expected", ARGV_CASES)
    def test_each_read_builds_its_own_argv(self, seen, name, args, expected):
        result = tools.dispatch(name, args, "buddy")
        assert result.get("success") is not False, result
        assert seen == [expected]

    def test_voice_health_reads_both_backends(self, seen):
        result = tools.dispatch("fleet_voice_health", {}, "buddy")
        assert result["success"] is True
        assert seen == [["tts", "status"], ["stt", "status"]]

    @pytest.mark.parametrize(
        "name", ["fleet_session_info", "fleet_session_inbox", "fleet_tasks"]
    )
    @pytest.mark.parametrize("bad", ["--help", "../etc/passwd", "worker one", ""])
    def test_garbled_session_names_fail_closed_everywhere(self, seen, name, bad):
        result = tools.dispatch(name, {"session": bad}, "buddy")
        assert result["success"] is False
        assert "valid session name" in result["error"]
        assert seen == []

    def test_wiki_query_cannot_reach_the_cli_as_a_flag(self, seen):
        tools.dispatch("fleet_wiki_search", {"query": "--rm -rf everything"}, "b")
        assert seen and seen[0][:2] == ["wiki", "query"]
        assert not seen[0][2].startswith("-")

    def test_wiki_query_is_stripped_and_bounded(self, seen):
        tools.dispatch(
            "fleet_wiki_search", {"query": "a\x1b[2Jb" + "c" * 500}, "b"
        )
        value = seen[0][2]
        assert "\x1b" not in value
        assert len(value) <= tools._MAX_QUERY_CHARS

    def test_an_empty_query_is_refused_with_speech(self, seen):
        result = tools.dispatch("fleet_wiki_search", {"query": "  ---  "}, "b")
        assert result["success"] is False
        assert result["must_speak"] is True
        assert seen == []
