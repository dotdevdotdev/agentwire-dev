"""CLI for claude-in-chrome tab bookkeeping — ``agentwire tabs track/untrack/list``.

Pure bookkeeping: agentwire cannot call `tabs_close_mcp` itself (that MCP
server runs inside the calling agent's own client), so this just tracks which
tab ids a session opened, so a crashed session's orphaned tabs can still be
surfaced and closed by whichever agent tears it down (#717).
"""

from . import chrome_tabs
from .core import _output_json, _output_result


def cmd_tabs_track(args) -> int:
    json_mode = getattr(args, 'json', False)
    session = getattr(args, 'session', None)
    tab_id = getattr(args, 'tab_id', None)
    if not session or not tab_id:
        return _output_result(False, json_mode, "Usage: agentwire tabs track --session <name> --tab-id <id> [--url <url>]")
    entry = chrome_tabs.track(session, tab_id, getattr(args, 'url', None))
    if json_mode:
        _output_json({"success": True, "session": session, **entry})
    else:
        print(f"Tracked tab {tab_id} for session '{session}'.")
    return 0


def cmd_tabs_untrack(args) -> int:
    json_mode = getattr(args, 'json', False)
    session = getattr(args, 'session', None)
    tab_id = getattr(args, 'tab_id', None)
    if not session or not tab_id:
        return _output_result(False, json_mode, "Usage: agentwire tabs untrack --session <name> --tab-id <id>")
    removed = chrome_tabs.untrack(session, tab_id)
    if json_mode:
        _output_json({"success": True, "session": session, "tab_id": tab_id, "removed": removed})
    else:
        print(f"Untracked tab {tab_id} for session '{session}'." if removed
              else f"Tab {tab_id} was not tracked for '{session}'.")
    return 0


def cmd_tabs_list(args) -> int:
    json_mode = getattr(args, 'json', False)
    session = getattr(args, 'session', None)
    data = {session: chrome_tabs.tabs_for(session)} if session else chrome_tabs.all_tabs()

    if json_mode:
        _output_json({"success": True, "tabs": data})
        return 0

    total = sum(len(v) for v in data.values())
    if not total:
        print("No tracked claude-in-chrome tabs.")
        return 0
    for sess, tabs in data.items():
        for t in tabs:
            url_bit = f" ({t['url']})" if t.get('url') else ""
            print(f"  {sess}: tab {t.get('tab_id')}{url_bit}")
    return 0


def register_tabs_parser(subparsers) -> None:
    """Register ``agentwire tabs track/untrack/list``."""
    tabs_parser = subparsers.add_parser(
        "tabs",
        help="Track claude-in-chrome tabs opened by a session (teardown bookkeeping, #717)",
        description="Record/drop/list claude-in-chrome tab ids a session opened, so worktree "
                    "teardown can surface any a crashed session never closed.",
    )
    tabs_sub = tabs_parser.add_subparsers(dest="tabs_command")
    tabs_parser.set_defaults(func=cmd_tabs_list)

    track_p = tabs_sub.add_parser("track", help="Record a tab id opened by a session")
    track_p.add_argument("--session", required=True, help="Owning session name")
    track_p.add_argument("--tab-id", dest="tab_id", required=True, help="claude-in-chrome tab id")
    track_p.add_argument("--url", help="Tab URL (informational)")
    track_p.add_argument("--json", action="store_true", help="Output as JSON")
    track_p.set_defaults(func=cmd_tabs_track)

    untrack_p = tabs_sub.add_parser("untrack", help="Drop a previously tracked tab id")
    untrack_p.add_argument("--session", required=True, help="Owning session name")
    untrack_p.add_argument("--tab-id", dest="tab_id", required=True, help="claude-in-chrome tab id")
    untrack_p.add_argument("--json", action="store_true", help="Output as JSON")
    untrack_p.set_defaults(func=cmd_tabs_untrack)

    list_p = tabs_sub.add_parser("list", help="List tracked tabs (all sessions, or one)")
    list_p.add_argument("--session", help="Limit to one session")
    list_p.add_argument("--json", action="store_true", help="Output as JSON")
    list_p.set_defaults(func=cmd_tabs_list)
