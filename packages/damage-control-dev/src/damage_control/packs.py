"""Pack/group state in the protected ``~/.agentwire/damagecontrol.yml`` (#1141).

``packs:`` lists the enabled vendor packs; ``disabled_groups:`` lists opted-out
default groups. A missing ``packs:`` key means *undecided* (a pre-packs install):
heal migrates it by recording every pack that already has files installed as
enabled, so upgrading changes no decision. Once the key exists it is
authoritative, even when empty.

The hook reads the same keys at run time (``engine.load_safety_config``) and skips the
bundled files of whatever is off (``groups.skipped_stems``): the bundle is immutable, so
"pack off" is "not loaded", never "not installed". An absent ``packs:`` key loads only the
host's default packs (#1209 M5).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from damage_control.groups import Entry, default_packs, registry

PACKS_KEY = "packs"
GROUPS_KEY = "disabled_groups"


def read_state(path: Path) -> Dict[str, Any]:
    """``{"packs": [...] | None, "disabled_groups": [...], "broken": str}``.

    ``packs`` None = undecided. ``broken`` is a non-empty message when the file
    exists but does not parse: the hook fails toward protection (every group and
    pack on), and this file is never migrated over or written to,
    so a typo cannot get overwritten.
    """
    import yaml

    data: Any = {}
    broken = ""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError, yaml.YAMLError) as e:
        data, broken = {}, f"{path.name} does not parse ({type(e).__name__}); packs/groups treated as all on"
    if not isinstance(data, dict):
        data, broken = {}, broken or f"{path.name} is not a mapping; packs/groups treated as all on"
    packs = data.get(PACKS_KEY)
    groups = data.get(GROUPS_KEY)
    return {
        PACKS_KEY: [str(p) for p in packs] if isinstance(packs, list) else None,
        GROUPS_KEY: [str(g) for g in groups] if isinstance(groups, list) else [],
        "broken": broken,
    }


def _render(key: str, values: List[str]) -> List[str]:
    if not values:
        return [f"{key}: []"]
    return [f"{key}:"] + [f"  - {v}" for v in sorted(values)]


def _replace_key(lines: List[str], key: str, values: List[str]) -> List[str]:
    """Replace (or append) one top-level block, leaving every other line alone."""
    out: List[str] = []
    i, replaced = 0, False
    while i < len(lines):
        if re.match(rf"^{re.escape(key)}\s*:", lines[i]):
            i += 1
            while i < len(lines) and lines[i].strip() and lines[i][0] in " \t-":
                i += 1
            out.extend(_render(key, values))
            replaced = True
            continue
        out.append(lines[i])
        i += 1
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.extend(_render(key, values))
    return out


def write_state(path: Path, packs: Optional[List[str]] = None,
                disabled_groups: Optional[List[str]] = None) -> None:
    """Write the given keys into the policy file, preserving everything else."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    if packs is not None:
        lines = _replace_key(lines, PACKS_KEY, packs)
    if disabled_groups is not None:
        lines = _replace_key(lines, GROUPS_KEY, disabled_groups)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def entry_active(entry: Entry, state: Dict[str, Any]) -> bool:
    """Whether the entry's files belong on this machine."""
    if state.get("broken"):
        return True
    if entry.kind == "pack":
        return entry.name in (state[PACKS_KEY] or [])
    return entry.kernel or entry.name not in state[GROUPS_KEY]


def migrate_packs(state: Dict[str, Any], installed: Dict[str, Set[str]]) -> List[str]:
    """Packs to record as enabled for a pre-packs install, or ``[]``.

    Only while ``state["packs"]`` is None. A fresh machine starts with every pack
    off except the host's defaults (``default_packs()``). On an
    existing machine (something is already installed)
    machine EVERY pack is enabled, not just those with files present: before
    packs, heal reinstalled any missing bundled file, so a partial install
    (a deleted rules file, a pack with no tooldef) must come out of an upgrade
    with the whole pack, never silently without it.
    """
    if state[PACKS_KEY] is not None or state.get("broken"):
        return []
    if not any(installed.get(section) for section in ("rules", "tooldefs")):
        return sorted(default_packs())
    return sorted(e.name for e in registry().values() if e.kind == "pack")


