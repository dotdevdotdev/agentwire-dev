"""The enforced rule set as ``{stable id: "tier:fingerprint"}``, and the diff between two of them (#1209 M5).

``install.json`` records one of these at every acknowledged install. ``status`` compares the
current set against it ("ruleset changed since last acknowledged") and ``init --refresh``
prints the diff by id. A refresh that WIDENS what is permitted must say so (#916): a removed
rule, a lowered tier, a new allow-path and a new default unattended grant are marked ``!!``.

Standard library only.
"""

import hashlib
import json
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

TIER_RANK = {"block": 3, "ask": 2, "allow": 1}
_PATH_LISTS = (("zeroAccessPaths", "block"), ("readOnlyPaths", "block"), ("noDeletePaths", "block"))
ALLOW_NAMESPACE = "allowedPaths:"


class Change(NamedTuple):
    loosening: bool
    text: str


def _fingerprint(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _label(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("path", "pattern", "entry", "id"):
            if isinstance(item.get(key), str):
                return item[key]
    return json.dumps(item, sort_keys=True, default=str)


def digest(config: Dict[str, Any]) -> Dict[str, str]:
    """Every enforced rule of ``config`` (an effective config) keyed by its stable id."""
    out: Dict[str, str] = {}
    for rule in config.get("bashToolPatterns") or []:
        if isinstance(rule, dict) and rule.get("id"):
            out.setdefault(rule["id"], f"{'ask' if rule.get('ask') else 'block'}:{_fingerprint(rule.get('pattern'))}")
    for policy in config.get("mcpToolPolicies") or []:
        if isinstance(policy, dict) and policy.get("id"):
            match = policy.get("match") or policy.get("tool") or policy.get("pattern")
            out.setdefault(policy["id"], f"{policy.get('tier', 'allow')}:{_fingerprint(match)}")
    for key, tier in _PATH_LISTS:
        for item in config.get(key) or []:
            out.setdefault(f"{key}:{_label(item)}", f"{tier}:-")
    for item in config.get("allowedPaths") or []:
        out.setdefault(f"{ALLOW_NAMESPACE}{_label(item)}", "allow:-")
    return out


def _tier(value: Optional[str]) -> str:
    return (value or "").split(":", 1)[0]


def diff(old: Dict[str, str], new: Dict[str, str],
         old_grants: Iterable[str] = (), new_grants: Iterable[str] = ()) -> List[Change]:
    """Added / removed / changed ids between two digests, plus new default grants. Loosening is flagged."""
    changes: List[Change] = []
    for rid in sorted(set(old) | set(new)):
        before, after = old.get(rid), new.get(rid)
        if before == after:
            continue
        allow_entry = rid.startswith(ALLOW_NAMESPACE)
        if before is None:
            changes.append(Change(allow_entry, f"+ {rid} ({_tier(after)})" + (" widens what is allowed" if allow_entry else "")))
        elif after is None:
            changes.append(Change(not allow_entry, f"- {rid} (was {_tier(before)})"))
        elif _tier(before) != _tier(after):
            lowered = TIER_RANK.get(_tier(after), 0) < TIER_RANK.get(_tier(before), 0)
            changes.append(Change(lowered, f"~ {rid} {_tier(before)} -> {_tier(after)}"))
        else:
            changes.append(Change(False, f"~ {rid} pattern changed (review: a narrower pattern permits more)"))
    for grant in sorted(set(new_grants) - set(old_grants)):
        changes.append(Change(True, f"+ default unattended grant {grant}"))
    return changes


def render(changes: List[Change]) -> List[str]:
    return [f"{'!!' if c.loosening else '  '} {c.text}" for c in changes]


def has_loosening(changes: List[Change]) -> bool:
    return any(c.loosening for c in changes)


def summary(changes: List[Change]) -> Tuple[int, int]:
    """``(total changes, loosening changes)``."""
    return len(changes), sum(1 for c in changes if c.loosening)
