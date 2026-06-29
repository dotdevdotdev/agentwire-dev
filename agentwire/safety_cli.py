"""CLI for damage control — ``agentwire safety ...``.

Thin command wrappers; the actual logic lives in ``safety_commands`` (the same
module the PreToolUse hooks invoke), so the CLI surface and the hook surface
share one implementation.
"""

from __future__ import annotations

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
    """CLI command: agentwire safety notify-unattended-block (hook-invoked)"""
    return safety_commands.safety_notify_unattended_block_cmd(
        getattr(args, "reason", "") or "",
        getattr(args, "rule_id", "") or "",
        getattr(args, "command", "") or "",
    )


def cmd_safety_logs(args) -> int:
    """CLI command: agentwire safety logs"""
    tail = getattr(args, 'tail', None)
    session = getattr(args, 'session', None)
    today = getattr(args, 'today', False)
    pattern = getattr(args, 'pattern', None)
    return safety_commands.safety_logs_cmd(tail, session, today, pattern)


def cmd_safety_install(args) -> int:
    """CLI command: agentwire safety install"""
    return safety_commands.safety_install_cmd(assume_yes=getattr(args, "yes", False))


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
        help="Email the owner that an unattended action was blocked (hook-internal)",
    )
    safety_notify.add_argument("--reason", default="", help="Why the command was blocked")
    safety_notify.add_argument("--rule-id", dest="rule_id", default="", help="Matched rule id")
    safety_notify.add_argument("--command", default="", help="The blocked command")
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

    # safety install
    safety_install = safety_subparsers.add_parser(
        "install", help="Install/heal damage control hooks, rules, and matchers"
    )
    safety_install.add_argument(
        "-y", "--yes", action="store_true",
        help="Non-interactive, drift-aware heal (install missing + update stale "
             "owned hooks; never clobbers existing rules)",
    )
    safety_install.set_defaults(func=cmd_safety_install)

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
