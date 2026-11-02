"""``agentwire safety packs|groups``: rule groups and opt-in vendor packs (#1141).

A thin wrapper over ``damage_control.packs`` (#1209 M6): the package owns the registry, the state
file format and the write; this adds only AgentWire's human-only gate (a real tty, same as
``tasks promote``, plus the ``PROTECTED_COMMAND_PATTERNS`` entry that stops an agent's Bash tool
before it gets this far). Reads are open to anyone (CLI, doctor, MCP).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

from damage_control import packs
from damage_control.engine import overlay_dirs, policy_file_path


def overview() -> List[Dict[str, Any]]:
    """One row per registry entry: kind, whether it is on, files, suggestion."""
    return packs.overview(policy_file_path(), *overlay_dirs())


def _gate(action: str) -> bool:
    from .tasks_cli import _host_context_ok

    if _host_context_ok():
        return True
    print(f"agentwire safety {action} changes damage-control policy — human-only. "
          f"Run it from your own terminal, not through an agent.", file=sys.stderr)
    return False


def _list(kind: str, as_json: bool, verbose: bool) -> int:
    rows = [r for r in overview() if r["kind"] == kind]
    if as_json:
        print(json.dumps({"success": True, "entries": rows}, indent=2))
        return 0
    for r in rows:
        flag = "on " if r["enabled"] else "off"
        tag = " [kernel]" if r["kernel"] else ""
        hint = "  <- vendor CLI found; `agentwire safety packs enable " + r["name"] + "`" if r["suggest_enable"] else ""
        print(f"  [{flag}] {r['name']}{tag} — {r['description']}{hint}")
        if verbose:
            for f in r["files"]:
                print(f"         {f['section']}/{f['file']}: {f['source']}")
    return 0


def _set(kind: str, name: str, enable: bool) -> int:
    plural = "packs" if kind == "pack" else "groups"
    if not _gate(f"{plural} {'enable' if enable else 'disable'}"):
        return 1
    try:
        packs.set_enabled(policy_file_path(), kind, name, enable)
    except packs.MembershipError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    print(f"{kind.capitalize()} '{name}' {'enabled' if enable else 'disabled'}. Takes effect on the next tool call.")
    return 0


def cmd_packs_enable(args) -> int:
    return _set("pack", args.name, True)


def cmd_packs_disable(args) -> int:
    return _set("pack", args.name, False)


def cmd_groups_enable(args) -> int:
    return _set("group", args.name, True)


def cmd_groups_disable(args) -> int:
    return _set("group", args.name, False)


def register_packs_parsers(safety_subparsers) -> None:
    """Attach ``safety packs`` and ``safety groups`` to the safety subparsers."""
    for kind, plural, enable_fn, disable_fn in (
        ("pack", "packs", cmd_packs_enable, cmd_packs_disable),
        ("group", "groups", cmd_groups_enable, cmd_groups_disable),
    ):
        parser = safety_subparsers.add_parser(
            plural,
            help=("Opt-in vendor rule packs (aws, databases, ...)" if kind == "pack"
                  else "Default rule groups (opt-out; the kernel is always on)"),
        )
        sub = parser.add_subparsers(dest=f"{plural}_command")
        parser.set_defaults(func=lambda args, k=kind: _list(k, False, False))
        for verb, verbose, helptext in (
            ("list", False, f"List {plural} and whether each is on"),
            ("status", True, f"List {plural} with per-file install state"),
        ):
            p = sub.add_parser(verb, help=helptext)
            p.add_argument("--json", action="store_true", help="Machine-readable output")
            p.set_defaults(func=lambda args, k=kind, v=verbose: _list(k, getattr(args, "json", False), v))
        for verb, fn in (("enable", enable_fn), ("disable", disable_fn)):
            p = sub.add_parser(verb, help=f"{verb.capitalize()} a {kind} (human-only)")
            p.add_argument("name", help=f"{kind.capitalize()} name")
            p.set_defaults(func=fn)
