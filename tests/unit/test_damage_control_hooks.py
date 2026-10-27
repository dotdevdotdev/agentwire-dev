"""The shipped damage-control hooks, driven the way Claude Code drives them.

Each test runs a hook script as a subprocess — JSON on stdin, verdict read from
the exit code and stdout: exit 2 = block, exit 0 + an ``ask`` JSON = confirm,
exit 0 + no output = allow. Decision-matrix coverage over the rule set lives in
the bypass corpus (``test_damage_control_bypass.py``); this file pins the
surface around it: posture modes, the unattended guardrail, the per-tool
hooks, and the audit record. At most two hook runs per test, to stay under 1 s.

HOME is deliberately NOT the pytest tmp dir: on the Linux CI runner tmp lives
under /tmp, and the shipped allowlist's /tmp/* entry then re-permits every
probe target. The path never needs to exist (the hooks match strings), and
audit logs are redirected into the real tmp dir via AGENTWIRE_DIR.
"""

import json
import subprocess
import sys

from tests.conftest import HOOKS_DIR

HERMETIC_HOME = "/home/agentwire-hermetic"
CONTROL_PLANE = HERMETIC_HOME + "/.claude/settings.json"
BASH = HOOKS_DIR / "bash-tool-damage-control.py"
EDIT = HOOKS_DIR / "edit-tool-damage-control.py"
WRITE = HOOKS_DIR / "write-tool-damage-control.py"
READ = HOOKS_DIR / "read-tool-damage-control.py"
MCP = HOOKS_DIR / "mcp-tool-damage-control.py"


def _run(hook, payload, tmp_path, *, unattended=False, allow=None, raw=None):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": HERMETIC_HOME,
        "AGENTWIRE_DIR": str(tmp_path / ".agentwire"),
    }
    if unattended:
        env["AGENTWIRE_UNATTENDED"] = "1"
    if allow is not None:
        env["AGENTWIRE_UNATTENDED_ALLOW"] = allow
    return subprocess.run(
        [sys.executable, str(hook)],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=15,
    )


def bash(command, tmp_path, mode="bypassPermissions", **kw):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "permission_mode": mode}
    return _run(BASH, payload, tmp_path, **kw)


def mcp(tool, tool_input, tmp_path, mode="bypassPermissions", **kw):
    payload = {"tool_name": tool, "tool_input": tool_input, "permission_mode": mode}
    return _run(MCP, payload, tmp_path, **kw)


def _edit(tool, path, tmp_path):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return _run(EDIT, {"tool_name": tool, "tool_input": {key: path}}, tmp_path)


def _write(path, tmp_path):
    return _run(WRITE, {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}, tmp_path)


def _read(path, tmp_path):
    return _run(READ, {"tool_name": "Read", "tool_input": {"file_path": path}}, tmp_path)


def verdict(proc):
    if proc.returncode == 2:
        return "block"
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    if not proc.stdout.strip():
        return "allow"
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


# ---------------------------------------------------------------------------
# Bash hook — fail closed, posture modes
# ---------------------------------------------------------------------------


def test_invalid_json_blocks_rather_than_failing_open(tmp_path):
    # exit 1 is NON-blocking to the harness (fail-open); a bad payload must block (#1108)
    proc = _run(BASH, None, tmp_path, raw="not json {{{")
    assert proc.returncode == 2
    assert "invalid JSON" in proc.stderr


def test_ask_tier_confirms_interactively_and_bypass_demotes_it(tmp_path):
    assert verdict(bash("gh pr merge 5", tmp_path, mode="default")) == "ask"
    assert verdict(bash("gh pr merge 5", tmp_path)) == "allow"


def test_unverifiable_command_is_never_demoted_by_bypass_or_auto(tmp_path):
    """#934: a substitution that decides what runs keeps its confirm in the
    postures that demote an ordinary ask."""
    command = 'psql -c "$(cat drop.sql)"'
    seen = {mode: verdict(bash(command, tmp_path, mode=mode)) for mode in ("bypassPermissions", "auto")}
    assert seen == {"bypassPermissions": "ask", "auto": "ask"}


# ---------------------------------------------------------------------------
# Bash hook — the unattended guardrail (#401, #804, #925)
# ---------------------------------------------------------------------------


def test_unattended_ask_tier_fails_closed_and_says_why(tmp_path):
    proc = bash("gh pr merge 5", tmp_path, unattended=True)
    assert proc.returncode == 2
    assert "unattended" in proc.stderr.lower()
    proc = bash("agentwire quo --to +15551234567", tmp_path, unattended=True)
    assert proc.returncode == 2
    assert "outbound.agentwire-quo" in proc.stderr


def test_unattended_default_grants_let_work_and_report_back_proceed(tmp_path):
    for command in ("git commit -m 'x'", "agentwire email --to anyone@example.com"):
        assert verdict(bash(command, tmp_path, unattended=True)) == "allow", command


