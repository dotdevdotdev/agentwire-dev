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
from types import SimpleNamespace

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

    def test_a_destructive_consume_is_gated_not_light(self):
        """B2 of the wave-3 review: msg_pull takes a session param and reads
        AND REMOVES that session's ingest messages — no one-action undo. The
        light grade came from the name reading like a fetch; the grade keys
        on the effect."""
        assert surface.tier_of("msg_pull") == "write_gated"

    def test_tier_of_covers_the_taxonomy(self):
        assert surface.tier_of("sessions_list") == "read"
        assert surface.tier_of("desktop_focus_window") == "write_light"
        assert surface.tier_of("msg_send") == "write_gated"
        assert surface.tier_of("session_create") == "excluded"
        assert surface.tier_of("no_such_capability") == "untiered"

    def test_a_report_that_authors_an_artifact_is_not_a_read(self):
        """#979/3: scheduler_report sat in TIER_READ while it writes an HTML
        artifact into ~/.agentwire/artifacts/ and can push a click-to-open
        portal notification — clause (d) and clause (b). 'Expand reads freely'
        would have wired it confirm-free on the strength of the word report."""
        assert surface.tier_of("scheduler_report") == "excluded"

    def test_a_detach_that_creates_a_session_is_excluded_not_gated(self):
        """#979/3: pane_detach's own docstring says the target session is
        'created if doesn't exist' — clause (a), and the created session has
        no #871 metadata record. The dispatch-path analyzer cannot see it
        (no build_agent_command on that path), so the tier entry is the only
        guard and a nonce is not the right one."""
        assert surface.tier_of("pane_detach") == "excluded"

    def test_each_reclassification_carries_a_written_ruling(self):
        """A bare tier move is not a ruling. surface.py is the precedent
        store, so the reason must be readable in the module that holds the
        decision — a future reader hits the docstring, not this test."""
        doc = surface.__doc__ or ""
        for name in ("scheduler_report", "pane_detach"):
            assert name in doc, f"{name} moved tier with no written ruling"


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
        for spec in write_tools.WRITE_SPECS:
            capabilities = surface.TOOL_CAPABILITY.get(f"send_{spec.name}")
            assert capabilities, (
                f"WriteSpec {spec.name} has no declared capability mapping — "
                "add it to surface.TOOL_CAPABILITY so its tier is auditable"
            )
            for capability in capabilities:
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


#: Arguments that satisfy every read tool's schema, so one sweep can capture
#: what each one actually dispatches. Extra keys are ignored by tools that do
#: not take them.
_SWEEP_ARGS = {"session": "sess", "repo": "owner/name", "query": "q"}


def _argv_compatible(voice_argv: list, mcp_argv: list) -> bool:
    """Does *mcp_argv* describe the same CLI call as *voice_argv*?

    Compared as a prefix with ``None`` (a non-constant element in the MCP
    source) as a wildcard: the MCP tool interpolates its parameters where the
    voice tool interpolates validated ones, and the leading verbs are what
    identify the capability.
    """
    if not mcp_argv or not voice_argv:
        return False
    width = min(len(mcp_argv), len(voice_argv))
    return all(
        mcp_argv[i] is None or mcp_argv[i] == voice_argv[i] for i in range(width)
    )


#: The ONE wired mapping the argv cross-check cannot corroborate, recorded as
#: a (tool, capability) PAIR and asserted set-equal. Scoping the exemption to
#: the capability NAME instead covered all 15 capabilities that build no
#: extractable argv — including `wiki_lint` and `wiki_status`, both tier READ,
#: so a future tool mapped to either would have been graded read with nothing
#: able to contradict it. An exemption that widens by itself is the failure
#: mode this whole file exists to make impossible.
_UNCORROBORATED = frozenset({("fleet_wiki_search", "wiki_query")})


def _dispatched_argvs(tool_name: str) -> list[list]:
    """What *tool_name* actually sends to the CLI, with the CLI stubbed out."""
    from unittest import mock

    seen: list[list] = []
    with mock.patch(
        "agentwire.voice_layer.tools.run_agentwire_cmd",
        lambda argv, **kw: seen.append(list(argv)) or {"success": True},
    ):
        tools.dispatch(tool_name, dict(_SWEEP_ARGS), "buddy")
    return seen


