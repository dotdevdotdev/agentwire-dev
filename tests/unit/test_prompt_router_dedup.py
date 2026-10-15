"""#1049 — two permission prompts of the same shape must not share a hash.

kind + question + option labels is identical for every "Do you want to
proceed?" confirm, so the dedup marker treated a second, different command as
the already-delivered first one and never re-notified. The hash now includes
the dialog body (the command/file being approved), whitespace-free so a
redraw at another pane width does not change it.
"""

import textwrap
from datetime import timedelta

from agentwire import prompt_router
from agentwire.prompt_router import detect_prompt

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


def _render_at_width(screen: str, width: int) -> str:
    """Re-render as a *width*-column pane: rules span the pane, text wraps."""
    out = []
    for line in screen.split("\n"):
        if line.strip() and set(line.strip()) == {"─"}:
            out.append("─" * width)
        elif len(line) <= width:
            out.append(line)
        else:
            indent = " " * (len(line) - len(line.lstrip()))
            out += textwrap.wrap(line, width, subsequent_indent=indent)
    return "\n".join(out)


CMD_A = 'psql -c "$(cat /Users/dotdev/projects/agentwire-dev/queries/long-report-query-alpha.sql)"'
CMD_B = 'psql -c "$(cat /Users/dotdev/projects/agentwire-dev/queries/long-report-query-bravo.sql)"'


def test_different_commands_get_different_hashes():
    a = detect_prompt(_dialog(CMD_A))
    b = detect_prompt(_dialog(CMD_B))
    assert a is not None and b is not None
    assert a.content_hash() != b.content_hash()


def test_same_prompt_keeps_one_hash_at_every_width():
    screen = _dialog(CMD_A)
    want = detect_prompt(screen).content_hash()
    bad = {}
    for width in range(40, 201):
        info = detect_prompt(_render_at_width(screen, width))
        got = info.content_hash() if info else None
        if got != want:
            bad[width] = got
    assert not bad, bad


def test_volatile_text_above_the_dialog_does_not_change_the_hash():
    quiet = detect_prompt(_dialog(CMD_A, above="✻ Baked for 12s"))
    busy = detect_prompt(_dialog(CMD_A, above="✶ Thinking… (47s · ↓ 3.1k tokens)"))
    assert quiet.content_hash() == busy.content_hash()


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


def _long_command(last_step: str) -> str:
    steps = [f"echo step-{i}-of-the-migration-plan && sleep 1" for i in range(11)]
    return " && \\\n   ".join(steps + [last_step])


def test_hash_survives_cropping_and_width_and_still_tells_commands_apart():
    """The rule scrolls off the top of a short pane (`_capture` has no -S)."""
    a = _dialog(_long_command("psql -c 'select alpha'"))
    b = _dialog(_long_command("psql -c 'select bravo'"))
    want_a = detect_prompt(a).content_hash()
    want_b = detect_prompt(b).content_hash()
    assert want_a != want_b
    bad = []
    for height in (24, 30, 40):
        for width in range(40, 201):
            hashes = []
            for screen in (a, b):
                shown = _render_at_width(screen, width).split("\n")[-height:]
                info = detect_prompt("\n".join(shown))
                hashes.append(info.content_hash() if info else None)
            if hashes != [want_a, want_b]:
                bad.append((height, width))
    assert not bad, bad
