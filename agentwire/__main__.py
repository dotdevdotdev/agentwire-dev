"""CLI entry point for AgentWire."""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env files (project first, then global config)
load_dotenv()  # .env in current directory
load_dotenv(Path.home() / ".agentwire" / ".env")  # Global config

from . import (  # noqa: E402  # must follow load_dotenv() above
    __version__,
)
from .core import (  # noqa: E402,F401  # E402: must follow load_dotenv(); F401: re-exported moved helpers
    _UNATTENDED_ENV_KEYS,
    CONFIG_DIR,
    AgentCommand,
    _add_posture_flag,
    _build_tmux_env_flags,
    _build_tmux_env_flags_shell,
    _check_portal_health,
    _check_tmux_installed,
    _default_portal_url,
    _display_parent,
    _get_agentwire_path,
    _get_all_machines,
    _get_machine_config,
    _get_portal_url,
    _get_session_project_path,
    _git_behind_origin,
    _install_global_tmux_hooks,
    _notify_portal_sessions_changed,
    _output_json,
    _output_result,
    _parse_session_target,
    _portal_auth_headers,
    _post_desktop_notification,
    _record_session_creator,
    _resolve_posture_from_args,
    _run_remote,
    _set_session_name_env,
    _start_portal_local,
    _tmux_global_option,
    _with_unattended_env,
    build_agent_command,
    check_pip_environment,
    check_python_version,
    format_relative_time,
    generate_certs,
    get_kokoro_session_name,
    get_portal_session_name,
    get_source_dir,
    get_stt_session_name,
    get_tts_session_name,
    inject_session_env,
    load_config,
    load_session_metadata,
    parse_env_args,
    store_session_metadata,
    tmux_session_exists,
    tmux_session_has_agent,
    wait_for_shell_prompt,
)


