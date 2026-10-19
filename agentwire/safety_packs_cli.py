"""``agentwire safety packs|groups`` — rule groups and opt-in vendor packs (#1141).

Reads are open to anyone (CLI, doctor, MCP). Mutations are human-only: the
host-context gate here (a real tty, same as ``tasks promote``) plus the
``PROTECTED_COMMAND_PATTERNS`` entry that stops an agent's Bash tool before it
gets this far. State lives in the protected ``~/.agentwire/damagecontrol.yml``.
"""

from __future__ import annotations

import json
import shutil
import sys
from typing import Any, Dict, List

from . import safety_commands
from .safety import packs as pack_state
from .safety.groups import REGISTRY, Entry


def _file_status(section: str, name: str) -> str:
    live = (safety_commands.RULES_DIR if section == "rules" else safety_commands.TOOLDEFS_DIR) / name
    if not live.exists():
        return "absent"
    digest = safety_commands._sha256(live)
    shipped = safety_commands.load_rule_baselines().get(section, {}).get(name, [])
    return "pristine" if digest in shipped else "edited"


def overview() -> List[Dict[str, Any]]:
    """One row per registry entry: kind, whether it is on, files, suggestion."""
    state = pack_state.effective_state(
        safety_commands.DAMAGECONTROL_FILE, safety_commands.RULES_DIR, safety_commands.TOOLDEFS_DIR
    )
    rows = []
    for e in REGISTRY.values():
        files = [
            {"section": section, "file": f, "installed": _file_status(section, f)}
            for section in ("rules", "tooldefs") for f in getattr(e, section)
        ]
        present = [b for b in e.binaries if shutil.which(b)]
        enabled = pack_state.entry_active(e, state)
        rows.append({
            "name": e.name, "kind": e.kind, "description": e.description,
            "kernel": e.kernel, "enabled": enabled, "files": files,
            "binaries_found": present,
            "suggest_enable": e.kind == "pack" and not enabled and bool(present),
        })
    return rows


def _gate(action: str) -> bool:
    from .tasks_cli import _host_context_ok

    if _host_context_ok():
        return True
    print(f"agentwire safety {action} changes damage-control policy — human-only. "
          f"Run it from your own terminal, not through an agent.", file=sys.stderr)
    return False


def _broken_refusal(state) -> bool:
    if state.get("broken"):
        print(f"Refusing to write: {state['broken']}. Fix the file by hand first.", file=sys.stderr)
        return True
    return False


def _heal_after_change() -> int:
    summary = safety_commands.heal_damage_control(quiet=False)
    if summary.get("refused"):
        print("State recorded, but files were NOT synced: this checkout is not the "
              "installed tool. Run `agentwire safety install --yes` from it.", file=sys.stderr)
        return 1
    return 0


def _entry(name: str, kind: str) -> Entry | None:
    e = REGISTRY.get(name)
    if e is None or e.kind != kind:
        other = "groups" if kind == "pack" else "packs"
        hint = f" (that is a {e.kind}; use `agentwire safety {other}`)" if e else ""
        print(f"Unknown {kind} '{name}'{hint}. Known: "
              f"{', '.join(sorted(n for n, x in REGISTRY.items() if x.kind == kind))}",
              file=sys.stderr)
        return None
    return e


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
                print(f"         {f['section']}/{f['file']}: {f['installed']}")
    return 0


def cmd_packs_list(args) -> int:
    return _list("pack", getattr(args, "json", False), False)


def cmd_packs_status(args) -> int:
    return _list("pack", getattr(args, "json", False), True)


def cmd_groups_list(args) -> int:
    return _list("group", getattr(args, "json", False), False)


def cmd_groups_status(args) -> int:
    return _list("group", getattr(args, "json", False), True)


def _set_pack(name: str, on: bool) -> int:
    e = _entry(name, "pack")
    if e is None or not _gate(f"packs {'enable' if on else 'disable'}"):
        return 1
    state = safety_commands.migrate_pack_state()
    if _broken_refusal(state):
        return 1
    current = set(state[pack_state.PACKS_KEY] or [])
    current = current | {name} if on else current - {name}
    pack_state.write_state(safety_commands.DAMAGECONTROL_FILE, packs=sorted(current))
    print(f"Pack '{name}' {'enabled' if on else 'disabled'}.")
    return _heal_after_change()


def cmd_packs_enable(args) -> int:
    return _set_pack(args.name, True)


def cmd_packs_disable(args) -> int:
    return _set_pack(args.name, False)


def _set_group(name: str, on: bool) -> int:
    e = _entry(name, "group")
    if e is None or not _gate(f"groups {'enable' if on else 'disable'}"):
        return 1
    if e.kernel and not on:
        print(f"Group '{name}' is part of the always-on kernel and cannot be disabled.",
              file=sys.stderr)
        return 1
    state = pack_state.read_state(safety_commands.DAMAGECONTROL_FILE)
    if _broken_refusal(state):
        return 1
    current = set(state[pack_state.GROUPS_KEY])
    current = current - {name} if on else current | {name}
    pack_state.write_state(safety_commands.DAMAGECONTROL_FILE, disabled_groups=sorted(current))
    print(f"Group '{name}' {'enabled' if on else 'disabled'}.")
    return _heal_after_change()


def cmd_groups_enable(args) -> int:
    return _set_group(args.name, True)


def cmd_groups_disable(args) -> int:
    return _set_group(args.name, False)


def register_packs_parsers(safety_subparsers) -> None:
    """Attach ``safety packs`` and ``safety groups`` to the safety subparsers."""
    for kind, plural, list_fn, status_fn, en_fn, dis_fn in (
        ("pack", "packs", cmd_packs_list, cmd_packs_status, cmd_packs_enable, cmd_packs_disable),
        ("group", "groups", cmd_groups_list, cmd_groups_status, cmd_groups_enable, cmd_groups_disable),
    ):
        parser = safety_subparsers.add_parser(
            plural,
            help=("Opt-in vendor rule packs (aws, databases, ...)" if kind == "pack"
                  else "Default rule groups (opt-out; the kernel is always on)"),
        )
        sub = parser.add_subparsers(dest=f"{plural}_command")
        parser.set_defaults(func=list_fn)
        for verb, fn, helptext in (
            ("list", list_fn, f"List {plural} and whether each is on"),
            ("status", status_fn, f"List {plural} with per-file install state"),
        ):
            p = sub.add_parser(verb, help=helptext)
            p.add_argument("--json", action="store_true", help="Machine-readable output")
            p.set_defaults(func=fn)
        for verb, fn in (("enable", en_fn), ("disable", dis_fn)):
            p = sub.add_parser(verb, help=f"{verb.capitalize()} a {kind} (human-only)")
            p.add_argument("name", help=f"{kind.capitalize()} name")
            p.set_defaults(func=fn)
