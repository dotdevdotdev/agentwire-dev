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
import re
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

    def test_a_write_to_a_remote_target_is_refused_at_the_pattern(self, monkeypatch):
        """The write side of the same ruling. `_require_live` used to compare
        `session.split("@")[0]` against LOCAL tmux, so a remote name that was
        genuinely live locally under its bare half could pass liveness and then
        address the wrong machine. With `@` gone from the pattern the refusal
        happens before liveness is ever consulted — asserted with tmux SAYING
        the bare half is live, which is the shape that used to slip."""
        monkeypatch.setattr("agentwire.inbox.live_sessions", lambda: {"web"})
        # The liveness check itself, called directly — the pattern refuses
        # before it is ever reached, so nothing else in the suite can tell a
        # whole-name comparison from a split one, and an unpinned split grows
        # back the moment remotes are revisited.
        with pytest.raises(tools.ToolError, match="Nothing is listening"):
            write_tools._require_live("web@laptop", cannot="")

        propose = write_tools.WRITE_TOOL_FNS["propose_session_message"]
        convo, runner = _spine()
        with pytest.raises(tools.ToolError, match="(?i)remote"):
            propose(
                {"session": "web@laptop", "message": "ship it", "_buddy": "buddy"},
                convo.spine,
            )
        assert runner.calls == []

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
    def test_a_remote_target_is_refused_and_says_why(self, seen, name):
        """#979/2, owner ruling 2026-08-09: remote `name@machine` targets are
        out of scope. The voice layer no longer ACCEPTS the syntax at all —
        half-accepting it is what produced three wrong answers at once (the
        wrong inbox interrogated, a live remote session called dead, an inbox
        dir keyed on the raw string).

        The false-reject half is priced by the wording, not by admitting the
        name: a refusal that just says 'not a valid session name' sends the
        owner round the loop re-pronouncing a name that was heard correctly.
        This one names the actual limit, so the owner can stop asking."""
        result = tools.dispatch(name, {"session": "web@laptop"}, "buddy")
        assert result["success"] is False
        assert result["must_speak"] is True
        assert "remote" in result["error"].lower()
        assert seen == []

    def test_the_pattern_itself_no_longer_admits_the_syntax(self):
        """Both layers, asserted separately. The spoken refusal fires first, so
        every behavioural test above passes with `@` still in the pattern —
        which is how the pattern quietly stays permissive and the next reader,
        seeing it accept `name@machine`, decides the message check is
        redundant. The ruling is that NO layer here accepts the syntax."""
        assert tools._SESSION_RE.match("web@laptop") is None
        assert tools._SESSION_RE.match("web") is not None

    def test_a_bare_local_name_is_still_accepted(self, seen):
        """The other half of the same gate: dropping @ must not narrow the
        ordinary local name, which is every name the buddy can reach."""
        result = tools.dispatch(
            "fleet_session_info", {"session": "agentwire-dev"}, "buddy"
        )
        assert result.get("success") is not False, result
        assert seen == [["info", "-s", "agentwire-dev"]]

    @pytest.mark.parametrize("bad", ["-x/y", "x/-y", "-/-"])
    def test_a_leading_dash_repo_is_refused_by_the_pattern(self, monkeypatch, bad):
        """#979/6: `_REPO_RE` admitted `-x/y`, which violates the
        leading-alphanumeric rule `_SESSION_RE` states two paragraphs earlier.
        A value-position flag is not exploitable today; an inconsistent stated
        discipline is how the next copy of the pattern gets it wrong.

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

    def test_an_ordinary_repo_still_reaches_gh(self, monkeypatch):
        """The false-reject half of the same tightening."""
        seen_cmd: list = []
        monkeypatch.setattr(
            "agentwire.voice_layer.tools.subprocess.run",
            lambda cmd, **kw: seen_cmd.append(cmd)
            or SimpleNamespace(returncode=0, stdout="[]", stderr=""),
        )
        result = tools.dispatch(
            "fleet_pull_requests", {"repo": "dotdevdotdev/agentwire-dev"}, "buddy"
        )
        assert result["success"] is True
        assert seen_cmd[0][:5] == [
            "gh", "pr", "list", "--repo", "dotdevdotdev/agentwire-dev",
        ]

    def test_an_empty_query_is_refused_with_speech(self, seen):
        result = tools.dispatch("fleet_wiki_search", {"query": "  ---  "}, "b")
        assert result["success"] is False
        assert result["must_speak"] is True
        assert seen == []