def test_unattended_per_task_grant_allows_its_rule_and_no_hard_block(tmp_path):
    grant = "tooldef.github-cli-merge-a-pull-request"
    assert verdict(bash("gh pr merge 5", tmp_path, unattended=True, allow=grant)) == "allow"
    assert verdict(bash("git push --force", tmp_path, unattended=True, allow=grant)) == "block"


def test_unattended_operand_substitution_runs_but_concealment_blocks(tmp_path):
    """#925: the for-loop shape (54% of all unattended blocks) runs; eval does not."""
    loop = 'for p in a b; do echo "$(basename $p)"; done'
    assert verdict(bash(loop, tmp_path, unattended=True)) == "allow"
    assert verdict(bash('eval "$PAYLOAD"', tmp_path, unattended=True)) == "block"


def test_unattended_block_is_written_to_the_audit_log(tmp_path):
    """Throttling the owner EMAIL (#925) must never throttle the record."""
    assert bash("gh pr merge 5", tmp_path, unattended=True).returncode == 2
    rows = [
        json.loads(line)
        for f in (tmp_path / ".agentwire" / "logs" / "damage-control").glob("*.jsonl")
        for line in f.read_text().splitlines()
    ]
    assert any(
        r.get("rule_id") == "tooldef.github-cli-merge-a-pull-request" and "gh pr merge 5" in r.get("command", "")
        for r in rows
    ), rows


# ---------------------------------------------------------------------------
# Edit / Write / NotebookEdit / Read hooks
# ---------------------------------------------------------------------------


def test_edit_and_notebookedit_refuse_the_control_plane(tmp_path):
    """An operation refused via Edit must be refused via NotebookEdit (#923)."""
    codes = {tool: _edit(tool, CONTROL_PLANE, tmp_path).returncode for tool in ("Edit", "NotebookEdit")}
    assert codes == {"Edit": 2, "NotebookEdit": 2}


def test_ordinary_edits_pass(tmp_path):
    codes = [_edit("Edit", HERMETIC_HOME + "/proj/a.py", tmp_path).returncode,
             _edit("NotebookEdit", HERMETIC_HOME + "/proj/analysis.ipynb", tmp_path).returncode]
    assert codes == [0, 0]


def test_write_hook_refuses_the_control_plane_and_passes_source(tmp_path):
    codes = [_write(CONTROL_PLANE, tmp_path).returncode, _write(HERMETIC_HOME + "/proj/b.py", tmp_path).returncode]
    assert codes == [2, 0]


def test_read_hook_refuses_a_secret_and_passes_source(tmp_path):
    codes = [_read(HERMETIC_HOME + "/.ssh/id_rsa", tmp_path).returncode,
             _read(HERMETIC_HOME + "/proj/main.py", tmp_path).returncode]
    assert codes == [2, 0]


# ---------------------------------------------------------------------------
# MCP hook — per-tool policy (#923) and path screening
# ---------------------------------------------------------------------------

RM = ("mcp__agentwire__worktree_remove", {"name": "feature-x"})


def test_mcp_teardown_confirms_interactively_and_bypass_demotes_it(tmp_path):
    assert verdict(mcp(*RM, tmp_path, mode="default")) == "ask"
    assert verdict(mcp(*RM, tmp_path)) == "allow"


def test_mcp_teardown_and_kill_fail_closed_unattended(tmp_path):
    proc = mcp(*RM, tmp_path, unattended=True)
    assert proc.returncode == 2 and "mcp.agentwire.worktree-remove" in proc.stderr
    assert mcp("mcp__agentwire__session_kill", {"session": "x"}, tmp_path, unattended=True).returncode == 2


def test_mcp_unattended_grants_are_unscoped_only(tmp_path):
    """A tool call has no filesystem target — a path-scoped grant refuses (#914)."""
    assert mcp(*RM, tmp_path, unattended=True, allow="mcp.agentwire.worktree-remove").returncode == 0
    scoped = json.dumps([{"id": "mcp.agentwire.worktree-remove", "paths": ["/srv/repos/"]}])
    assert mcp(*RM, tmp_path, unattended=True, allow=scoped).returncode == 2


def test_mcp_secret_and_control_plane_write_arguments_are_refused(tmp_path):
    codes = [
        mcp("mcp__filesystem__read_file", {"path": HERMETIC_HOME + "/.ssh/id_rsa"}, tmp_path).returncode,
        mcp("mcp__filesystem__write_file", {"path": CONTROL_PLANE, "content": "x"}, tmp_path).returncode,
    ]
    assert codes == [2, 2]


def test_mcp_messaging_and_control_plane_reads_stay_open(tmp_path):
    """The priced false-refusal half: a report-back that mentions a protected
    path must never dead-loop an unattended session, and the control plane is
    readable by design."""
    msg = mcp("mcp__agentwire__msg_send",
              {"to": "orch", "kind": "done", "message": "see ~/.agentwire/.env and docs/x.md",
               "ref": "https://example.com/a/b"},
              tmp_path, unattended=True)
    read = mcp("mcp__filesystem__read_file", {"path": CONTROL_PLANE}, tmp_path)
    assert [msg.returncode, read.returncode] == [0, 0], (msg.stderr, read.stderr)