class VersionAction(argparse.Action):
    """Custom version action that checks Python version and pip environment."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        # Print version
        print(f"agentwire {__version__}")
        print(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

        # Check version compatibility
        version_ok = check_python_version()
        env_ok = check_pip_environment()

        if version_ok and env_ok:
            print("\n✓ System is ready for AgentWire")
        else:
            print("\n⚠️  Please resolve the issues above before installing/running AgentWire")

        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser with every registered subcommand.

    Extracted from ``main()`` so the parser tree can be constructed without
    dispatching, which the CLI smoke tests rely on to enumerate subcommands
    and invoke their ``--help`` (see ``tests/unit/test_cli_smoke.py``).
    """
    parser = argparse.ArgumentParser(
        prog="agentwire",
        description="Multi-session voice web interface for AI coding agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Command Categories:
  Getting Started:
    init             Interactive setup wizard
    portal           Manage the web portal
    new              Create a new Claude Code session
    say              Speak text via TTS

  Sessions:
    list             List panes or sessions
    info             Get session information
    kill             Kill a session or pane
    spawn            Spawn a worker pane in current session
    worktree         Create a git worktree + session
    send             Send prompt to a session or pane
    output           Read session or pane output

  Voice:
    listen           Voice input recording
    tts              Manage TTS server
    stt              Manage STT server

  Diagnostics:
    doctor           Auto-diagnose and fix common issues
    network          Network diagnostics and status
    safety           Damage control security commands
    hooks            Manage agentwire hook files

  Advanced:
    council          Multi-soul council operations
    scheduler        Manage the task scheduler
    ensure           Run named task with reliable session management
    limits           Usage-limit recovery management
"""
    )
    parser.add_argument(
        "--version",
        action=VersionAction,
        help="Show version and check system compatibility",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # === quo command ===
    from agentwire.channels.quo import cmd_quo
    quo_parser = subparsers.add_parser("quo", help="Send SMS via Quo (OpenPhone)")
    quo_parser.add_argument("--body", "-b", type=str, help="Message body (or pipe via stdin)")
    quo_parser.add_argument("--to", type=str, help="Recipient phone number (+E.164 format)")
    quo_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    quo_parser.set_defaults(func=cmd_quo)

    # === email command ===
    from agentwire.channels.email import cmd_email
    email_parser = subparsers.add_parser("email", help="Send branded email notification via Resend")
    email_parser.add_argument(
        "--to", action="append", default=None,
        help="Recipient email. Repeat or pass comma-separated for multiple recipients (default: from config).",
    )
    email_parser.add_argument("--subject", "-s", type=str, help="Email subject")
    email_parser.add_argument("--body", "-b", type=str, help="Email body - markdown supported (or pipe via stdin)")
    email_parser.add_argument("--attach", "-a", type=str, action="append", help="Attach file (can use multiple times)")
    email_parser.add_argument("--plain", action="store_true", help="Send plain text only (no HTML template)")
    email_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    email_parser.set_defaults(func=cmd_email)

    # === fetch command ===
    from agentwire.fetch import cmd_fetch
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch a URL via Jina Reader — handles JS-rendered pages, returns clean markdown.",
    )
    fetch_parser.add_argument("url", help="URL to fetch")
    fetch_parser.add_argument(
        "--limit", "-l", type=int, default=8000,
        help="Max characters to return (default: 8000, 0 = no limit)",
    )
    fetch_parser.set_defaults(func=cmd_fetch)

    # === Extracted command groups (each registrar owns its own subparser) ===
    # Phase 1 of #495 appends one entry here per extracted domain.
    #   - limits: usage-limit recovery (detect dialog, park, auto-resume)
    #   - diff:   structured git diff for the mobile Review window
    #   - prompts: prompt routing (rides the limits watchdog)
    #   - msg:    polite agent-to-agent inbox (rides the watchdog)
    from . import diff_cli, limits_cli, msg_cli, pane_cli, prompts_cli, send_cli  # noqa: I001  # session_cli kept on its own line below to minimize Phase 1 #495 merge conflicts
    from . import session_cli
    from . import channels_cli, config_cli, portal_cli, tts_cli
    from . import scheduler_cli, ensure_cli, tasks_cli
    from . import doctor_cli, history_cli, machine_cli, safety_cli
    from . import handoff_cli, hooks_cli, mcp_cli, roles_cli, tunnels_cli
    from . import notify_cli, palette_cli, push_cli, repo_cli, system_cli, wiki_cli
    from . import tabs_cli, wait_cli
    from .council import cli as council_cli

    _REGISTRARS = [  # noqa: N806  # registry constant; Phase 1 of #495 appends here
        diff_cli.register_diff_parser,
        prompts_cli.register_prompts_parser,
        msg_cli.register_msg_parser,
        limits_cli.register_limits_parser,
        pane_cli.register_pane_parser,
        send_cli.register_send_parser,
        session_cli.register_session_parser,
        portal_cli.register_portal_parser,
        tts_cli.register_tts_parser,
        channels_cli.register_channels_parser,
        scheduler_cli.register_scheduler_parser,
        ensure_cli.register_ensure_parser,
        tasks_cli.register_tasks_parser,
        doctor_cli.register_doctor_parser,
        safety_cli.register_safety_parser,
        machine_cli.register_machine_parser,
        history_cli.register_history_parser,
        roles_cli.register_roles_parser,
        roles_cli.register_projects_parser,
        hooks_cli.register_hooks_parser,
        tunnels_cli.register_tunnels_parser,
        handoff_cli.register_handoff_parser,
        mcp_cli.register_mcp_parser,
        notify_cli.register_notify_parser,
        wiki_cli.register_wiki_parser,
        system_cli.register_system_parser,
        push_cli.register_push_parser,
        repo_cli.register_repo_parser,
        config_cli.register_config_parser,
        palette_cli.register_palette_parser,
        tabs_cli.register_tabs_parser,
        wait_cli.register_wait_parser,
    ]
    for _reg in _REGISTRARS:
        _reg(subparsers)

    # === council command group ===
    council_parser = subparsers.add_parser(
        "council",
        help="Multi-soul council: fan a prompt out to lens sessions, synthesize",
        description=(
            "An agentwire-council orchestrator session fans prompts out to a "
            "roster of lens souls (brain, conscience, gut, critic, historian, "
            "devils-advocate), collects their takes through a file inbox, and "
            "synthesizes. See docs/wiki/council.md."
        ),
    )
    council_subparsers = council_parser.add_subparsers(dest="council_command")

    # Targeting is shared: --name picks the sitting; absent, the cwd-repo-slug
    # if it matches a live sitting, else the sole live sitting, else error.
    _name_help = "Sitting name (default: cwd-repo-slug / sole live sitting)"

    # council start
    c_start = council_subparsers.add_parser(
        "start", help="Start a sitting: orchestrator + all soul sessions"
    )
    c_start.add_argument(
        "--name", help="Sitting name (default: cwd-repo-slug)"
    )
    c_start.add_argument(
        "--roster", help="Comma-separated lens names (default: full bundled roster)"
    )
    c_start.add_argument("--posture", help="Permission mode for council sessions (default: bypass)")
    c_start.add_argument("--model", help="Model override for all council sessions")
    c_start.add_argument(
        "--force", action="store_true", help="Tear down a live sitting first"
    )
    c_start.add_argument("--json", action="store_true", help="Output JSON")
    c_start.set_defaults(func=council_cli.cmd_council_start)

    # council stop
    c_stop = council_subparsers.add_parser(
        "stop", help="Kill the sitting's sessions (prompt history kept)"
    )
    c_stop.add_argument("--name", help=_name_help)
    c_stop.add_argument(
        "--minutes", dest="minutes", action="store_true", default=None,
        help="Render the minutes artifact (default: render when any prompt exists)",
    )
    c_stop.add_argument(
        "--no-minutes", dest="minutes", action="store_false",
        help="Skip the minutes artifact",
    )
    c_stop.add_argument(
        "--synthesis", help="Synthesis for the minutes: text, or a path to a file"
    )
    c_stop.add_argument("--json", action="store_true", help="Output JSON")
    c_stop.set_defaults(func=council_cli.cmd_council_stop)

    # council status
    c_status = council_subparsers.add_parser(
        "status", help="Sitting state, session liveness, open prompts"
    )
    c_status.add_argument("--name", help=_name_help)
    c_status.add_argument("--json", action="store_true", help="Output JSON")
    c_status.set_defaults(func=council_cli.cmd_council_status)

    # council list
    c_list = council_subparsers.add_parser(
        "list", help="Every known sitting: name, cwd, age, live sessions, prompts"
    )
    c_list.add_argument("--json", action="store_true", help="Output JSON")
    c_list.set_defaults(func=council_cli.cmd_council_list)

    # council ask
    c_ask = council_subparsers.add_parser(
        "ask", help="Fan a prompt out to every soul in the sitting"
    )
    c_ask.add_argument("prompt", nargs="?", help="Prompt text (or --file / stdin)")
    c_ask.add_argument("--name", help=_name_help)
    c_ask.add_argument("--file", help="Read prompt text from a file")
    c_ask.add_argument("--json", action="store_true", help="Output JSON")
    c_ask.set_defaults(func=council_cli.cmd_council_ask)

    # council collect
    c_collect = council_subparsers.add_parser(
        "collect", help="Wait for every soul's take/ack/pass (or timeout)"
    )
    c_collect.add_argument("--name", help=_name_help)
    c_collect.add_argument("--prompt", type=int, help="Prompt id (default: latest)")
    c_collect.add_argument(
        "--timeout", type=float, default=120, help="Soft timeout in seconds (default: 120)"
    )
    c_collect.add_argument(
        "--no-wait", action="store_true", help="Snapshot once, don't block"
    )
    c_collect.add_argument("--json", action="store_true", help="Output JSON")
    c_collect.set_defaults(func=council_cli.cmd_council_collect)

    # council reply (run by souls)
    c_reply = council_subparsers.add_parser(
        "reply", help="File a soul's reply: --take / --ack / --pass"
    )
    c_reply.add_argument("--name", help=_name_help)
    c_reply.add_argument("--prompt", type=int, help="Prompt id (default: latest)")
    c_reply.add_argument(
        "--take", action="store_true", help="Substantive take (text required)"
    )
    c_reply.add_argument(
        "--ack", action="store_true", help="Researching — follow-up coming"
    )
    c_reply.add_argument(
        "--pass", action="store_true", help="Nothing to add through this lens"
    )
    c_reply.add_argument("--soul", help="Lens name (default: inferred from session)")
    c_reply.add_argument("--text", help="Reply text")
    c_reply.add_argument("--file", help="Read reply text from a file")
    c_reply.add_argument("--json", action="store_true", help="Output JSON")
    c_reply.set_defaults(func=council_cli.cmd_council_reply)

    # council minutes
    c_minutes = council_subparsers.add_parser(
        "minutes",
        help="Render a sitting's minutes artifact (question + takes + synthesis)",
        description=(
            "Deterministically renders the sitting's persisted prompt history "
            "(question + attributed verbatim take/ack/pass replies) plus an "
            "optional orchestrator-supplied synthesis into a self-contained "
            "HTML artifact at ~/.agentwire/artifacts/council-<name>-minutes/, "
            "and opens it as a portal artifact window when the portal is up. "
            "Works for live and dismissed sittings (prompt history is kept "
            "on stop)."
        ),
    )
    c_minutes.add_argument("--name", help=_name_help)
    c_minutes.add_argument(
        "--prompt", help="Prompt id to render, or 'all' (default: all)"
    )
    c_minutes.add_argument(
        "--synthesis", help="Synthesis: text, or a path to a file containing it"
    )
    c_minutes.add_argument("--json", action="store_true", help="Output JSON")
    c_minutes.set_defaults(func=council_cli.cmd_council_minutes)

    return parser


def _find_subparser(parser: argparse.ArgumentParser, *names: str):
    """Walk the subparser tree by command name(s); return the parser or None."""
    current = parser
    for name in names:
        sub = None
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                sub = action.choices.get(name)
                break
        if sub is None:
            return None
        current = sub
    return current


# Command groups whose bare invocation (no subcommand) prints group help.
_GROUP_COMMANDS = [
    "portal", "tts", "stt", "tunnels", "machine", "history", "handoff",
    "wiki", "hooks", "projects", "safety", "network", "listen",
    "roles", "task", "lock", "scheduler", "council", "limits",
    "config",
]


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    if (
        args.command in _GROUP_COMMANDS
        and getattr(args, f"{args.command}_command", None) is None
    ):
        _find_subparser(parser, args.command).print_help()
        return 0

    if (
        args.command == "safety"
        and getattr(args, "safety_command", None) == "tooldefs"
        and getattr(args, "tooldefs_command", None) is None
    ):
        _find_subparser(parser, "safety", "tooldefs").print_help()
        return 0

    if hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