def uncorroborated_pairs(mapping: dict) -> set:
    """(tool, capability) pairs the argv cross-check cannot corroborate.

    Reported separately from the mismatches so the exemption can be asserted
    set-equal — a new uncheckable mapping is red, and so is a stale entry here
    once the capability grows an extractable argv.
    """
    mcp_argvs = mcp_tool_argvs()
    pairs = set()
    for tool in tools.READ_ONLY_TOOLS:
        for capability in mapping.get(tool.name, ()):
            if _dispatched_argvs(tool.name) and not mcp_argvs.get(capability):
                pairs.add((tool.name, capability))
    return pairs


def capability_argv_mismatches(mapping: dict) -> dict[str, str]:
    """Wired read tools whose dispatched argv contradicts their mapped capability.

    A capability with no extractable argv corroborates nothing. That is
    tolerated only for the exact pairs in :data:`_UNCORROBORATED` — never for a
    capability name, which would exempt every tool that ever maps to it.
    """
    mcp_argvs = mcp_tool_argvs()
    mismatches: dict[str, str] = {}
    for tool in tools.READ_ONLY_TOOLS:
        capabilities = mapping.get(tool.name)
        if not capabilities:
            continue
        seen = _dispatched_argvs(tool.name)
        if not seen:
            continue  # voice-native (gh, spool) — ruled in VOICE_NATIVE
        for capability in capabilities:
            candidates = mcp_argvs.get(capability, [])
            if not candidates:
                if (tool.name, capability) in _UNCORROBORATED:
                    continue
                mismatches[tool.name] = (
                    f"{capability} builds no extractable argv, so this mapping "
                    "is unfalsifiable and is not a recorded exemption"
                )
                continue
            if not any(
                _argv_compatible(voice, mcp)
                for voice in seen for mcp in candidates
            ):
                mismatches[tool.name] = (
                    f"dispatches {seen} but {capability} builds {candidates}"
                )
    return mismatches


