"""The policy ladder, tested directly (#1209 M3): ``Guard.decide`` against a stub engine.

The ladder used to live, copy-pasted, in the five hook ``main()``s and could only be
reached by running a hook as a subprocess. It is now one pure function, so the whole
bypass x unverifiable x unattended x grant matrix runs in-process with no rule files:
the stub engine returns a canned engine finding, and the test asserts the verdict.
"""

import itertools
import json
import types

import pytest

from damage_control import adapter, policy
from damage_control.policy import ALLOW, ASK, BLOCK, Context, Guard, ToolCall
from damage_control.profile import GENERIC, McpCommandTemplate

HINT = "To permit this for an unattended task, add rule id '{rule_id}' to its tasks file"


def make_engine(finding=None, grants=(), profile=GENERIC, **overrides):
    """A stub of the engine functions the ladder reaches for. ``finding`` is check_command's answer."""
    calls = types.SimpleNamespace(grant_kwargs=[])

    def grant_allows(rule_id, command, granted, cwd, pattern=None, scopeable=True):
        calls.grant_kwargs.append({"cwd": cwd, "scopeable": scopeable})
        return (rule_id in grants, "no grant" if rule_id not in grants else "granted")

    engine = types.SimpleNamespace(
        PROFILE=profile,
        calls=calls,
        SCAN_DEADLINE_SECONDS=10,
        check_command=lambda command, config, cwd=None: dict(finding),
        resolve_unattended_grants=lambda config: {g: [] for g in grants},
        unattended_grant_allows=grant_allows,
        safe_cwd=lambda: "/safe-cwd",
        synthesize_mcp_command=lambda name, tool_input: None,
        kill_switch_off=lambda config: False,
        mentions_control_plane=lambda text: False,
        load_allowed_paths=lambda config: [],
        check_read_path=lambda path, config: (False, ""),
        check_path=lambda path, config: (False, ""),
        check_protected_path=lambda path, allowed: (False, ""),
        check_mcp_tool=lambda name, config: {"decision": "allow", "reason": "", "id": None, "mutates": None},
        ScanBudgetExceeded=type("ScanBudgetExceeded", (BaseException,), {}),
    )
    engine.scan_deadline = lambda seconds: _NullCtx()
    for key, value in overrides.items():
        setattr(engine, key, value)
    return engine


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def shell(command="do-thing", cwd="/proj"):
    return ToolCall("shell", "Bash", command=command, cwd=cwd)


def guard(engine, safety=None):
    return Guard({"safety": safety or {}}, engine)


def expected_shell(decision, unverifiable, mode, unattended, granted, ambiguous, synthesized):
    """The contract, stated independently of the implementation's branch order."""
    if decision != "ask":
        return decision
    if unattended:
        # No human: bypass never helps. Only the ambiguity carve-out or a grant lets it through.
        if ambiguous and not unverifiable and not synthesized:
            return ALLOW
        return ALLOW if granted else BLOCK
    if mode == "bypass" and not unverifiable:
        return ALLOW  # bypass demotes an ask it can recognize...
    return ASK  # ...but never a verb-concealing one (#934)


def test_shell_ladder_matrix():
    wrong = []
    for decision, unverifiable, mode, unattended, granted, ambiguous in itertools.product(
        ("allow", "ask", "block"), (False, True), ("default", "bypass"), (False, True), (False, True), (False, True),
    ):
        rule_id = policy.AMBIGUOUS_COMMAND_RULE if ambiguous else "git.push"
        finding = {"decision": decision, "reason": "r", "id": rule_id, "unverifiable": unverifiable}
        engine = make_engine(finding, grants=(rule_id,) if granted else ())
        got = guard(engine).decide(shell(), Context(mode=mode, unattended=unattended)).decision
        want = expected_shell(decision, unverifiable, mode, unattended, granted, ambiguous, synthesized=False)
        if got != want:
            wrong.append((decision, unverifiable, mode, unattended, granted, ambiguous, got, want))
    assert not wrong, wrong


