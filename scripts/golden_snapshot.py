#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""Golden snapshot oracle for the damage-control engine and its five hooks (#1209 M0).

Records ``(input, context) -> decision, rule_id`` for:

  * every corpus case (``corpus/cases/*.yaml``),
  * every engine input harvested from the safety tests (``corpus/golden/harvested.json``),
  * representative audit-log row shapes (the real ``audit_logger`` writers),

through the in-process engine (``agentwire.safety._core``) AND through the five
real hook entrypoints run as subprocesses with recorded payloads. Every later
milestone must reproduce ``corpus/golden/snapshot.json`` byte-identically; a
deliberate behaviour change lands as an explicit snapshot diff in its own PR.

    scripts/golden_snapshot.py record [--out PATH] [--layer engine|hooks|audit ...]
    scripts/golden_snapshot.py check                 # record to memory, diff vs committed
    scripts/golden_snapshot.py harvest               # refresh harvested.json from the safety tests

Determinism: the recorder pins HOME / PWD / AGENTWIRE_DIR to fixed, nonexistent-or-private
locations and strips timestamps, so two recordings are byte-identical. Decisions are
platform-dependent in exactly one place: path-case folding (macOS folds, Linux is exact-case,
#1217). Those rows are DISCOVERED (darwin and linux emulations disagree), stored per platform
under ``*_by_platform``, and the host's emulation is proven against its real behaviour on
every record, so a Linux value is derived from the rule and verified by Linux CI, not guessed.
Every other row is compared unweakened on every platform.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO / "corpus"
CASES_DIR = CORPUS_DIR / "cases"
GOLDEN_DIR = CORPUS_DIR / "golden"
SNAPSHOT = GOLDEN_DIR / "snapshot.json"
HARVESTED = GOLDEN_DIR / "harvested.json"
HOOKS_DIR = REPO / "agentwire" / "hooks" / "damage-control"
RULES_DIR = HOOKS_DIR / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"

# Fixed environment. FAKE_HOME is deliberately NOT under /tmp: core.yaml allowlists
# /tmp, which outranks zeroAccessPaths and would silently flip every ``~`` secret
# case to allow (the #893/#1121 trap). It need not exist.
FAKE_HOME = "/golden/home"
FAKE_CWD = "/golden/home/project"

SCHEMA_VERSION = 1
LAYERS = ("engine", "hooks", "audit")
HOOK_FOR_TOOL = {
    "Bash": "bash-tool-damage-control.py",
    "Edit": "edit-tool-damage-control.py",
    "Write": "write-tool-damage-control.py",
    "Read": "read-tool-damage-control.py",
    "mcp": "mcp-tool-damage-control.py",
}
# check_mcp_tool is never reached by the harvested tests (they drive the hook), so the
# recorder carries its own representative tool names: ask-tier teardown, ungated
# messaging, a write-shaped unknown name, a read-shaped unknown name, a non-mcp name.
MCP_TOOLS = (
    "mcp__agentwire__worktree_remove", "mcp__agentwire__session_kill", "mcp__agentwire__say",
    "mcp__agentwire__msg_send", "mcp__agentwire__sessions_list", "mcp__other__delete_everything",
    "mcp__other__list_things", "Task",
)

# Engine contexts. ``default`` is the shipped posture; ``kill-switch-off`` proves the
# protected-control-plane carve-out still blocks with the switch down.
ENGINE_CONTEXTS = ("default", "kill-switch-off")
HOOK_PERMISSION_MODES = {"interactive": "default", "bypass": "bypassPermissions"}

HARVEST_PATTERNS = (
    "tests/unit/test_audit_attribution.py",
    "tests/unit/test_audit_outcomes.py",
    "tests/unit/test_block_rows_carry_an_id.py",
    "tests/unit/test_brace_expansion_zero_access.py",
    "tests/unit/test_control_plane_protection.py",
    "tests/unit/test_damage_control_bypass.py",
    "tests/unit/test_damage_control_delete_recursive.py",
    "tests/unit/test_damage_control_hooks.py",
    "tests/unit/test_damage_control_sync.py",
    "tests/unit/test_heredoc_marker_liveness.py",
    "tests/unit/test_hook_fail_closed.py",
    "tests/unit/test_mcp_tool_policy.py",
    "tests/unit/test_onfailure_block.py",
    "tests/unit/test_path_case_folding.py",
    "tests/unit/test_powershell_matcher.py",
    "tests/unit/test_prose_operands.py",
    "tests/unit/test_rule_files_load.py",
    "tests/unit/test_safe_cwd.py",
    "tests/unit/test_scan_budget.py",
    "tests/unit/test_sed_script_is_data.py",
    "tests/unit/test_unattended_allow.py",
    "tests/unit/test_unattended_scoped_allow.py",
)

# --------------------------------------------------------------------------------------
# Harvest: record every engine input the safety tests feed the real engine.
# Loaded as a pytest plugin (``pytest -p golden_snapshot``) with GOLDEN_HARVEST_OUT set.
# --------------------------------------------------------------------------------------

_HARVEST_OUT = os.environ.get("GOLDEN_HARVEST_OUT")
_seen: Dict[str, set] = {"check_command": set(), "check_path": set(), "check_read_path": set(), "check_mcp_tool": set()}
_depth = [0]


def _install_harvest_wrappers() -> None:
    import agentwire.safety._core as core

    for name in _seen:
        original = getattr(core, name)

        def wrapper(first, *args, __orig=original, __name=name, **kwargs):
            if _depth[0] == 0 and isinstance(first, str):
                _seen[__name].add(first)
            _depth[0] += 1
            try:
                return __orig(first, *args, **kwargs)
            finally:
                _depth[0] -= 1

        wrapper.__name__ = name
        setattr(core, name, wrapper)


MAX_HARVESTED_LEN = 4000  # scan-budget stress inputs run to 1 MB; test_scan_budget owns those


def _portable(value: str) -> Optional[str]:
    """Map the real home to ``~``; drop per-run temp paths and stress-sized inputs."""
    if len(value) > MAX_HARVESTED_LEN:
        return None
    home = str(Path.home())
    if home and home != "/":
        value = value.replace(home, "~")
    if any(marker in value for marker in ("pytest-of-", "/var/folders/", "/private/var/", "/private/tmp/")):
        return None
    return value


def pytest_sessionfinish(session, exitstatus):  # pragma: no cover - plugin hook
    if not _HARVEST_OUT:
        return
    out: Dict[str, List[str]] = {}
    for name, values in _seen.items():
        kept = {p for v in values for p in [_portable(v)] if p is not None and p.strip()}
        out[name] = sorted(kept)
    Path(_HARVEST_OUT).write_text(json.dumps(out, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


if _HARVEST_OUT:  # inert unless the harvest command asked for it
    _install_harvest_wrappers()


def cmd_harvest(_args: argparse.Namespace) -> int:
    env = dict(os.environ, GOLDEN_HARVEST_OUT=str(HARVESTED), PYTHONPATH=str(Path(__file__).parent))
    cmd = [
        sys.executable, "-m", "pytest", "-q", "-p", "golden_snapshot", "-p", "no:cacheprovider",
        "-m", "not requires_tmux and not slow", *HARVEST_PATTERNS,
    ]
    proc = subprocess.run(cmd, cwd=REPO, env=env)
    if not HARVESTED.exists():
        print("harvest produced no output", file=sys.stderr)
        return 1
    counts = {k: len(v) for k, v in json.loads(HARVESTED.read_text()).items()}
    print(f"harvested {counts} (pytest exit {proc.returncode})")
    return 0 if proc.returncode == 0 else proc.returncode


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------

Item = Dict[str, Any]


def load_corpus_cases() -> List[Dict[str, Any]]:
    import yaml

    cases: List[Dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        cases.extend(yaml.safe_load(path.read_text()) or [])
    return cases


def collect_items() -> List[Item]:
    """Every distinct (tool, input, context) the recorder runs, in a stable order.

    ``tool`` is the hook-level tool name (Bash/Read/Edit/mcp); ``home`` / ``kill_switch`` come
    from a corpus case's context, harvested inputs use the defaults.
    """
    items: Dict[Tuple[str, str, str, bool], Item] = {}

    def add(tool: str, text: str, home: str = FAKE_HOME, kill_switch: bool = False) -> None:
        items.setdefault((tool, text, home, kill_switch), {
            "tool": tool, "input": text, "home": home, "kill_switch": kill_switch,
        })

    for case in load_corpus_cases():
        ctx = case.get("context") or {}
        home = ctx.get("home") or FAKE_HOME
        add(case["tool"], case["input"], home, bool(ctx.get("kill_switch")))
    harvested = json.loads(HARVESTED.read_text()) if HARVESTED.exists() else {}
    for text in harvested.get("check_command", []):
        add("Bash", text)
    for text in harvested.get("check_path", []):
        add("Edit", text)
    for text in harvested.get("check_read_path", []):
        add("Read", text)
    for text in sorted(set(harvested.get("check_mcp_tool", [])) | set(MCP_TOOLS)):
        add("mcp", text)
    return [items[k] for k in sorted(items)]


# --------------------------------------------------------------------------------------
# Engine layer (in-process)
# --------------------------------------------------------------------------------------


@contextlib.contextmanager
def pinned_env(home: str = FAKE_HOME) -> Iterator[None]:
    keys = ("HOME", "PWD", "AGENTWIRE_DIR", "AGENTWIRE_UNATTENDED", "AGENTWIRE_SESSION_ID", "TMUX", "TMUX_PANE")
    saved = {k: os.environ.get(k) for k in keys}
    # Session attribution (an audit row's session_id) must not leak the recorder's own tmux pane.
    os.environ.update(HOME=home, PWD=FAKE_CWD, AGENTWIRE_SESSION_ID="golden-session")
    for key in ("AGENTWIRE_UNATTENDED", "TMUX", "TMUX_PANE"):
        os.environ.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="golden-aw-") as aw:
        os.environ["AGENTWIRE_DIR"] = aw
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _engine_config(kill_switch: bool) -> Dict[str, Any]:
    from agentwire.safety._core import load_config

    config = load_config(RULES_DIR, TOOLDEFS_DIR)
    if not config.get("bashToolPatterns"):
        raise SystemExit("bundled rules failed to load")
    config["safety"] = {"enabled": not kill_switch, "disabled_rules": [], "unattended_allow": []}
    return config


def engine_decision(tool: str, text: str, config: Dict[str, Any]) -> Dict[str, Any]:
    from agentwire.safety import _core

    if tool == "Bash":
        r = _core.check_command(text, config, cwd=FAKE_CWD)
        return {"decision": r["decision"], "rule_id": r.get("id"),
                "protected": bool(r.get("protected")), "unverifiable": bool(r.get("unverifiable"))}
    if tool in ("Edit", "Write"):
        blocked, _ = _core.check_path(text, config)
        return {"decision": "block" if blocked else "allow", "rule_id": None}
    if tool == "Read":
        blocked, _ = _core.check_read_path(text, config)
        return {"decision": "block" if blocked else "allow", "rule_id": None}
    r = _core.check_mcp_tool(text, config)
    return {"decision": r["decision"], "rule_id": r.get("id")}


PATH_TOOLS = ("Edit", "Write", "Read")
VARIANTS = ("darwin", "linux")


def host_variant() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


@contextlib.contextmanager
def casefold_as(variant: str) -> Iterator[None]:
    """Emulate a platform's path-case behaviour.

    ``_core._casefolds_paths`` is the ONLY platform switch in the engine (macOS folds,
    Linux is exact-case except under a WSL ``/mnt/<drive>`` mount). Patching it lets one
    machine compute both platforms' verdicts; ``record_engine`` then proves the emulation
    against the host's REAL behaviour, so a lie fails loudly instead of being committed.
    """
    from agentwire.safety import _core

    original = _core._casefolds_paths
    if variant == "darwin":
        _core._casefolds_paths = lambda path: True
    else:
        _core._casefolds_paths = lambda path: bool(_core._DRVFS_RE.match(path))
    try:
        yield
    finally:
        _core._casefolds_paths = original


def record_engine(items: List[Item]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Return (rows identical on every platform, {variant: rows that differ by platform}).

    A row is platform-dependent exactly when the darwin and linux emulations disagree, so
    the set is discovered, never hand-tagged, and every other row is compared unweakened.
    """
    per_variant: Dict[str, Dict[str, Any]] = {v: {} for v in VARIANTS}
    real: Dict[str, Any] = {}
    for home in sorted({i["home"] for i in items}):
        with pinned_env(home):
            configs = {ks: _engine_config(ks) for ks in (False, True)}
            for item in items:
                if item["home"] != home:
                    continue
                for name in ENGINE_CONTEXTS:
                    key = record_key(item["tool"], item["input"], home, name)
                    config = configs[name == "kill-switch-off"]
                    real[key] = engine_decision(item["tool"], item["input"], config)
                    for variant in VARIANTS:
                        with casefold_as(variant):
                            per_variant[variant][key] = engine_decision(item["tool"], item["input"], config)
    wrong = [k for k in real if real[k] != per_variant[host_variant()][k]]
    if wrong:
        raise SystemExit(f"{host_variant()} emulation disagrees with real behaviour on {len(wrong)} rows: {wrong[:3]}")
    differing = {k for k in real if per_variant["darwin"][k] != per_variant["linux"][k]}
    invariant = {k: v for k, v in real.items() if k not in differing}
    return invariant, {v: {k: per_variant[v][k] for k in sorted(differing)} for v in VARIANTS}


def record_key(tool: str, text: str, home: str, context: str) -> str:
    return json.dumps([tool, text, home, context], ensure_ascii=False)


# --------------------------------------------------------------------------------------
# Hook layer (subprocess, real entrypoints)
# --------------------------------------------------------------------------------------


def hook_payload(tool: str, text: str, permission_mode: str) -> Dict[str, Any]:
    base = {"session_id": "00000000-0000-0000-0000-000000000000", "cwd": FAKE_CWD,
            "permission_mode": permission_mode, "hook_event_name": "PreToolUse"}
    if tool == "Bash":
        return dict(base, tool_name="Bash", tool_input={"command": text})
    if tool in ("Edit", "Write"):
        return dict(base, tool_name=tool, tool_input={"file_path": text})
    if tool == "Read":
        return dict(base, tool_name="Read", tool_input={"file_path": text})
    return dict(base, tool_name=text, tool_input={})


def run_hook(tool: str, text: str, home: str, mode: str, unattended: bool) -> Dict[str, Any]:
    """Run one hook as a subprocess against a private AGENTWIRE_DIR; return decision + audit row."""
    hook = HOOKS_DIR / HOOK_FOR_TOOL[tool if tool in HOOK_FOR_TOOL else "mcp"]
    with tempfile.TemporaryDirectory(prefix="golden-hook-") as aw, tempfile.TemporaryDirectory(prefix="golden-cwd-") as cwd:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "PWD": FAKE_CWD,
               "AGENTWIRE_DIR": aw, "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"}
        if unattended:
            env["AGENTWIRE_UNATTENDED"] = "1"
        payload = hook_payload(tool, text, HOOK_PERMISSION_MODES[mode])
        proc = subprocess.run(
            [sys.executable, str(hook)], input=json.dumps(payload), capture_output=True,
            text=True, env=env, cwd=cwd, timeout=120,
        )
        verdict = "allow"
        if proc.returncode == 2:
            verdict = "block"
        elif proc.returncode == 0 and proc.stdout.strip():
            try:
                verdict = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
            except (ValueError, KeyError, TypeError):
                verdict = "unparsed"
        elif proc.returncode not in (0, 2):
            verdict = f"crash-{proc.returncode}"
        return {"exit": proc.returncode, "decision": verdict, **audit_summary(Path(aw))}


def audit_summary(aw: Path) -> Dict[str, Any]:
    rows = []
    for log in sorted((aw / "logs" / "damage-control").glob("*.jsonl")):
        rows += [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    if not rows:
        return {"audit": None}
    row = rows[-1]
    return {"audit": row.get("decision"), "rule_id": row.get("rule_id")}


def derived_path_hook_row(engine_row: Dict[str, Any]) -> Dict[str, Any]:
    """The hook row a path tool yields for an engine verdict (verified against the real run)."""
    if engine_row["decision"] == "block":
        return {"exit": 2, "decision": "block", "audit": "blocked", "rule_id": None}
    return {"exit": 0, "decision": "allow", "audit": "allowed", "rule_id": None}


def record_hooks(items: List[Item], engine: Dict[str, Any],
                 by_platform: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    jobs: List[Tuple[str, str, str, str, str, bool]] = []
    for item in items:
        if item["kill_switch"]:
            continue  # the kill switch is host state, not a hook payload field
        tool, text, home = item["tool"], item["input"], item["home"]
        jobs.append((record_key(tool, text, home, "interactive"), tool, text, home, "interactive", False))
        ask = engine.get(record_key(tool, text, home, "default"), {}).get("decision") == "ask"
        if tool == "Bash" and ask:
            # Only an `ask` can change with the posture, so only asks are re-run.
            jobs.append((record_key(tool, text, home, "bypass"), tool, text, home, "bypass", False))
            jobs.append((record_key(tool, text, home, "unattended"), tool, text, home, "interactive", True))
    results: Dict[str, Any] = {}
    workers = min(8, os.cpu_count() or 2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_hook, tool, text, home, mode, unattended): key
                   for key, tool, text, home, mode, unattended in jobs}
        for fut in concurrent.futures.as_completed(futures):
            results[futures[fut]] = fut.result()
    # A path-tool row whose engine verdict differs by platform: the host's real run is
    # authoritative for the host's variant; the other variant is derived and the derivation
    # is proven against the real run, so it cannot drift from what a hook actually emits.
    platform_rows: Dict[str, Dict[str, Any]] = {v: {} for v in VARIANTS}
    for key in sorted(results):
        tool, text, home, mode = json.loads(key)
        engine_key = record_key(tool, text, home, "default")
        if tool in PATH_TOOLS and engine_key in by_platform["darwin"]:
            for variant in VARIANTS:
                platform_rows[variant][key] = derived_path_hook_row(by_platform[variant][engine_key])
            if platform_rows[host_variant()][key] != results[key]:
                raise SystemExit(f"derived hook row disagrees with the real run for {key}: "
                                 f"{platform_rows[host_variant()][key]} != {results[key]}")
            del results[key]
    return results, platform_rows


# --------------------------------------------------------------------------------------
# Audit layer: the real audit_logger writers, one row per decision shape
# --------------------------------------------------------------------------------------

_AUDIT_SHAPES: List[Tuple[str, Dict[str, Any]]] = [
    ("log_blocked", {"tool": "Bash", "command": "git reset --hard", "reason": "r", "pattern": "p", "rule_id": "git.reset-hard"}),
    ("log_blocked", {"tool": "Bash", "command": "x", "reason": "r"}),
    ("log_asked", {"tool": "Bash", "command": "x", "reason": "r", "pattern": "p"}),
    ("log_allowed", {"tool": "Bash", "command": "ls", "user_approved": False}),
    ("log_allowed", {"tool": "Bash", "command": "ls", "user_approved": False, "allowed_by": "allowedPaths"}),
    ("log_escape", {"tool": "Bash", "command": "x # allow: why", "escape_reason": "why"}),
    ("log_disabled", {"tool": "Bash", "command": "x"}),
    ("log_blocked", {"tool": "Edit", "command": "/golden/home/.agentwire/damagecontrol.yml", "reason": "control-plane"}),
]


def record_audit() -> Dict[str, Any]:
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        import audit_logger
    finally:
        sys.path.remove(str(HOOKS_DIR))
    out: Dict[str, Any] = {}
    with pinned_env():
        for index, (writer, kwargs) in enumerate(_AUDIT_SHAPES):
            audit_logger.HOOK_INPUT = {"session_id": "00000000-0000-0000-0000-000000000000"}
            getattr(audit_logger, writer)(**kwargs)
            logs = sorted(audit_logger.get_log_dir().glob("*.jsonl"))
            row = json.loads(logs[-1].read_text().splitlines()[-1])
            row.pop("timestamp", None)
            out[f"{index:02d}:{writer}:{json.dumps(kwargs, sort_keys=True)}"] = row
    return out


# --------------------------------------------------------------------------------------
# Record / check
# --------------------------------------------------------------------------------------


def record(layers: Tuple[str, ...]) -> Dict[str, Any]:
    items = collect_items()
    snapshot: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "layers": list(layers),
        "counts": {},
    }
    engine, engine_by_platform = record_engine(items)
    if "engine" in layers:
        snapshot["engine"] = engine
        snapshot["engine_by_platform"] = engine_by_platform
    if "hooks" in layers:
        snapshot["hooks"], snapshot["hooks_by_platform"] = record_hooks(items, engine, engine_by_platform)
    if "audit" in layers:
        snapshot["audit"] = record_audit()
    snapshot["counts"] = {k: len(snapshot[k]) for k in LAYERS if k in snapshot}
    for key in ("engine_by_platform", "hooks_by_platform"):
        if key in snapshot:
            snapshot["counts"][key] = len(snapshot[key]["darwin"])
    return snapshot


def dump(snapshot: Dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def cmd_record(args: argparse.Namespace) -> int:
    layers = tuple(args.layer) if args.layer else LAYERS
    text = dump(record(layers))
    out = Path(args.out) if args.out else SNAPSHOT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    committed = json.loads(SNAPSHOT.read_text())
    fresh = record(tuple(committed["layers"]))
    old, new = dump(committed), dump(fresh)
    if old == new:
        print("snapshot reproduced byte-identically")
        return 0
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "committed", "fresh", lineterm="", n=0))
    print("\n".join(diff[:200]))
    print(f"{len(diff)} differing lines")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="record the snapshot")
    rec.add_argument("--out")
    rec.add_argument("--layer", action="append", choices=LAYERS)
    rec.set_defaults(func=cmd_record)
    sub.add_parser("check", help="re-record and diff against the committed snapshot").set_defaults(func=cmd_check)
    sub.add_parser("harvest", help="refresh harvested.json from the safety tests").set_defaults(func=cmd_harvest)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
