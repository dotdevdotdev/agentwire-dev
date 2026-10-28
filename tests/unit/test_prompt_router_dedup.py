"""#1049 — two permission prompts of the same shape must not share a hash.

kind + question + option labels is identical for every "Do you want to
proceed?" confirm, so the dedup marker treated a second, different command as
the already-delivered first one and never re-notified. The hash now includes
the dialog body (the command/file being approved), whitespace-free so a
redraw at another pane width does not change it.
"""

from datetime import timedelta

from agentwire import prompt_router

RULE = "─" * 80


def _dialog(command: str, reason: str = "Unverifiable command — confirm before running",
            above: str = "") -> str:
    return (
        f"{above}\n"
        f"{RULE}\n"
        " Bash command\n\n"
        f"   {command}\n"
        f"   {reason}\n\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No\n\n"
        " Esc to cancel · Tab to amend · ctrl+e to explain\n"
    )


CMD_A = 'psql -c "$(cat /Users/dotdev/projects/agentwire-dev/queries/long-report-query-alpha.sql)"'
CMD_B = 'psql -c "$(cat /Users/dotdev/projects/agentwire-dev/queries/long-report-query-bravo.sql)"'


def test_a_redraw_of_a_delivered_prompt_does_not_renotify(monkeypatch):
    """The #905 pin: same prompt, delivered marker -> sweep stays silent.

    Different command on the next sweep -> routed again (the #1049 case).
    """
    calls = []
    screens = iter([_dialog(CMD_A, above="✻ Baked for 12s"),
                    _dialog(CMD_A, above="✻ Baked for 13s"),
                    _dialog(CMD_B)])
    current = {}
    markers = {}

    monkeypatch.setattr(prompt_router, "_capture", lambda target: current["screen"])
    monkeypatch.setattr(prompt_router, "read_marker", lambda s, p: markers.get((s, p)))
    monkeypatch.setattr(prompt_router, "_marker_age", lambda m, key="detected_at": timedelta(seconds=5))

    def fake_route(session, pane, info, source="sweep", project_path=None):
        calls.append(info.content_hash())
        markers[(session, pane)] = {
            "hash": info.content_hash(), "source": source,
            "notified_at": prompt_router._now().isoformat(),
        }
        return "parent"

    monkeypatch.setattr(prompt_router, "route_prompt", fake_route)

    pane = prompt_router.PaneRef(session="s", pane=0, command="claude", path="")
    for screen in screens:
        current["screen"] = screen
        prompt_router._sweep_pane(pane)

    assert len(calls) == 2 and calls[0] != calls[1], calls