def effective_state(path: Path) -> Dict[str, Any]:
    """The state the hook acts on: an undecided policy (no ``packs:`` key) loads the host's default packs.

    Read-only. ``heal`` is what records a decision; until it does, readers must show exactly
    the packs the hook is enforcing.
    """
    state = read_state(path)
    if state[PACKS_KEY] is None and not state["broken"]:
        state[PACKS_KEY] = sorted(default_packs())
    return state


def read_flag(path: Path, key: str) -> Optional[bool]:
    """A top-level ``key: true|false`` in the policy file, or None. Plain-text read: no PyYAML needed."""
    try:
        text = path.read_text()
    except OSError:
        return None
    m = re.search(rf"^{re.escape(key)}\s*:\s*(true|false)\s*(?:#.*)?$", text, re.MULTILINE | re.IGNORECASE)
    return None if m is None else m.group(1).lower() == "true"


def write_flag(path: Path, key: str, value: bool) -> None:
    """Set a top-level boolean in the policy file, preserving every other line. Atomic."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    rendered = f"{key}: {'true' if value else 'false'}"
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    if any(pattern.match(line) for line in lines):
        lines = [rendered if pattern.match(line) else line for line in lines]
    else:
        lines.append(rendered)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def _file_source(overlay: Optional[Path], name: str) -> str:
    """``bundled``, or ``overridden`` when the host's overlay holds a file of the same name."""
    return "overridden" if overlay and any((overlay / name).with_suffix(ext).exists() for ext in (".yaml", ".json")) else "bundled"


def overview(policy: Path, rules_overlay: Optional[Path], tooldefs_overlay: Optional[Path]) -> List[Dict[str, Any]]:
    """One row per registry entry: kind, whether it is on, its files, and whether to suggest it."""
    import shutil

    state = effective_state(policy)
    rows = []
    for e in registry().values():
        files = [
            {"section": section, "file": f, "source": _file_source(overlay, f)}
            for section, overlay in (("rules", rules_overlay), ("tooldefs", tooldefs_overlay))
            for f in e.files(section)
        ]
        found = [b for b in e.binaries if shutil.which(b)]
        enabled = entry_active(e, state)
        rows.append({
            "name": e.name, "kind": e.kind, "description": e.description,
            "kernel": e.kernel, "enabled": enabled, "files": files,
            "binaries_found": found,
            "suggest_enable": e.kind == "pack" and not enabled and bool(found),
        })
    return rows


class MembershipError(ValueError):
    """A refused pack/group change; the message says why."""


def set_enabled(policy: Path, kind: str, name: str, enable: bool) -> None:
    """Enable or disable one pack/group in ``policy``. Raises :class:`MembershipError` when refused.

    The caller owns the human-only gate. Writing is refused over an unparseable policy file.
    """
    entry = registry().get(name)
    if entry is None or entry.kind != kind:
        known = ", ".join(sorted(n for n, x in registry().items() if x.kind == kind))
        other = f" (that is a {entry.kind})" if entry else ""
        raise MembershipError(f"unknown {kind} {name!r}{other}; known: {known}")
    if entry.kernel and not enable:
        raise MembershipError(f"{name} is part of the always-on kernel and cannot be disabled")
    state = read_state(policy)
    if state["broken"]:
        raise MembershipError(f"refusing to write: {state['broken']}; fix the file by hand first")
    if kind == "pack":
        current = set(effective_state(policy)[PACKS_KEY] or [])
        write_state(policy, packs=sorted(current | {name} if enable else current - {name}))
    else:
        current = set(state[GROUPS_KEY])
        write_state(policy, disabled_groups=sorted(current - {name} if enable else current | {name}))
