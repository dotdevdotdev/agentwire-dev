"""The policy ladder, lifted out of the five hook ``main()``s (#1209 M3, section 4.1).

``Guard.decide(ToolCall, Context) -> Verdict`` is the one place that turns an engine
finding into allow / ask / block: bypass demotion, the unverifiable carve-out, the
unattended fail-closed path with its grants, the kill switch, and the MCP screening.
It is pure — it does no I/O and never exits. The caller supplies facts (``ToolCall``,
``Context``) and renders the ``Verdict`` (``adapter.py``).

The engine functions the ladder needs are reached through ``engine``, an object that
exposes them as attributes (the ``_core`` module in-process, the hook's own namespace
when inlined). Taking it as an argument keeps this file free of imports from the
package, so it concatenates into a standalone hook, and lets the ladder be tested
against a stub engine — the bypass x unverifiable x unattended x grant matrix needs no
rule files and no subprocess.

Control-plane and protected-path checks run BEFORE the kill switch; nothing the
profile or the context supplies can loosen them.
"""

import re
from typing import Any, Dict, NamedTuple, Optional, Tuple

BYPASS = "bypass"
DEFAULT = "default"

ALLOW = "allow"
ASK = "ask"
BLOCK = "block"

# Rule id of the ambiguous-command fallback; an operand-position substitution is
# allowed unattended, a verb-concealing one (``unverifiable``) never is (#934/#925).
AMBIGUOUS_COMMAND_RULE = "core.ambiguous-command"

# Bridge for installs whose rules dir predates ``mcp-tools.yaml`` (#923): a stale user
# rules dir wins over the bundled copy and carries no ``mcpToolPolicies``, so per-tool
# classification loads empty. Until the heal installs the missing file, control-plane
# screening must not degrade below the #1036 posture — consulted ONLY when zero policies
# loaded. With policies present, the same shape lives as DATA (the ``mcp.writeish-name``
# entry in mcp-tools.yaml) and this constant is inert.
_WRITEISH_BRIDGE_RE = re.compile(
    r"(write|edit|delete|remove|move|rename|create|append|patch|mkdir|"
    r"rmdir|copy|upload|chmod|prune|kill|purge)",
    re.IGNORECASE,
)

# Per path-kind: the phrase in the block message.
_PATH_VERB = {"read": "read of", "write": "write to", "edit": "edit to"}


class ToolCall(NamedTuple):
    kind: str  # shell | read | write | edit | mcp
    tool_name: str
    command: str = ""  # shell: the command line
    paths: Tuple[str, ...] = ()  # read / write / edit: the target path
    tool_input: Optional[Dict[str, Any]] = None  # mcp: the call's arguments
    cwd: Optional[str] = None  # shell: the directory the harness reports


class Context(NamedTuple):
    mode: str = DEFAULT  # "bypass" = the human opted out of ask-escalation friction
    unattended: bool = False  # no human present (scheduler dispatch)


class Verdict(NamedTuple):
    decision: str  # allow | ask | block
    reason: str = ""
    rule_id: Optional[str] = None
    unverifiable: bool = False
    disabled: bool = False
    escape: bool = False
    allowed_by: Optional[str] = None
    # What the caller must do beyond the decision itself. All data: the ladder stays pure.
    audit: Optional[Tuple[str, Tuple[Any, ...], Dict[str, Any]]] = None  # (event, args, kwargs)
    stderr: Tuple[str, ...] = ()
    notify: Optional[Tuple[str, str, Optional[str]]] = None  # (subject, reason, rule_id)


def _audit(event: str, *args: Any, **kwargs: Any) -> Tuple[str, Tuple[Any, ...], Dict[str, Any]]:
    return (event, args, kwargs)


def _allowed(tool: str, subject: str, **kwargs: Any) -> Verdict:
    return Verdict(ALLOW, audit=_audit("allowed", tool, subject, user_approved=False, **kwargs))


