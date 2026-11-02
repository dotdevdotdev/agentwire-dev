"""``damage-control-dev``: install, inspect and operate the Claude Code hooks (#1209 M5).

    damage-control-dev init [--scope user|project|managed] [--dry-run] [--refresh] [--fail-open]
    damage-control-dev status [--json]      # read-only drift report
    damage-control-dev doctor [--fix] [--json]
    damage-control-dev uninstall [--purge]
    damage-control-dev check "<command>"    # what would the hook decide?
    damage-control-dev rules                # the enforced rule ids
    damage-control-dev packs|groups list|enable|disable [name]

Exit status: 0 = fine, 1 = a refusal or a finding marked ``[!!]``, 2 = bad usage.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from damage_control import __version__, engine, install, packs
from damage_control import claude_code as cc
from damage_control import harness_config as hc
from damage_control.groups import registry
from damage_control.profile import ProfileError, load_profile


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--harness", default=cc.HARNESS, choices=[cc.HARNESS], help="the agent harness (Claude Code only)")
    parser.add_argument("--scope", default="user", choices=cc.SCOPES, help="which settings file to manage (default: user)")
    parser.add_argument("--settings", metavar="PATH", help="manage this settings file instead of the scope's")
    parser.add_argument("--profile", metavar="PATH", help="a host profile manifest the hooks load (for host installers)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="damage-control-dev", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"damage-control-dev {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="register the hooks, prove they run, record the install")
    _common(p)
    p.add_argument("--dry-run", action="store_true", help="show the diff and write nothing")
    p.add_argument("--refresh", action="store_true", help="print and acknowledge the ruleset diff (loosening is marked !!)")
    p.add_argument("--force", action="store_true", help="override the downgrade refusal")
    p.add_argument("--fail-open", action="store_true", help="register without onFailure=block (recorded in the policy)")
    p.add_argument("--allow-dev-source", action="store_true", help="allow an editable/vcs/uvx install to write machine-global state")
    p.add_argument("--apply", action="store_true", help="managed scope only: actually write (otherwise a dry run)")

    p = sub.add_parser("status", help="read-only drift report")
    _common(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("doctor", help="status plus self-test, harness version and repair")
    _common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--fix", action="store_true", help="re-run init to repair the registration")
    p.add_argument("--allow-dev-source", action="store_true")

    p = sub.add_parser("uninstall", help="remove exactly our entries")
    _common(p)
    p.add_argument("--purge", action="store_true", help="also delete the policy, overlays and install state")

    p = sub.add_parser("check", help="what would the hooks decide for a shell command?")
    p.add_argument("--profile", metavar="PATH")
    p.add_argument("text")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("rules", help="list the enforced rule ids")
    p.add_argument("--profile", metavar="PATH")
    p.add_argument("--json", action="store_true")

    for noun in ("packs", "groups"):
        p = sub.add_parser(noun, help=f"list, enable or disable {noun} (enable/disable: human terminal only)")
        p.add_argument("--profile", metavar="PATH")
        p.add_argument("action", choices=["list", "enable", "disable"])
        p.add_argument("name", nargs="?")
    return parser


def _load_profile(path: Optional[str]) -> None:
    if path:
        engine.install_profile(load_profile(Path(path)))


def _print_findings(findings: List[install.Finding], as_json: bool) -> int:
    code = install.exit_code(findings)
    if as_json:
        print(json.dumps({"ok": code == 0, "findings": [f._asdict() for f in findings]}, indent=2))
    else:
        for f in findings:
            print(f.line())
    return code


def _cmd_check(args: argparse.Namespace) -> int:
    result = engine.check_command(args.text, engine.load_effective_config(), cwd=str(Path.cwd()))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"{result.get('decision')}: {result.get('reason') or 'no rule matched'}"
              + (f" [{result['id']}]" if result.get("id") else ""))
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    config = engine.load_effective_config()
    rows = [{"id": r["id"], "tier": "ask" if r.get("ask") else "block", "source": r.get("source")}
            for r in config["bashToolPatterns"]]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['tier']:5} {r['id']}  ({r['source']})")
    return 0


def _cmd_pack_group(args: argparse.Namespace) -> int:
    kind = "pack" if args.command == "packs" else "group"
    policy = engine.policy_file_path()
    entries = {n: e for n, e in registry().items() if e.kind == kind}
    state = packs.effective_state(policy)
    if args.action == "list":
        for name, entry in sorted(entries.items()):
            print(f"{'on ' if packs.entry_active(entry, state) else 'off'} {name:16} {entry.description}")
        return 0
    if not args.name or args.name not in entries:
        print(f"unknown {kind} {args.name!r}; choose from: {', '.join(sorted(entries))}", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print(f"REFUSED: changing damage-control {kind}s is a policy change; run it from your own terminal.", file=sys.stderr)
        return 1
    entry = entries[args.name]
    if entry.kernel and args.action == "disable":
        print(f"REFUSED: {args.name} is a kernel group and cannot be disabled.", file=sys.stderr)
        return 1
    enable = args.action == "enable"
    if kind == "pack":
        current = set(state[packs.PACKS_KEY] or [])
        packs.write_state(policy, packs=sorted(current | {args.name} if enable else current - {args.name}))
    else:
        current = set(state[packs.GROUPS_KEY])
        packs.write_state(policy, disabled_groups=sorted(current - {args.name} if enable else current | {args.name}))
    print(f"{args.name}: {'enabled' if enable else 'disabled'}")
    return 0


def run(args: argparse.Namespace) -> int:
    _load_profile(getattr(args, "profile", None))
    profile = getattr(args, "profile", None)
    command = args.command
    if command == "init":
        return install.init(scope=args.scope, settings=args.settings, dry_run=args.dry_run, refresh=args.refresh,
                            force=args.force, fail_open=args.fail_open, allow_dev_source=args.allow_dev_source,
                            apply=args.apply, profile=profile)
    if command == "status":
        return _print_findings(install.inspect(args.scope, args.settings, profile_hint=profile), args.json)
    if command == "doctor":
        findings = install.inspect(args.scope, args.settings, deep=True, profile_hint=profile)
        code = _print_findings(findings, args.json)
        if args.fix and code:
            print("--fix: re-running init to repair the registration", file=sys.stderr)
            return install.init(scope=args.scope, settings=args.settings, allow_dev_source=args.allow_dev_source,
                                profile=profile, force=False)
        return code
    if command == "uninstall":
        return install.uninstall(args.scope, args.settings, purge=args.purge)
    if command == "check":
        return _cmd_check(args)
    if command == "rules":
        return _cmd_rules(args)
    return _cmd_pack_group(args)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (install.InstallError, hc.ConfigError, ProfileError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