class TestEveryWiredToolIsRuled:
    """#979/5: the tier audit swept ``@mcp.tool`` names — a namespace that is
    not the exposed surface. ``buddy_inbox``, ``buddy_sent`` and
    ``fleet_pull_requests`` have no MCP capability behind them, so 'EVERY tool
    appears in exactly one tier' was true and beside the point: the next
    voice-native tool could ship with nothing forcing a grade out of anyone.
    Concretely ``buddy_inbox(ack=true)`` mutates state from the read-only
    allowlist."""

    def test_every_wired_tool_maps_to_a_capability_or_a_native_ruling(self):
        unruled = surface.unruled_tools(
            [t.name for t in tools.READ_ONLY_TOOLS]
            + [f"send_{s.name}" for s in write_tools.WRITE_SPECS]
        )
        assert unruled == {}, (
            f"wired tools with no tier and no voice-native ruling: {unruled} — "
            "rule them in voice_layer/surface.py"
        )

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

    def test_each_mapping_matches_the_argv_the_tool_actually_dispatches(self):
        """The map is asserted CORRECT, not merely consistent.

        Every other leg here reads `TOOL_CAPABILITY` and believes it, so
        `fleet_session_output: ('sessions_list',)` — simply wrong — kept the
        whole suite green, and "a wired tool's tier is derivable" meant "a
        hand-written map nothing checks": the same over-claim this PR fixes,
        one level up. This runs each read tool with the CLI stubbed, captures
        the argv it really builds, and demands the mapped MCP capability build
        a compatible one."""
        mismatches = capability_argv_mismatches(surface.TOOL_CAPABILITY)
        assert mismatches == {}, (
            f"TOOL_CAPABILITY entries whose argv does not match the capability "
            f"they name: {mismatches}"
        )

    def test_a_wrong_mapping_turns_that_red(self):
        """Must-fail control, using the reviewer's own mutation."""
        mutated = dict(surface.TOOL_CAPABILITY)
        mutated["fleet_session_output"] = ("sessions_list",)
        assert "fleet_session_output" in capability_argv_mismatches(mutated)

    def test_the_uncorroborable_exemption_is_scoped_to_named_pairs(self):
        """The exemption is one MAPPING, not a capability name.

        Scoped to the name, it covered every capability that builds no
        extractable argv — 15 of them, three of which (`wiki_lint`,
        `wiki_query`, `wiki_status`) are tier READ, so any future tool mapped
        to one would be graded read and be structurally unfalsifiable. That is
        the exemption widening in silence, which is the thing
        ``UNANALYZABLE_TOOLS`` is asserted set-equal to prevent."""
        assert uncorroborated_pairs(surface.TOOL_CAPABILITY) == _UNCORROBORATED
        # The pair exists because of the analyzer's blind spot, and the two
        # records must agree about which capability that is.
        for _tool, capability in _UNCORROBORATED:
            assert capability in UNANALYZABLE_TOOLS

    def test_a_second_uncheckable_mapping_is_not_covered_by_the_first(self):
        """Watched failing before the scoping: `wiki_status` is uncheckable and
        tier READ, so under a name-scoped exemption this mapping passed with
        nothing corroborating it."""
        mutated = dict(surface.TOOL_CAPABILITY)
        mutated["fleet_locks"] = ("wiki_status",)
        assert "fleet_locks" in capability_argv_mismatches(mutated)
        assert ("fleet_locks", "wiki_status") in uncorroborated_pairs(mutated)

    def test_an_uncheckable_mapping_is_named_not_waved_through(self):
        """The exempted mapping is still held to the rest of the check: point
        `fleet_wiki_search` somewhere that DOES build an argv and it must be
        compared against it like any other."""
        mutated = dict(surface.TOOL_CAPABILITY)
        mutated["fleet_wiki_search"] = ("council_list",)
        assert "fleet_wiki_search" in capability_argv_mismatches(mutated)

    def test_the_remote_ruling_is_pinned_like_the_other_two(self):
        """The constraint was that EVERY reclassification lands as a written
        ruling. The `@machine` paragraph could be deleted whole with nothing
        going red, while the other two rulings were pinned by name."""
        doc = " ".join((surface.__doc__ or "").split())
        assert "@machine" in doc
        assert "2026-08-09" in doc
        # The ruling as it now stands: LIVENESS decides, not the character.
        assert "LIVENESS" in doc

    def test_a_new_unruled_tool_turns_this_red(self):
        """Mutation check: the leg above is worthless if it passes for a name
        nobody ever ruled on."""
        unruled = surface.unruled_tools(["fleet_sessions", "buddy_telepathy"])
        assert set(unruled) == {"buddy_telepathy"}

    def test_a_native_tool_that_mutates_is_ruled_as_a_write(self):
        """buddy_inbox(ack=true) advances the read cursor. It sits in the
        read-only allowlist because the WIRING has one shape; its GRADE is a
        separate question and gets a separate answer."""
        assert surface.VOICE_NATIVE["buddy_inbox"]["grade"] == "write_light"
        assert surface.VOICE_NATIVE["buddy_sent"]["grade"] == "read"
        for ruling in surface.VOICE_NATIVE.values():
            assert ruling["ruling"].strip(), "a grade with no reason is not a ruling"

    def test_the_docstring_no_longer_claims_no_light_writes_are_wired(self):
        """The sentence was true when written and false the moment buddy_inbox
        shipped. Rewritten, not qualified — a stale guarantee gets rounded back
        up by the next reader."""
        doc = " ".join((surface.__doc__ or "").split())
        assert "currently none are wired" not in doc
        assert "buddy_inbox" in doc


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


_CREATION_MARKER_CALLS = {"build_agent_command", "create_and_register_worktree"}


def _spawns_ensure(fn: ast.AST) -> bool:
    """A literal ["agentwire", "ensure", ...] anywhere in the function body."""
    for node in ast.walk(fn):
        if isinstance(node, (ast.List, ast.Tuple)):
            vals = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            if any(a == "agentwire" and b == "ensure"
                   for a, b in zip(vals, vals[1:])):
                return True
    return False


def session_creating_functions() -> set[str]:
    """Fixpoint over the package call graph: names that can reach creation.

    Conservative on name collisions (same-named functions union their edges);
    over-flagging surfaces as a failure here and gets resolved by a human,
    which is the correct direction for a harness-boundary check.
    """
    package_root = Path(tools.__file__).resolve().parents[1]
    funcs: dict[str, set[str]] = {}
    marked: set[str] = set()
    for path in package_root.rglob("*.py"):
        if path.name.startswith("mcp_"):
            continue  # the MCP layer is the SUBJECT of the check, not its map
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = {n for sub in ast.walk(node)
                         if isinstance(sub, ast.Call) and (n := _call_name(sub))}
                funcs.setdefault(node.name, set()).update(calls)
                if calls & _CREATION_MARKER_CALLS or _spawns_ensure(node):
                    marked.add(node.name)
    creating = set(marked)
    changed = True
    while changed:
        changed = False
        for name, calls in funcs.items():
            if name not in creating and calls & creating:
                creating.add(name)
                changed = True
    return creating


