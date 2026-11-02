"""Runner for the damage-control corpus (``corpus/cases/*.yaml``, #1209 M0).

Pytest-free on purpose: the same loader, linter and evaluator must survive the move
to a standalone repo. ``tests/unit/test_corpus.py`` is the only pytest consumer.
"""

from __future__ import annotations

import copy
import functools
import json
import os
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import jsonschema
import yaml
from damage_control.engine import bundled_rule_sources, bundled_tooldefs_dir

REPO = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO / "corpus"
CASES_DIR = CORPUS_DIR / "cases"
SCHEMA_PATH = CORPUS_DIR / "schema.json"
RULES_DIR = bundled_rule_sources()
TOOLDEFS_DIR = bundled_tooldefs_dir()

# Deliberately not under /tmp: core.yaml allowlists /tmp, which outranks zeroAccessPaths.
DEFAULT_HOME = "/golden/home"
FAKE_CWD = "/golden/home/project"

Case = Dict[str, Any]


def load_cases() -> List[Case]:
    cases: List[Case] = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        cases.extend(yaml.safe_load(path.read_text()) or [])
    return cases


def lint(cases: List[Case]) -> List[str]:
    """Schema-validate every case; also reject duplicate ids and a class with no cases."""
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))
    problems: List[str] = []
    seen: Dict[str, int] = defaultdict(int)
    for index, case in enumerate(cases):
        label = case.get("id", f"#{index}") if isinstance(case, dict) else f"#{index}"
        for err in sorted(validator.iter_errors(case), key=lambda e: list(e.path)):
            where = ".".join(str(p) for p in err.path) or "<case>"
            problems.append(f"{label}: {where}: {err.message}")
        if isinstance(case, dict) and "id" in case:
            seen[case["id"]] += 1
    problems += [f"{cid}: duplicate id" for cid, n in sorted(seen.items()) if n > 1]
    return problems


@contextmanager
def _pinned_env(home: str) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in ("HOME", "PWD")}
    os.environ.update(HOME=home, PWD=FAKE_CWD)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@functools.lru_cache(maxsize=None)
def _base_config(ruleset: str) -> Dict[str, Any]:
    from damage_control.engine import load_config

    config = load_config(RULES_DIR, TOOLDEFS_DIR if ruleset == "full" else None)
    if not config.get("bashToolPatterns"):
        raise RuntimeError("bundled rules failed to load")
    return config


def _config(ctx: Dict[str, Any]) -> Dict[str, Any]:
    config = dict(_base_config(ctx.get("ruleset", "full")))
    config["safety"] = {
        "enabled": not ctx.get("kill_switch", False),
        "disabled_rules": [],
        "unattended_allow": [],
    }
    if ctx.get("allow_all_paths"):
        config["allowedPaths"] = [{"path": "*", "allow": "all"}]
    return config


def _as_set(value: Any) -> set:
    return set(value) if isinstance(value, list) else {value}


def observe(case: Case) -> Dict[str, Any]:
    """Run the case through the real engine; return every fact an expectation can name.

    Memoized on the engine-relevant fields: the must-fail controls re-evaluate each case
    against a contradicted expectation and must not pay the engine twice.
    """
    return _observe(case["tool"], case["input"], json.dumps(case["context"], sort_keys=True))


@functools.lru_cache(maxsize=None)
def _observe(tool: str, text: str, context_json: str) -> Dict[str, Any]:
    from damage_control import engine as _core

    ctx = json.loads(context_json)
    config = _config(ctx)
    with _pinned_env(ctx.get("home", DEFAULT_HOME)):
        if tool == "Bash":
            r = _core.check_command(text, config)
            return {"decision": r["decision"], "protected_is_true": r.get("protected") is True,
                    "protected_truthy": bool(r.get("protected")), "unverifiable": bool(r.get("unverifiable"))}
        blocked, reason = (_core.check_read_path(text, config) if tool == "Read"
                           else _core.check_path(text, config))
        facts = {"blocked": blocked is True, "reason": reason or ""}
        if tool in ("Edit", "Write"):
            facts["control_plane"] = bool(_core.is_protected_control_plane(text))
        return facts


def failures(case: Case, expect: Dict[str, Any] = None) -> List[str]:
    """Return what in ``expect`` (default: the case's own) the engine did not deliver."""
    expect = case["expect"] if expect is None else expect
    got = observe(case)
    bad: List[str] = []
    if "decision" in expect and got["decision"] not in _as_set(expect["decision"]):
        bad.append(f"decision {got['decision']!r} not in {sorted(_as_set(expect['decision']))}")
    if "decision_not" in expect and got["decision"] in _as_set(expect["decision_not"]):
        bad.append(f"decision {got['decision']!r} is forbidden")
    if "protected" in expect:
        # `true` means exactly True; `false` means no truthy protected flag at all.
        holds = got["protected_is_true"] if expect["protected"] else not got["protected_truthy"]
        if not holds:
            bad.append(f"protected={got['protected_truthy']!r}, expected {expect['protected']}")
    if "unverifiable" in expect and got["unverifiable"] is not expect["unverifiable"]:
        bad.append(f"unverifiable={got['unverifiable']}, expected {expect['unverifiable']}")
    if "blocked" in expect and got["blocked"] is not expect["blocked"]:
        bad.append(f"blocked={got['blocked']}, expected {expect['blocked']}")
    if "reason_contains" in expect and expect["reason_contains"] not in got["reason"]:
        bad.append(f"reason {got['reason']!r} lacks {expect['reason_contains']!r}")
    if "control_plane" in expect and got["control_plane"] is not expect["control_plane"]:
        bad.append(f"control_plane={got['control_plane']}, expected {expect['control_plane']}")
    return bad


def contradiction(expect: Dict[str, Any]) -> Dict[str, Any]:
    """The negation of every clause. A case whose own expectation holds MUST fail this.

    This is the must-fail control: if the evaluator ever stops reading a clause (a lost
    expectation), the contradiction passes and the control test goes red.
    """
    out: Dict[str, Any] = {}
    for key, value in expect.items():
        if key == "decision":
            out["decision_not"] = copy.deepcopy(value)
        elif key == "decision_not":
            out["decision"] = copy.deepcopy(value)
        elif key == "reason_contains":
            out[key] = "\x00no-such-reason\x00"
        else:
            out[key] = not value
    return out


def by_class(cases: List[Case]) -> List[Tuple[str, List[Case]]]:
    groups: Dict[str, List[Case]] = defaultdict(list)
    for case in cases:
        groups[case["class"]].append(case)
    return sorted(groups.items())