def test_mcp_outbound_ladder_matrix_has_no_ambiguity_carve_out_and_never_scopes():
    wrong = []
    for decision, unverifiable, mode, unattended, granted, ambiguous in itertools.product(
        ("allow", "ask", "block"), (False, True), ("default", "bypass"), (False, True), (False, True), (False, True),
    ):
        rule_id = policy.AMBIGUOUS_COMMAND_RULE if ambiguous else "outbound.email"
        finding = {"decision": decision, "reason": "r", "id": rule_id, "unverifiable": unverifiable}
        engine = make_engine(finding, grants=(rule_id,) if granted else (),
                             synthesize_mcp_command=lambda name, tool_input: "send --to x")
        call = ToolCall("mcp", "mcp__host__email_send", tool_input={})
        got = guard(engine).decide(call, Context(mode=mode, unattended=unattended)).decision
        want = expected_shell(decision, unverifiable, mode, unattended, granted, ambiguous, synthesized=True)
        scopeable = [k["scopeable"] for k in engine.calls.grant_kwargs]
        if got != want or any(scopeable):
            wrong.append((decision, unverifiable, mode, unattended, granted, ambiguous, got, want, scopeable))
    assert not wrong, wrong


def test_an_unattended_block_notifies_names_the_grant_and_is_audited():
    finding = {"decision": "ask", "reason": "pushes", "id": "git.push", "pattern": "p"}
    profile = GENERIC._replace(unattended_grant_hint=HINT)
    v = guard(make_engine(finding, profile=profile)).decide(shell("git push"), Context(unattended=True))
    assert v.decision == BLOCK
    assert v.notify == ("git push", "pushes — no grant", "git.push")
    assert v.audit[0] == "blocked" and v.audit[1][2].startswith("unattended: ")
    assert v.stderr[-1] == HINT.format(rule_id="git.push") + " (optionally scoped: `- {id: git.push, paths: [<dir>/]}`)."
    # An MCP-synthesized command names no directory, so the hint omits the scoped form.
    engine = make_engine(finding, profile=profile, synthesize_mcp_command=lambda n, i: "send")
    v = guard(engine).decide(ToolCall("mcp", "mcp__h__send", tool_input={}), Context(unattended=True))
    assert v.stderr[-1] == HINT.format(rule_id="git.push") + "."


def test_a_profile_without_a_grant_hint_says_nothing_about_grants():
    finding = {"decision": "ask", "reason": "r", "id": "git.push"}
    v = guard(make_engine(finding)).decide(shell(), Context(unattended=True))
    assert v.decision == BLOCK
    assert not any("permit" in line for line in v.stderr)


def test_the_directory_a_scoped_grant_is_measured_from_is_the_hook_cwd_then_safe_cwd():
    finding = {"decision": "ask", "reason": "r", "id": "git.commit"}
    engine = make_engine(finding, grants=("git.commit",))
    guard(engine).decide(shell(cwd="/proj"), Context(unattended=True))
    guard(engine).decide(shell(cwd=None), Context(unattended=True))
    assert [k["cwd"] for k in engine.calls.grant_kwargs] == ["/proj", "/safe-cwd"]


def test_escape_and_disabled_findings_allow_with_their_own_audit_event():
    for flag, event in (("escape", "escape"), ("disabled", "disabled")):
        finding = {"decision": "allow", "reason": "", "id": None, flag: True, "escape_reason": "because"}
        v = guard(make_engine(finding)).decide(shell(), Context())
        assert (v.decision, getattr(v, flag), v.audit[0]) == (ALLOW, True, event), flag


def test_the_allow_row_carries_the_rule_that_allowed_it_for_shell_only():
    finding = {"decision": "allow", "reason": "", "id": None, "allowed_by": "allowlist"}
    assert guard(make_engine(finding)).decide(shell(), Context()).audit[2]["allowed_by"] == "allowlist"
    engine = make_engine(finding, synthesize_mcp_command=lambda n, i: "send")
    v = guard(engine).decide(ToolCall("mcp", "mcp__h__send", tool_input={}), Context())
    assert "allowed_by" not in v.audit[2]


# -- Read / Write / Edit: the protected check precedes the kill switch ---------------


