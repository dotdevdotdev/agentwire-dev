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

import ast
import itertools
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

def mcp_tool_names(sources: "list[str] | None" = None) -> set[str]:
    r"""Every ``@mcp.tool`` name in ``agentwire/mcp_*.py``.

    Delegates to :func:`mcp_tool_defs` rather than running its own regex, and
    that consolidation is a bug fix rather than tidying. The regex it replaced
    was ``@mcp\.tool\([^)]*\)\s*\ndef\s+(\w+)`` — it required the ``def``
    to be the NEXT line, so a tool wearing a second decorator became invisible
    to the audit. It went unnoticed because nothing here stacked one until the
    beta gate did (``@mcp.tool()`` / ``@gated_doc`` / ``def msg_send``), and
    the direction that hid it is the dangerous one: a tool the audit cannot
    see is a tool nobody is required to place in a tier.

    **Scope of the regex era, measured, so nobody re-opens it as a shipped
    gap:** it missed ZERO tools on ``origin/main``, zero on the spine base and
    zero before a stacked decorator existed — one tool (``msg_send``), in one
    tree, introduced and fixed in the same change.

    **Recorded limitation, deliberately not fixed:** this walks the parsed
    module's TOP-LEVEL body, so a tool defined inside a function or an ``if``
    block is invisible to it. Neither shape exists in the package today, and
    both are worth catching if one ever lands — a conditionally-defined tool is
    exactly the kind that would want a tier ruling most.
    """
    names = set(mcp_tool_defs(sources))
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


#: Arguments that satisfy every read tool's schema, so one sweep can capture
#: what each one actually dispatches. Extra keys are ignored by tools that do
#: not take them.


#: The ONE wired mapping the argv cross-check cannot corroborate, recorded as
#: a (tool, capability) PAIR and asserted set-equal. Scoping the exemption to
#: the capability NAME instead covered all 15 capabilities that build no
#: extractable argv — including `wiki_lint` and `wiki_status`, both tier READ,
#: so a future tool mapped to either would have been graded read with nothing
#: able to contradict it. An exemption that widens by itself is the failure
#: mode this whole file exists to make impossible.


class TestEveryWiredToolIsRuled:
    """#979/5: the tier audit swept ``@mcp.tool`` names — a namespace that is
    not the exposed surface. ``buddy_inbox``, ``buddy_sent`` and
    ``project_lookup`` have no MCP capability behind them, so 'EVERY tool
    appears in exactly one tier' was true and beside the point: the next
    voice-native tool could ship with nothing forcing a grade out of anyone.
    Concretely ``buddy_inbox(ack=true)`` mutates state from the read-only
    allowlist."""


    def test_no_wired_tool_maps_to_an_excluded_capability(self):
        """The by-name check catches `propose_worktree_create`; it cannot catch
        a wired read whose CAPABILITY is excluded under an unrelated name —
        which is precisely the scheduler_report shape (#979/3)."""
        wired = {t.name for t in tools.READ_ONLY_TOOLS} | {
            f"send_{s.name}" for s in write_tools.WRITE_SPECS
        }
        for name in wired:
            for capability in surface.TOOL_CAPABILITY.get(name, ()):
                assert capability not in surface.TIER_EXCLUDED, (
                    f"{name} wires excluded capability {capability}"
                )


# =============================================================================
# 2b. No tiered-in tool's dispatch path can create a session
# =============================================================================
#
# The wave-3 lesson: `task_run` and `scheduler_run` sat GATED while both
# dispatch through `agentwire ensure` — which creates the session when it is
# missing and then drives it — clause (a) under names that don't look like it.
# A by-name exclusion list cannot catch the next one, so this analyzer keys on
# the DISPATCH PATH: it walks every MCP tool's argv into the CLI registrars,
# resolves the handler function, and asks the package-wide call graph whether
# that handler can reach session creation. The creation markers are the SSOT
# helpers themselves (``build_agent_command``, ``create_and_register_worktree``
# — CLAUDE.md: every launch site routes through them) plus a spawned
# ``["agentwire", "ensure", ...]`` subprocess (the scheduler's dispatch shape).


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def mcp_tool_defs(sources: "list[str] | None" = None) -> dict[str, ast.AST]:
    """tool name -> its ``@mcp.tool``-decorated function node.

    *sources* replaces the packaged ``mcp_*.py`` modules with literal source
    strings, which is how the must-fail controls below exercise shapes the
    real package does not currently contain.
    """
    if sources is None:
        package_root = Path(tools.__file__).resolve().parents[1]
        sources = [p.read_text() for p in package_root.glob("mcp_*.py")]
    out: dict[str, ast.AST] = {}
    for source in sources:
        for node in ast.parse(source).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any((isinstance(d, ast.Call) and _call_name(d) == "tool")
                       or (isinstance(d, ast.Attribute) and d.attr == "tool")
                       for d in node.decorator_list):
                continue
            out[node.name] = node
    return out


#: Dispatches that reach a creating HANDLER in a mode that cannot create:
#: handler -> the mode flags that select its non-creating branches. Keyed on
#: the argv shape (the dispatch), never on the MCP tool's name — a new tool
#: hitting cmd_worktree without one of these flags still fails the check.

#: MCP tools the dispatch-path analyzer extracts NO argv from, and therefore
#: never checked — each one placed in its tier by hand, by reading it. They
#: reach their work through a Python API rather than a CLI argv: the desktop
#: family writes the portal's window state, the wiki family calls
#: ``agentwire.wiki`` directly, ``notify_user``/``transcribe`` go through the
#: portal and the STT backend, ``desktop_write_artifact`` writes a file.
#: Adding to this set is a claim that a human looked; the leg that asserts it
#: is the thing stopping a new tool from being unchecked AND unnoticed.


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


# =============================================================================
# The expanded reads: each one builds exactly its own argv
# =============================================================================


class TestExpandedReads:
    @pytest.fixture
    def seen(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "agentwire.voice_layer.tools.run_agentwire_cmd",
            lambda argv, **kw: calls.append(list(argv)) or {"success": True},
        )
        return calls


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


# =============================================================================
# Wave-2 prose: the premise a repair would argue from
# =============================================================================