def cli_verb_tree() -> dict:
    """verb -> {"func": handler|None, "children": {subverb: handler}},
    parsed from every ``*_cli.py`` registrar (add_parser / add_subparsers /
    set_defaults(func=...) — the uniform registrar shape per CLAUDE.md #495).
    """
    package_root = Path(tools.__file__).resolve().parents[1]
    tree: dict = {}
    for path in package_root.glob("*_cli.py"):
        module = ast.parse(path.read_text())
        parser_parent: dict[str, str | None] = {}
        parser_verb: dict[str, str] = {}
        sub_owner: dict[str, str] = {}
        parser_func: dict[str, str] = {}
        for node in ast.walk(module):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                name = _call_name(node.value)
                base = node.value.func.value if isinstance(
                    node.value.func, ast.Attribute) else None
                if (name == "add_parser" and node.value.args
                        and isinstance(node.value.args[0], ast.Constant)):
                    parser_parent[target.id] = base.id if isinstance(
                        base, ast.Name) else None
                    parser_verb[target.id] = node.value.args[0].value
                elif name == "add_subparsers" and isinstance(base, ast.Name):
                    sub_owner[target.id] = base.id
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if _call_name(call) == "set_defaults" and isinstance(
                        call.func.value, ast.Name):
                    for kw in call.keywords:
                        if kw.arg == "func" and isinstance(kw.value, ast.Name):
                            parser_func[call.func.value.id] = kw.value.id
        for var, verb in parser_verb.items():
            parent = parser_parent.get(var)
            func = parser_func.get(var)
            if parent in sub_owner:
                owner_verb = parser_verb.get(sub_owner[parent])
                if owner_verb:
                    tree.setdefault(owner_verb, {"func": None, "children": {}})[
                        "children"][verb] = func
                    continue
            tree.setdefault(verb, {"func": None, "children": {}})
            if func:
                tree[verb]["func"] = func
    assert tree, "parsed no CLI registrars — the analyzer itself broke"
    return tree


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


def mcp_tool_argvs(sources: "list[str] | None" = None) -> dict[str, list[list]]:
    """tool name -> the constant-string argv literals its body builds.

    **The covered shape, stated** (#979/4): a LIST LITERAL whose first element
    is a string constant, appearing anywhere in the tool's body. That is the
    shape ``run_agentwire_cmd(["worktree", ...])`` takes and nothing else. An
    argv assembled dynamically — built by a helper, extended from a variable,
    chosen by a branch that stores the verb in a name — contributes NOTHING
    here, and a tool with no extracted argv is therefore UNCHECKED by the
    dispatch-path analyzer rather than cleared by it. Those tools are flagged
    for manual placement by
    ``TestNoTieredInToolCanCreateASession`` (the manual-placement leg)
    instead of passing silently, which is the direction this whole check
    claims to fail in.
    """
    out: dict[str, list[list]] = {}
    for name, node in mcp_tool_defs(sources).items():
        argvs = []
        for sub in ast.walk(node):
            if (isinstance(sub, ast.List) and sub.elts
                    and isinstance(sub.elts[0], ast.Constant)
                    and isinstance(sub.elts[0].value, str)):
                argvs.append([e.value if isinstance(e, ast.Constant) else None
                              for e in sub.elts])
        out[name] = argvs
    return out


def in_process_creating_tools(sources: "list[str] | None" = None) -> dict[str, str]:
    """MCP tool -> the session-creating function it calls IN PROCESS.

    The second blind spot (#979/4): ``session_creating_functions`` deliberately
    skips ``mcp_*.py`` when building its map, so an MCP tool that reaches a
    creation helper directly — no CLI dispatch, no argv to walk — was invisible
    to the whole check. No shipped tool has that shape today; the control below
    proves the detector would see one, because a check that is silently green
    and a check that is green because the codebase is clean are the same
    observation until you force the difference.
    """
    # The markers themselves count: they are the creation, so a tool calling
    # ``build_agent_command`` directly is the shortest possible version of
    # this path and must not need an intermediary to register.
    creating = session_creating_functions() | _CREATION_MARKER_CALLS
    flagged: dict[str, str] = {}
    for name, node in mcp_tool_defs(sources).items():
        calls = {n for sub in ast.walk(node)
                 if isinstance(sub, ast.Call) and (n := _call_name(sub))}
        hit = sorted(calls & creating)
        if hit or _spawns_ensure(node):
            flagged[name] = hit[0] if hit else "agentwire ensure"
    return flagged