def test_path_ladder_runs_the_protected_check_before_the_kill_switch():
    wrong = []
    for kind, blocked, kill in itertools.product(("read", "write", "edit"), (False, True), (False, True)):
        engine = make_engine(
            check_path=lambda p, c, b=blocked: (b, "protected thing"),
            check_read_path=lambda p, c, b=blocked: (b, "protected thing"),
        )
        v = guard(engine, {"enabled": not kill}).decide(ToolCall(kind, "T", paths=("/p",)), Context())
        want = (BLOCK, False) if blocked else (ALLOW, kill)  # (decision, disabled)
        if (v.decision, v.disabled) != want:
            wrong.append((kind, blocked, kill, v.decision, v.disabled))
    assert not wrong, wrong


def test_path_kinds_read_with_the_read_ladder_and_write_with_the_write_ladder():
    engine = make_engine(
        check_path=lambda p, c: (True, "WRITE-LADDER"),
        check_read_path=lambda p, c: (True, "READ-LADDER"),
    )
    got = {k: guard(engine).decide(ToolCall(k, "T", paths=("/p",)), Context()) for k in ("read", "write", "edit")}
    assert {k: v.stderr[0] for k, v in got.items()} == {
        "read": "SECURITY: Blocked read of READ-LADDER: /p",
        "write": "SECURITY: Blocked write to WRITE-LADDER: /p",
        "edit": "SECURITY: Blocked edit to WRITE-LADDER: /p",
    }


# -- MCP tools that are not outbound ------------------------------------------------


def mcp(engine, ctx=Context(), tool="mcp__h__tool", tool_input=None, safety=None):
    return guard(engine, safety).decide(ToolCall("mcp", tool, tool_input=tool_input or {}), ctx)


def test_an_mcp_tool_policy_block_blocks_and_other_tiers_flow_through():
    blocked = make_engine(check_mcp_tool=lambda n, c: {"decision": "block", "reason": "nope", "id": "mcp.x"})
    assert mcp(blocked).decision == BLOCK
    assert mcp(make_engine()).decision == ALLOW


def test_a_protected_path_argument_blocks_whatever_tier_the_tool_has():
    engine = make_engine(check_read_path=lambda p, c: (p == "/secret", "zero-access"))
    v = mcp(engine, tool_input={"args": ["/ok", {"nested": "/secret"}]})
    assert (v.decision, v.stderr) == (BLOCK, ("SECURITY: Blocked MCP tool argument: zero-access: /secret",))


def test_the_control_plane_is_screened_only_for_tools_whose_policy_says_they_mutate():
    seen = []
    engine = make_engine(check_protected_path=lambda p, a: (seen.append(p) or True, "control plane"))
    readish = {"decision": "allow", "reason": "", "id": None, "mutates": False}
    writeish = dict(readish, mutates=True)
    assert mcp(make_engine(check_mcp_tool=lambda n, c: readish, check_protected_path=engine.check_protected_path),
               tool_input={"p": "/cfg"}).decision == ALLOW
    assert seen == []
    assert mcp(make_engine(check_mcp_tool=lambda n, c: writeish, check_protected_path=engine.check_protected_path),
               tool_input={"p": "/cfg"}).decision == BLOCK


def test_a_stale_rules_dir_with_no_policies_falls_back_to_the_write_ish_name_heuristic():
    engine = make_engine(check_protected_path=lambda p, a: (True, "control plane"))
    config = {"safety": {}, "mcpToolPolicies": []}
    for tool, want in (("mcp__h__delete_file", BLOCK), ("mcp__h__list_files", ALLOW)):
        v = Guard(config, engine).decide(ToolCall("mcp", tool, tool_input={"p": "/cfg"}), Context())
        assert v.decision == want, tool


def budget_exceeded_engine(kill=False, control_plane=False):
    exceeded = type("ScanBudgetExceeded", (BaseException,), {})

    def over_budget(tool, tool_input, writeish):
        raise exceeded()

    engine = make_engine(ScanBudgetExceeded=exceeded, kill_switch_off=lambda c: kill,
                         mentions_control_plane=lambda s: control_plane)
    return engine, over_budget