def _iter_string_values(obj: Any):
    """Every string in ``obj``, iteratively (no recursion limit to crash on)."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            yield cur
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)


def _pathlike_args(tool_input: Dict[str, Any]):
    """String argument values that plausibly name a filesystem path.

    Whitespace-bearing strings are prose, ``://`` marks a URL; what remains is
    screened. A missed candidate degrades to today's behaviour (unscreened),
    never to a false block.
    """
    seen = set()
    for s in _iter_string_values(tool_input):
        if s in seen:
            continue
        seen.add(s)
        if not s or " " in s or "\t" in s or "\n" in s or "://" in s:
            continue
        if "/" in s or s.startswith("~"):
            yield s


class Guard:
    """Decides tool calls against a loaded ``config`` using ``engine``'s checks."""

    def __init__(self, config: Dict[str, Any], engine: Any) -> None:
        self.config = config
        self.e = engine

    def decide(self, call: ToolCall, ctx: Context) -> Verdict:
        if call.kind in _PATH_VERB:
            return self._decide_path(call)
        if call.kind == "shell":
            return self._decide_command(call, ctx, call.command, synthesized=False)
        if call.kind == "mcp":
            return self._decide_mcp(call, ctx)
        raise ValueError(f"unknown tool-call kind: {call.kind!r}")

    # -- Read / Write / Edit ---------------------------------------------

    def _decide_path(self, call: ToolCall) -> Verdict:
        e, path = self.e, call.paths[0]
        check = e.check_read_path if call.kind == "read" else e.check_path
        # The protected control plane is enforced even when safety is disabled, so
        # it must run BEFORE the kill-switch short-circuit (#466).
        blocked, reason = check(path, self.config)
        if blocked:
            return Verdict(
                BLOCK, reason,
                audit=_audit("blocked", call.tool_name, path, reason),
                stderr=(f"SECURITY: Blocked {_PATH_VERB[call.kind]} {reason}: {path}",),
            )
        if self.config["safety"].get("enabled", True) is False:
            return Verdict(ALLOW, disabled=True, audit=_audit("disabled", call.tool_name, path))
        return _allowed(call.tool_name, path)

    # -- Shell commands, and the shell command an outbound MCP tool runs --------

    def _decide_command(self, call: ToolCall, ctx: Context, command: str, synthesized: bool) -> Verdict:
        """``synthesized``: ``command`` stands in for an MCP tool call — it names no directory."""
        e, tool = self.e, call.tool_name
        if synthesized:
            result = e.check_command(command, self.config)
        else:
            result = e.check_command(command, self.config, cwd=call.cwd)
        decision, reason = result["decision"], result["reason"]
        rule_id, pattern = result.get("id"), result.get("pattern")
        shown = f"Command: {command[:100]}{'...' if len(command) > 100 else ''}"

        if decision == BLOCK:
            return Verdict(
                BLOCK, reason, rule_id,
                audit=_audit("blocked", tool, command, reason, pattern=pattern, rule_id=rule_id),
                stderr=(f"SECURITY: Blocked: {reason}", shown),
            )
        if decision == ASK and ctx.unattended:
            return self._unattended_ask(call, command, result, shown, synthesized)
        if decision == ASK and (ctx.mode != BYPASS or result.get("unverifiable")):
            # ``unverifiable`` (#934): a verb-concealing shape — eval, base64-pipe, a
            # substitution deciding what runs or feeding a wrapper's payload — keeps its
            # ask even under bypass. Bypass means "don't stop me for things you
            # recognize"; it never meant "don't stop me for things you can't see".
            return Verdict(ASK, reason, rule_id, unverifiable=bool(result.get("unverifiable")),
                           audit=_audit("asked", tool, command, reason))
        if result.get("escape"):
            return Verdict(ALLOW, escape=True,
                           audit=_audit("escape", tool, command, result.get("escape_reason") or ""))
        if result.get("disabled"):
            return Verdict(ALLOW, disabled=True, audit=_audit("disabled", tool, command))
        if synthesized:
            return _allowed(tool, command)
        return _allowed(tool, command, allowed_by=result.get("allowed_by"))

    def _unattended_ask(self, call: ToolCall, command: str, result: Dict[str, Any],
                        shown: str, synthesized: bool) -> Verdict:
        """No human present, so the interactive confirm is meaningless: fail closed.

        BLOCK + notify the owner, unless the matched rule carries an unattended grant —
        and, when that grant is path-scoped (#914), unless this command's target
        directory is inside it.
        """
        e, tool = self.e, call.tool_name
        reason, rule_id, pattern = result["reason"], result.get("id"), result.get("pattern")
        if not synthesized and rule_id == AMBIGUOUS_COMMAND_RULE and not result.get("unverifiable"):
            # #925 Part 3, against the #934 classifier: an operand-position substitution
            # is the most common phrasing in shell and every rule has already scanned its
            # masked form. Only VERB CONCEALMENT is non-redundant coverage, so the
            # unattended refusal is scoped to ``unverifiable``.
            return _allowed(tool, command)
        grants = e.resolve_unattended_grants(self.config)
        if synthesized:
            # A synthesized command names no directory, so a path-scoped grant cannot be
            # evaluated and must refuse (#914). Unscoped grants are unaffected.
            granted, why = e.unattended_grant_allows(
                rule_id, command, grants, e.safe_cwd(), pattern=pattern, scopeable=False,
            )
        else:
            granted, why = e.unattended_grant_allows(
                rule_id, command, grants, call.cwd or e.safe_cwd(), pattern=pattern,
            )
        if granted:
            return _allowed(tool, command)
        text = f"{reason} — {why}"
        return Verdict(
            BLOCK, reason, rule_id,
            audit=_audit("blocked", tool, command, f"unattended: {text}", pattern=pattern, rule_id=rule_id),
            stderr=(f"SECURITY: Blocked (unattended — no human to confirm): {reason}", shown,
                    f"Why: {why}") + self._grant_hint(rule_id, grants, scoped_form=not synthesized),
            notify=(command, text, rule_id),
        )

    def _grant_hint(self, rule_id: Optional[str], grants: Dict[str, Any], scoped_form: bool) -> Tuple[str, ...]:
        hint = self.e.PROFILE.unattended_grant_hint
        if not (hint and rule_id and rule_id not in grants):
            return ()
        text = hint.format(rule_id=rule_id)
        if scoped_form:
            text += f" (optionally scoped: `- {{id: {rule_id}, paths: [<dir>/]}}`)"
        return (text + ".",)

    # -- MCP tools ----------------------------------------------------------

    def _decide_mcp(self, call: ToolCall, ctx: Context) -> Verdict:
        e, tool_input = self.e, call.tool_input or {}
        command = e.synthesize_mcp_command(call.tool_name, tool_input)
        if command:
            return self._decide_command(call, ctx, command, synthesized=True)
        # Not a gated outbound tool: run the per-tool policy ladder (#923) and screen
        # path-valued arguments — an operation refused via Bash must not be permitted
        # via a tool.
        tool, config = call.tool_name, self.config
        policy = e.check_mcp_tool(tool, config)
        decision, reason, rule_id = policy["decision"], policy["reason"], policy.get("id")
        if decision == BLOCK:
            return Verdict(BLOCK, reason, rule_id, audit=_audit("blocked", tool, tool, reason),
                           stderr=(f"SECURITY: Blocked MCP tool: {reason}",))

        # Path screening runs before any allow/ask verdict: a protected path argument
        # blocks regardless of the tool's tier.
        mutates = policy.get("mutates")
        if mutates is None and not config.get("mcpToolPolicies"):
            mutates = bool(_WRITEISH_BRIDGE_RE.search(tool.split("__")[-1]))
        budget_exceeded = False
        try:
            with e.scan_deadline(e.SCAN_DEADLINE_SECONDS):
                screened = self._screen_tool_paths(tool, tool_input, bool(mutates))
            if screened is not None:
                return screened
        except e.ScanBudgetExceeded:
            # Arguments too large to screen in time: fail closed (#1108) — an unverifiable
            # ask, so bypass cannot demote it and unattended blocks. The kill switch turns
            # the budget off with everything else; a protected control-plane path anywhere
            # in the arguments is never exempted by the switch.
            if not e.kill_switch_off(config) or any(
                e.mentions_control_plane(s) for s in _iter_string_values(tool_input)
            ):
                budget_exceeded = True
                decision, rule_id = ASK, "core.scan-budget"
                reason = (
                    f"MCP tool arguments exceeded the {e.SCAN_DEADLINE_SECONDS:g}s screening "
                    "budget — too large to analyze safely; a human must confirm "
                    "(not demoted by bypass)"
                )

        if decision == ASK and ctx.unattended:
            return self._unattended_mcp_ask(tool, reason, rule_id)
        if decision == ASK and (ctx.mode != BYPASS or budget_exceeded):
            return Verdict(ASK, reason, rule_id, unverifiable=budget_exceeded,
                           audit=_audit("asked", tool, tool, reason))
        return Verdict(ALLOW)

    def _unattended_mcp_ask(self, tool: str, reason: str, rule_id: Optional[str]) -> Verdict:
        e = self.e
        # A tool call names no directory, so scopeable=False — a path-scoped grant
        # refuses rather than guessing (#914).
        grants = e.resolve_unattended_grants(self.config)
        granted, why = e.unattended_grant_allows(rule_id, tool, grants, e.safe_cwd(), scopeable=False)
        if granted:
            return _allowed(tool, tool)
        text = f"{reason} — {why}"
        return Verdict(
            BLOCK, reason, rule_id,
            audit=_audit("blocked", tool, tool, f"unattended: {text}", rule_id=rule_id),
            stderr=(f"SECURITY: Blocked (unattended — no human to confirm): {reason}",
                    f"Tool: {tool}", f"Why: {why}")
            + self._grant_hint(rule_id, grants, scoped_form=False),
            notify=(tool, text, rule_id),
        )

    def _screen_tool_paths(self, tool: str, tool_input: Dict[str, Any], writeish: bool) -> Optional[Verdict]:
        """A block Verdict when a path-valued argument violates the path ladders (#923).

        Zero-access secrets block on MENTION for every tool (same semantics as
        ``check_read_path`` — the path's mere presence in a call is the leak); the
        protected control plane is readable by design, so it is only enforced when the
        tool's POLICY says it mutates (``writeish``).
        """
        e = self.e
        allowed = e.load_allowed_paths(self.config)
        for p in _pathlike_args(tool_input):
            blocked, reason = e.check_read_path(p, self.config)
            if not blocked and writeish:
                blocked, reason = e.check_protected_path(p, allowed)
            if blocked:
                return Verdict(
                    BLOCK, reason, audit=_audit("blocked", tool, p, reason),
                    stderr=(f"SECURITY: Blocked MCP tool argument: {reason}: {p}",),
                )
        return None