#: Dispatches that reach a creating HANDLER in a mode that cannot create:
#: handler -> the mode flags that select its non-creating branches. Keyed on
#: the argv shape (the dispatch), never on the MCP tool's name — a new tool
#: hitting cmd_worktree without one of these flags still fails the check.
_NON_CREATING_MODES = {
    "cmd_worktree": {"--list", "--status", "--remove", "--prune", "--dangling"},
}

#: MCP tools the dispatch-path analyzer extracts NO argv from, and therefore
#: never checked — each one placed in its tier by hand, by reading it. They
#: reach their work through a Python API rather than a CLI argv: the desktop
#: family writes the portal's window state, the wiki family calls
#: ``agentwire.wiki`` directly, ``notify_user``/``transcribe`` go through the
#: portal and the STT backend, ``desktop_write_artifact`` writes a file.
#: Adding to this set is a claim that a human looked; the leg that asserts it
#: is the thing stopping a new tool from being unchecked AND unnoticed.
UNANALYZABLE_TOOLS = frozenset({
    "desktop_close_window", "desktop_collage", "desktop_focus_window",
    "desktop_layout", "desktop_minimize_all", "desktop_open_artifact",
    "desktop_open_panel", "desktop_open_session", "desktop_tile_window",
    "desktop_write_artifact",
    "notify_user", "transcribe",
    "wiki_lint", "wiki_query", "wiki_status",
})


def session_creating_tools() -> dict[str, str]:
    """MCP tool -> the creating CLI handler its dispatch path reaches."""
    creating = session_creating_functions()
    verbs = cli_verb_tree()
    flagged: dict[str, str] = {}
    for tool, argvs in mcp_tool_argvs().items():
        for argv in argvs:
            entry = verbs.get(argv[0])
            if not entry:
                continue
            func = entry["func"]
            if len(argv) > 1 and argv[1] in entry["children"]:
                func = entry["children"][argv[1]]
            if func not in creating:
                continue
            if any(t in _NON_CREATING_MODES.get(func, ()) for t in argv if t):
                continue
            flagged[tool] = func
    return flagged