def test_an_over_budget_mcp_screen_fails_closed_and_bypass_cannot_demote_it():
    wrong = []
    for mode, unattended, granted in itertools.product(("default", "bypass"), (False, True), (False, True)):
        engine, over_budget = budget_exceeded_engine()
        g = guard(engine)
        g._screen_tool_paths = over_budget
        engine.resolve_unattended_grants = lambda c: {"core.scan-budget": []} if granted else {}
        engine.unattended_grant_allows = lambda r, t, gr, cwd, scopeable=True, g=granted: (g, "w")
        v = g.decide(ToolCall("mcp", "mcp__h__t", tool_input={}), Context(mode=mode, unattended=unattended))
        want = (ALLOW if granted else BLOCK) if unattended else ASK
        if v.decision != want:
            wrong.append((mode, unattended, granted, v.decision, want))
    assert not wrong, wrong


def test_the_kill_switch_lifts_the_scan_budget_unless_a_control_plane_path_is_named():
    for control_plane, want in ((False, ALLOW), (True, ASK)):
        engine, over_budget = budget_exceeded_engine(kill=True, control_plane=control_plane)
        g = guard(engine)
        g._screen_tool_paths = over_budget
        v = g.decide(ToolCall("mcp", "mcp__h__t", tool_input={"x": "y"}), Context())
        assert v.decision == want, control_plane


# -- Rendering: the only place a verdict becomes an exit ----------------------------


def run_render(verdict, capsys, log=None):
    logged = []
    engine = types.SimpleNamespace(
        PROFILE=GENERIC._replace(unattended_block_notifier=()),
        audit_best_effort=lambda fn: fn,
        log_blocked=lambda *a, **k: logged.append(("blocked", a, k)),
        log_asked=lambda *a, **k: logged.append(("asked", a, k)),
        log_allowed=lambda *a, **k: logged.append(("allowed", a, k)),
    )
    with pytest.raises(SystemExit) as exc:
        adapter.render(verdict, engine)
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err, logged


def test_render_maps_each_decision_to_its_exit_code_and_channel(capsys):
    block = policy.Verdict(BLOCK, "why", stderr=("SECURITY: Blocked: why",),
                           audit=policy._audit("blocked", "Bash", "cmd", "why"))
    code, out, err, logged = run_render(block, capsys)
    assert (code, out, err, [e[0] for e in logged]) == (2, "", "SECURITY: Blocked: why\n", ["blocked"])

    code, out, err, logged = run_render(policy.Verdict(ASK, "sure?", audit=policy._audit("asked", "Bash", "c", "sure?")), capsys)
    assert code == 0 and err == ""
    assert json.loads(out)["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "sure?",
    }

    code, out, err, logged = run_render(policy.Verdict(ALLOW), capsys)
    assert (code, out, err, logged) == (0, "", "", [])


def test_parse_payload_builds_the_call_and_the_context(capsys):
    engine = types.SimpleNamespace(is_unattended=lambda: True)
    tools = {"Edit": ("edit", ("file_path", "notebook_path")), "Bash": ("shell", ("command",))}
    call, ctx = adapter.parse_payload(
        {"tool_name": "Edit", "tool_input": {"notebook_path": "/n.ipynb"}, "permission_mode": "auto"}, tools, engine)
    assert (call.kind, call.paths, ctx.mode, ctx.unattended) == ("edit", ("/n.ipynb",), "bypass", True)
    call, ctx = adapter.parse_payload({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/w"}, tools, engine)
    assert (call.kind, call.command, call.cwd, ctx.mode) == ("shell", "ls", "/w", "default")
    # Nothing to judge: a tool this hook does not handle, or an empty command.
    assert adapter.parse_payload({"tool_name": "Read", "tool_input": {}}, tools, engine)[0] is None
    assert adapter.parse_payload({"tool_name": "Bash", "tool_input": {"command": ""}}, tools, engine)[0] is None
    # No tool table = every tool is an MCP call.
    assert adapter.parse_payload({"tool_name": "mcp__h__t"}, None, engine)[0].kind == "mcp"


def test_a_template_less_profile_synthesizes_nothing():
    assert GENERIC.template_for("mcp__h__email_send") is None
    profile = GENERIC._replace(mcp_command_templates=(McpCommandTemplate("email_send", ("send",)),))
    assert profile.template_for("mcp__h__email_send").argv == ("send",)
