"""CLI for damage control — ``agentwire safety ...``.

Thin command wrappers; the actual logic lives in ``safety_commands`` (the same
module the PreToolUse hooks invoke), so the CLI surface and the hook surface
share one implementation.
"""

from __future__ import annotations

import sys

from . import safety_commands


def cmd_safety_check(args) -> int:
    """CLI command: agentwire safety check"""
    command = args.command
    verbose = getattr(args, 'verbose', False)
    return safety_commands.safety_check_cmd(command, verbose)


def cmd_safety_status(args) -> int:
    """CLI command: agentwire safety status"""
    return safety_commands.safety_status_cmd()


def cmd_safety_notify_unattended_block(args) -> int:
    """CLI command: agentwire safety notify-unattended-block (hook-invoked; event JSON on stdin)"""
    return safety_commands.safety_notify_unattended_block_cmd(sys.stdin.read())


def cmd_safety_logs(args) -> int:
    """CLI command: agentwire safety logs"""
    tail = getattr(args, 'tail', None)
    session = getattr(args, 'session', None)
    today = getattr(args, 'today', False)
    pattern = getattr(args, 'pattern', None)
    return safety_commands.safety_logs_cmd(tail, session, today, pattern)


def cmd_safety_report(args) -> int:
    """CLI command: agentwire safety report"""
    return safety_commands.safety_report_cmd(args.days, args.json)


def cmd_safety_suggest_delete_recursive(args) -> int:
    """CLI command: agentwire safety suggest-delete-recursive"""
    return safety_commands.safety_suggest_delete_recursive_cmd(args.days, args.min, args.json)


def cmd_safety_install(args) -> int:
    """CLI command: agentwire safety install"""
    return safety_commands.safety_install_cmd(
        assume_yes=getattr(args, "yes", False),
        force=getattr(args, "force", False),
        allow_foreign=getattr(args, "allow_foreign_source", False),
    )


def cmd_safety_migrate(args) -> int:
    """CLI command: agentwire safety migrate"""
    from .safety_migrate import cmd_safety_migrate as run

    return run(args)


def cmd_safety_tooldefs_list(args) -> int:
    """CLI command: agentwire safety tooldefs list"""
    return safety_commands.safety_tooldefs_list_cmd()


def cmd_safety_tooldefs_show(args) -> int:
    """CLI command: agentwire safety tooldefs show <tool>"""
    return safety_commands.safety_tooldefs_show_cmd(args.tool)


def register_safety_parser(subparsers) -> None:
    # === safety command group ===
    safety_parser = subparsers.add_parser(
        "safety", help="Damage control security commands"
    )
    safety_subparsers = safety_parser.add_subparsers(dest="safety_command")

    # safety check <command>
    safety_check = safety_subparsers.add_parser(
        "check", help="Test if a command would be blocked/allowed"
    )
    safety_check.add_argument("command", help="Command to test")
    safety_check.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )
    safety_check.set_defaults(func=cmd_safety_check)

    # safety status
    safety_status = safety_subparsers.add_parser(
        "status", help="Show safety status and pattern counts"
    )
    safety_status.set_defaults(func=cmd_safety_status)

    # safety notify-unattended-block (hook-invoked, not for humans)
    safety_notify = safety_subparsers.add_parser(
        "notify-unattended-block",
        help="Email the owner that an unattended action was blocked (hook-internal; event JSON on stdin)",
    )
    safety_notify.set_defaults(func=cmd_safety_notify_unattended_block)

    # safety logs
    safety_logs = safety_subparsers.add_parser(
        "logs", help="Query audit logs"
    )
    safety_logs.add_argument(
        "--tail", "-n", type=int, help="Show last N entries"
    )
    safety_logs.add_argument(
        "--session", "-s", help="Filter by session ID"
    )
    safety_logs.add_argument(
        "--today", action="store_true", help="Show only today's logs"
    )
    safety_logs.add_argument(
        "--pattern", "-p", help="Filter by pattern (regex or substring)"
    )
    safety_logs.set_defaults(func=cmd_safety_logs)

    # safety report — block summary by tier/rule/session (#940)
    safety_report = safety_subparsers.add_parser(
        "report",
        help="Summarize damage-control blocks by tier/rule/session over a window",
    )
    safety_report.add_argument(
        "--days", type=int, default=14, help="Window in days (default 14)"
    )
    safety_report.add_argument(
        "--json", action="store_true", help="Machine-readable output"
    )
    safety_report.set_defaults(func=cmd_safety_report)

    # safety suggest-delete-recursive — read-only; prints the policy a human pastes (#1152)
    safety_suggest = safety_subparsers.add_parser(
        "suggest-delete-recursive",
        help="List recurring blocked `rm -rf` of regenerable dirs (dist, node_modules, ...) and print the delete-recursive policy to paste (never writes policy)",
    )
    safety_suggest.add_argument(
        "--days", type=int, default=14, help="Window in days (default 14)"
    )
    safety_suggest.add_argument(
        "--min", type=int, default=2, help="Only targets blocked at least this many times (default 2)"
    )
    safety_suggest.add_argument(
        "--json", action="store_true", help="Machine-readable output"
    )
    safety_suggest.set_defaults(func=cmd_safety_suggest_delete_recursive)

    # safety install
    safety_install = safety_subparsers.add_parser(
        "install", help="Register the damage-control package hooks with AgentWire's profile"
    )
    safety_install.add_argument(
        "-y", "--yes", action="store_true",
        help="Non-interactive: register the hooks (after proving them) without asking",
    )
    safety_install.add_argument(
        "--force", action="store_true",
        help="Override the downgrade refusal (this package is older than the recorded install)",
    )
    safety_install.add_argument(
        "--allow-foreign-source", action="store_true",
        help="Let a checkout that is NOT the installed tool write these "
             "machine-global files. Installing from a task branch is what "
             "silently downgraded this machine's security hooks (#936)",
    )
    safety_install.set_defaults(func=cmd_safety_install)

    # safety migrate
    safety_migrate = safety_subparsers.add_parser(
        "migrate",
        help="Bring a pre-package damage-control install onto the package (human-only, one-shot)",
    )
    safety_migrate.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing")
    safety_migrate.add_argument(
        "--allow-foreign-source", action="store_true",
        help="Let a checkout that is NOT the installed tool write machine-global files (#936)",
    )
    safety_migrate.set_defaults(func=cmd_safety_migrate)

    # safety packs / groups (#1141)
    from .safety_packs_cli import register_packs_parsers
    register_packs_parsers(safety_subparsers)

    # safety tooldefs
    safety_tooldefs = safety_subparsers.add_parser(
        "tooldefs", help="Browse tool definitions"
    )
    tooldefs_subparsers = safety_tooldefs.add_subparsers(dest="tooldefs_command")

    tooldefs_list = tooldefs_subparsers.add_parser("list", help="List available tooldefs")
    tooldefs_list.set_defaults(func=cmd_safety_tooldefs_list)

    tooldefs_show = tooldefs_subparsers.add_parser("show", help="Show tooldef for a tool")
    tooldefs_show.add_argument("tool", help="Tool name (e.g. git, gh, docker)")
    tooldefs_show.set_defaults(func=cmd_safety_tooldefs_show)