class TestNoTieredInToolCanCreateASession:
    def test_the_analyzer_sees_the_known_creators(self):
        """Must-fail control: an analyzer that goes blind would pass the main
        assertion vacuously. ensure, the scheduler's forced run, and the raw
        creation verbs must all register as session-creating."""
        creating = session_creating_functions()
        for fn in ("cmd_ensure", "cmd_new", "cmd_worktree",
                   "cmd_scheduler_run", "cmd_spawn"):
            assert fn in creating, fn
        flagged = session_creating_tools()
        # The two wave-3 escapees, caught by path — not by their names.
        assert flagged.get("task_run") == "cmd_ensure"
        assert flagged.get("scheduler_run") == "cmd_scheduler_run"

    def test_a_tool_with_no_extractable_argv_is_flagged_for_manual_placement(self):
        """#979/4: the analyzer's covered shape is narrower than 'every MCP
        tool'. A tool it extracts no argv from is UNCHECKED, and an unchecked
        tool passing the main assertion is silent green — the exact direction
        the docstring claims this check avoids. Recorded here so a NEW one
        fails until a human places it by hand."""
        unanalyzable = {t for t, argvs in mcp_tool_argvs().items() if not argvs}
        assert unanalyzable == UNANALYZABLE_TOOLS, (
            "the set of MCP tools the dispatch-path analyzer cannot see has "
            "changed — place each new one by hand, then record it here: "
            f"new={sorted(unanalyzable - UNANALYZABLE_TOOLS)} "
            f"gone={sorted(UNANALYZABLE_TOOLS - unanalyzable)}"
        )

    def test_the_analyzer_admits_what_it_cannot_see(self):
        """Must-fail control for the leg above: a dynamically-built argv must
        register as unanalyzable, not as 'no argv, therefore harmless'."""
        source = (
            "@mcp.tool()\n"
            "def sneaky_verb(name: str) -> str:\n"
            "    argv = build_the_argv(name)\n"
            "    return run_agentwire_cmd(argv)\n"
        )
        assert mcp_tool_argvs([source]) == {"sneaky_verb": []}

    def test_an_in_process_creator_would_be_seen(self):
        """Must-fail control for the in-process leg: the fixture-shaped trap is
        a detector exercised only on the shape it already handles."""
        source = (
            "@mcp.tool()\n"
            "def helpful_verb(name: str) -> str:\n"
            "    cmd = build_agent_command(name)\n"
            "    return run(cmd)\n"
        )
        assert in_process_creating_tools([source]) == {
            "helpful_verb": "build_agent_command"
        }

    def test_no_tiered_in_tool_creates_a_session_in_process(self):
        """Clause (a) again, for the path with no argv at all."""
        offenders = {
            tool: fn for tool, fn in in_process_creating_tools().items()
            if tool not in surface.TIER_EXCLUDED
        }
        assert offenders == {}, (
            f"MCP tools calling session creation in process while tiered in: "
            f"{offenders}"
        )

    def test_every_session_creating_dispatch_path_is_excluded(self):
        """Clause (a) by dispatch path: any MCP tool whose argv reaches a
        handler that can create a session must sit in TIER_EXCLUDED."""
        offenders = {
            tool: fn for tool, fn in session_creating_tools().items()
            if tool not in surface.TIER_EXCLUDED
        }
        assert offenders == {}, (
            f"tools whose dispatch path can create a session but are not "
            f"excluded: {offenders} — clause (a) keys on the path, not the name"
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
        assert argv[-1].startswith("restart the portal")

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

    def test_a_proposal_carries_the_buddy_identity_whatever_its_argv(self):
        """#979/1, the same assumption one field over: the outbox reads the
        writer from ``--from``, which only the msg shape has. An argv-only
        write recorded under 'unknown' is invisible to the buddy's own
        buddy_sent — the instrument cannot answer about a write it filed under
        someone else. propose carries the identity in params, so attribution
        does not depend on the argv having a --from."""
        convo, _runner = _spine()
        propose = gated_triple(probe_spec())[0][3]
        propose({"_buddy": "buddy"}, convo.spine)
        (proposal,) = list(convo.spine._proposals.values())
        assert proposal.params.get("_buddy") == "buddy"

    def test_a_write_to_an_unreachable_at_name_is_refused_whole(self, monkeypatch):
        """`_require_live` used to compare `session.split("@")[0]` against LOCAL
        tmux, so `web@laptop` passed liveness on the strength of a local `web`
        and then addressed something else. It compares the WHOLE name now — and
        that is also what makes accepting a local `ops@edge` safe, since every
        layer asks about the name it was given."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: {"web"})
        # Called directly: `_session_arg` refuses this name first, so nothing
        # else in the suite can tell a whole-name comparison from a split one,
        # and an unpinned split grows back the moment remotes are revisited.
        with pytest.raises(tools.ToolError, match="Nothing is listening"):
            write_tools._require_live("web@laptop", cannot="")

        propose = write_tools.WRITE_TOOL_FNS["propose_session_message"]
        convo, runner = _spine()
        with pytest.raises(tools.ToolError, match="(?i)no live session"):
            propose(
                {"session": "web@laptop", "message": "ship it", "_buddy": "buddy"},
                convo.spine,
            )
        assert runner.calls == []

    def test_a_write_to_a_live_local_at_name_is_allowed(self, monkeypatch):
        """The false-reject half on the write path: `ops@edge` is a creatable,
        addressable LOCAL tmux session, and refusing to message one is the
        buddy declining work it can do."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: {"ops@edge"})
        propose = write_tools.WRITE_TOOL_FNS["propose_session_message"]
        convo, _runner = _spine()
        result = propose(
            {"session": "ops@edge", "message": "ship it", "_buddy": "buddy"},
            convo.spine,
        )
        assert result["session"] == "ops@edge"
        (proposal,) = list(convo.spine._proposals.values())
        assert "ops@edge" in proposal.argv_prefix

    def test_shipped_specs_pass_the_same_declaration_guards(self):
        for spec in write_tools.WRITE_SPECS:
            assert "{phrase}" not in spec.fallback_template
            assert spec.params_schema.get("additionalProperties") is False

    def test_an_argv_only_write_reads_as_executed_not_delivered(self):
        """A kind-less outbox entry has no queue to interrogate; claiming
        'delivered' for it would be a category error (§3.6)."""
        from agentwire.voice_layer import outbox

        entry = {"proposal_id": "abc123", "session": "target",
                 "body": "target", "kind": "", "dispatched": True}
        assert outbox.delivery_state(entry)["state"] == "executed"

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

    @pytest.mark.parametrize(
        "name", ["fleet_session_info", "fleet_session_inbox", "fleet_session_output"]
    )
    def test_an_at_name_tmux_does_not_know_is_refused(self, seen, monkeypatch, name):
        """Owner ruling 2026-08-09: remote `name@machine` targets are out of
        scope. Enforced by LIVENESS, not by the shape of the name — because the
        shape does not say remote. tmux accepts `@` verbatim, so `ops@edge` is
        a creatable local session, and a refusal keyed on the character alone
        told the owner a true local name was remote: a confident falsehood with
        no move from it, in a channel with no screen."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: {"ops@edge"})
        result = tools.dispatch(name, {"session": "web@laptop"}, "buddy")
        assert result["success"] is False
        assert result["must_speak"] is True
        assert seen == []
        # It must NOT assert remoteness — that is the claim it cannot make.
        assert "web@laptop" in result["error"]
        assert "no live session" in result["error"].lower()
        assert "aren't reachable" not in result["error"]

    def test_a_live_local_at_name_is_reachable(self, seen, monkeypatch):
        """The false-reject half, measured against the base branch's behaviour:
        base dispatched `['info', '-s', 'ops@edge']` and the first fix refused
        it. `@` is legal in tmux (only `.` and `:` are rewritten, #878) and in
        `inbox._SESSION_RE`, so this name is ordinary local work."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: {"ops@edge"})
        result = tools.dispatch(
            "fleet_session_info", {"session": "ops@edge"}, "buddy"
        )
        assert result.get("success") is not False, result
        assert seen == [["info", "-s", "ops@edge"]]

    def test_an_unprovable_liveness_does_not_refuse(self, seen, monkeypatch):
        """`live_sessions()` returns None when tmux itself is unreachable. That
        is an outage, not a verdict — refusing there would ground the buddy on
        every local `@` name during a tmux blip, and the CLI reports what it
        finds. Same doctrine as `_require_live` (spec §5): only POSITIVE
        knowledge refuses."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: None)
        result = tools.dispatch(
            "fleet_session_info", {"session": "ops@edge"}, "buddy"
        )
        assert result.get("success") is not False, result
        assert seen == [["info", "-s", "ops@edge"]]

    @pytest.mark.parametrize("bad", ["we b@x", "@a", "--help@x", "../etc@passwd"])
    def test_a_malformed_at_name_gets_the_shape_refusal(self, seen, monkeypatch, bad):
        """Ordering, pinned. The `@` check used to run BEFORE shape validation,
        so a garbled name that happened to contain one was told it was remote —
        a wrong diagnosis of a mis-transcription, and nothing in the suite
        noticed if the two moved past each other."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: set())
        result = tools.dispatch("fleet_session_info", {"session": bad}, "buddy")
        assert result["success"] is False
        assert "valid session name" in result["error"]
        assert "no live session" not in result["error"].lower()
        assert seen == []

    def test_a_bare_local_name_is_still_accepted(self, seen):
        """A name with no `@` never consults liveness at all — reads must keep
        working for a session that has since exited."""
        result = tools.dispatch(
            "fleet_session_info", {"session": "agentwire-dev"}, "buddy"
        )
        assert result.get("success") is not False, result
        assert seen == [["info", "-s", "agentwire-dev"]]

    @pytest.mark.parametrize("bad", ["-x/y", "-/-", "own er/x", "a/b/c", "o.x/n"])
    def test_a_bad_owner_segment_is_refused_by_the_pattern(self, monkeypatch, bad):
        """#979/6: `_REPO_RE` admitted `-x/y`, a value-position flag. GitHub
        constrains OWNERS to alphanumeric-and-hyphen starting alphanumeric, so
        that is where the rule belongs.

        Asserted at the pattern, with `gh` monkeypatched out — letting the real
        subprocess run makes 'gh said no' indistinguishable from 'the pattern
        said no', which is exactly how this test passes without the fix."""
        ran: list = []
        monkeypatch.setattr(
            "agentwire.voice_layer.tools.subprocess.run",
            lambda *a, **kw: ran.append(a) or (_ for _ in ()).throw(AssertionError),
        )
        result = tools.dispatch("fleet_pull_requests", {"repo": bad}, "buddy")
        assert result["success"] is False
        assert "owner/name" in result["error"]
        assert ran == []

    @pytest.mark.parametrize(
        "repo", ["dotdevdotdev/agentwire-dev", "github/.github", "owner/_name",
                 "owner/-name"],
    )
    def test_real_repository_names_still_reach_gh(self, monkeypatch, repo):
        """The false-reject half, and the reason the rule is owner-only:
        `github/.github` is a real repository, and GitHub lets a REPO name
        begin with `.`, `_` or `-`. Buying pattern symmetry with a refusal of
        real repositories is a worse trade than the inconsistency it fixes."""
        seen_cmd: list = []
        monkeypatch.setattr(
            "agentwire.voice_layer.tools.subprocess.run",
            lambda cmd, **kw: seen_cmd.append(cmd)
            or SimpleNamespace(returncode=0, stdout="[]", stderr=""),
        )
        result = tools.dispatch("fleet_pull_requests", {"repo": repo}, "buddy")
        assert result["success"] is True
        assert seen_cmd[0][:5] == ["gh", "pr", "list", "--repo", repo]

    def test_an_empty_query_is_refused_with_speech(self, seen):
        result = tools.dispatch("fleet_wiki_search", {"query": "  ---  "}, "b")
        assert result["success"] is False
        assert result["must_speak"] is True
        assert seen == []


# =============================================================================
# Wave-2 prose: the premise a repair would argue from
# =============================================================================


def _flat(text: str) -> str:
    """Whitespace-normalized, so an assertion survives a re-wrap of the prose."""
    return " ".join((text or "").split())


class TestRequireLiveNamesTheMechanismItActuallyHas:
    """``_require_live``'s docstring justified its whole-name comparison from a
    premise the shipped code does not have: that ``_session_arg`` "refuses the
    syntax outright", making a name "local by construction".

    #994's final shape refuses no syntax — surface.py records that the first
    attempt did and that doing so was itself a false statement, because
    ``ops@edge`` is a creatable local session. The gate is LIVENESS. The
    conclusion survives; the premise did not, and a premise is what the next
    person repairing this reasons from — they would look for a syntax refusal,
    fail to find one, and "restore" it.

    Pinned in both directions: the dead mechanism must not come back into the
    prose, and the live one must be named.
    """

    def test_the_docstring_does_not_claim_a_syntax_refusal(self):
        doc = _flat(write_tools._require_live.__doc__)
        assert "refuses the syntax outright" not in doc
        assert "local by construction" not in doc

    def test_the_docstring_names_liveness_and_session_arg(self):
        doc = _flat(write_tools._require_live.__doc__)
        assert "LIVENESS" in doc
        assert "_session_arg" in doc
        assert "local by DEMONSTRATION" in doc

    def test_the_named_mechanism_is_the_one_that_runs(self, monkeypatch):
        """The control that keeps the sentence honest rather than merely
        rewritten: ``_session_arg`` admits an ``@`` name local tmux reports
        live and refuses one it does not — a liveness gate, not a syntax one.
        If that ever becomes a syntax refusal again, this fails alongside the
        prose that describes it."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: {"ops@edge"})
        assert tools._session_arg({"session": "ops@edge"}) == "ops@edge"
        with pytest.raises(tools.ToolError, match="no live session"):
            tools._session_arg({"session": "web@laptop"})

    def test_an_unreachable_tmux_makes_neither_layer_guess(self, monkeypatch):
        """The one case the rewritten sentence carves out (spec §5): nothing is
        demonstrated, so nothing is refused."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: None)
        assert tools._session_arg({"session": "web@laptop"}) == "web@laptop"
        write_tools._require_live("web@laptop", cannot="")


class TestTheAuditSeesThroughDecorators:
    """The blind spot the beta gate exposed: the tier audit's tool discovery
    must not depend on ``def`` following ``@mcp.tool()`` on the very next line.

    A tool the discovery cannot see is a tool ``test_every_mcp_tool_is_tiered``
    cannot force anyone to tier — the audit fails OPEN, silently, and the
    module's whole claim ("every tool name is placed in exactly one tier by a
    written rule") quietly stops being true.
    """

    STACKED = (
        "@mcp.tool()\n"
        "@gated_doc\n"
        "def stacked_tool(x: str) -> str:\n"
        '    """doc"""\n'
        "    return x\n"
    )

    def test_a_stacked_decorator_does_not_hide_a_tool(self):
        assert "stacked_tool" in mcp_tool_names([self.STACKED])

    def test_the_control_a_plain_tool_is_still_found(self):
        plain = "@mcp.tool()\ndef plain_tool() -> str:\n    return ''\n"
        assert mcp_tool_names([plain]) == {"plain_tool"}

    def test_the_real_package_still_exposes_msg_send(self):
        """The concrete regression: ``msg_send`` wears ``@gated_doc`` now, and
        the old regex reported it as a ghost tier entry."""
        assert "msg_send" in mcp_tool_names()
