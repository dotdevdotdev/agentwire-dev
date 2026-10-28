"""#1179 — baseline awareness: the opening roster and ``project_context``.

The owner's scope: at all times the buddy knows every open session and every
configured project (path, one-line overview, what's live, recent activity) and
which tools deep-dive them — compact and budgeted, ranked by recent activity,
never an unbounded dump. Deep dives stay tool calls.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentwire.voice_layer import digest, project_read, realtime, server, tools

NOW = datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc)
TASKS_FILE = ".agentwire" + ".tasks.yml"


def _iso(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _fleet(n_projects: int, n_sessions: int, tmp: Path):
    projects = [{"name": f"proj{i:03d}", "path": str(tmp / f"proj{i:03d}"), "machine": "local"}
                for i in range(n_projects)]
    sessions = [{"name": f"proj{i % max(n_projects, 1):03d}-w{i}",
                 "path": str(tmp / f"proj{i % max(n_projects, 1):03d}"),
                 "role": "worker", "parent": "orch", "service": False}
                for i in range(n_sessions)]
    sessions.append({"name": "portal", "service": True, "service_healthy": True})
    # Session i last did something i minutes ago: lower index = more recent.
    activity = [{"ts": _iso(i + 1), "event": "session_idle", "session": s["name"]}
                for i, s in enumerate(sessions[:-1])]
    return {"projects": projects, "sessions": sessions, "activity": activity}


def _fetch(fleet):
    def fetch(argv):
        key = {"list": "sessions", "projects": "projects", "activity": "activity"}[argv[0]]
        return {"success": True, key: fleet[key]}
    return fetch


def _digest(fleet, overview=lambda path: f"about {Path(path).name}"):
    return digest.build_digest(fetch=_fetch(fleet), now=NOW.timestamp(), overview=overview)


def test_small_fleet_lists_every_session_and_project_ranked_by_recency(tmp_path):
    fleet = _fleet(3, 4, tmp_path)
    fleet["projects"].append({"name": "idle", "path": str(tmp_path / "idle"), "machine": "local"})
    out = _digest(fleet)
    roster = out.split("</fleet_roster>")[0]
    missing = [n for n in [s["name"] for s in fleet["sessions"][:-1]] + ["proj000", "proj001", "proj002", "idle"]
               if f"- {n}" not in roster]
    assert not missing, (missing, roster)
    assert "1 services, all healthy" in roster
    assert "- portal:" not in roster  # healthy services collapse to the count
    # proj000-w0 was active 1m ago and ranks first; the idle project last.
    order = [ln.split()[1] for ln in roster.splitlines() if ln.startswith("- proj") or ln.startswith("- idle")]
    assert order.index("proj000-w0:") < order.index("proj001-w1:")
    assert [o for o in order if not o.endswith(":")][-1].startswith("idle")
    row = next(ln for ln in roster.splitlines() if ln.startswith("- proj000 ("))
    assert "about proj000" in row and "live" in row and "active 1m ago" in row
    assert str(tmp_path / "proj000") in row or "~" in row
    for tool in ("project_lookup", "project_context", "fleet_session_output", "fleet_activity"):
        assert tool in out, tool


def test_a_large_fleet_stays_within_budget_and_keeps_the_most_recent(tmp_path):
    out = _digest(_fleet(200, 300, tmp_path))
    roster = out.split("</fleet_roster>")[0] + "</fleet_roster>"
    assert len(roster) <= digest.MAX_CHARS + 120, len(roster)
    assert "- proj000-w0:" in roster  # the most recent survives truncation
    assert "fleet_sessions" in roster and "fleet_projects" in roster  # says where the rest are
    assert "proj199-w199" not in roster


def test_a_failing_source_degrades_to_a_note_and_never_raises():
    def broken(argv):
        raise RuntimeError("tmux gone")
    out = digest.build_digest(fetch=broken, now=NOW.timestamp(), overview=lambda p: "")
    assert "sessions unavailable" in out and "projects unavailable" in out
    assert "project_lookup" in out


def test_the_roster_rides_into_the_minted_instructions(monkeypatch):
    seen = {}
    monkeypatch.setattr(realtime, "mint_session",
                        lambda **kw: seen.update(kw) or {"client_secret": "x"})
    bridge = server.BuddyBridge("buddy", "tok", roster=lambda: "<fleet_roster>R-42</fleet_roster>")
    assert bridge.mint({})["success"]
    assert "R-42" in seen["instructions"]


def test_overview_line_skips_what_is_not_a_description(tmp_path):
    cases = {
        "# Title\n\nReal line here.\n": "Real line here.",
        "@AGENTS.md\n": "",
        "# T\nThis file provides guidance to Claude Code.\n\nThe app does X.\n": "The app does X.",
        "```\ncode\n```\n![badge](x)\n- list\nDoes Y.\n": "Does Y.",
        "This is a [Next.js](x) project bootstrapped with create-next-app.\nZ.\n": "Z.",
    }
    bad = []
    for i, (text, want) in enumerate(cases.items()):
        root = tmp_path / str(i)
        root.mkdir()
        (root / "README.md").write_text(text)
        got = digest.overview_line(str(root))
        if got != want:
            bad.append((text, got, want))
    assert not bad, bad


# ---------------------------------------------------------------------------
# project_context
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("@AGENTS.md\n")
    (root / "AGENTS.md").write_text("Ship via the release script.\n")
    (root / ".claude" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: How to ship it.\n---\nbody\n")
    (root / TASKS_FILE).write_text("tasks:\n  nightly:\n    prompt: Run the nightly sweep\n")
    claude_projects = tmp_path / "claude-projects"
    from agentwire import history
    mem = claude_projects / history.encode_project_path(str(root.resolve())) / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [Release flow](r.md) — tag then publish\n")
    monkeypatch.setattr(history, "claude_projects_dir", lambda: claude_projects)
    calls = []
    monkeypatch.setattr(project_read, "run_agentwire_cmd", lambda argv, **kw: calls.append(argv) or {
        "success": True, "projects": [{"name": "proj", "path": str(root), "machine": "local"}]})
    return calls


def test_project_context_reads_the_projects_own_notes_as_marked_data(project):
    result = tools.dispatch("project_context", {"project": "proj"}, "buddy")
    assert result["success"], result
    c = result["untrusted_content"]
    assert c["claude_md"].startswith("Ship via the release script")  # @AGENTS.md followed
    assert "tag then publish" in c["memory_index"]
    assert c["skills"] == [{"name": "deploy", "description": "How to ship it."}]
    assert c["tasks"] == [{"name": "nightly", "about": "Run the nightly sweep"}]
    assert result["note"] == project_read.PROJECT_FILES_NOTE
    assert set(result) == {"success", "project", "part", "untrusted_content", "note"}
    assert project == [["projects", "list", "--machine", "local"]]


def test_project_context_refuses_bad_shapes_before_reading_anything(project):
    shapes = [
        {"project": "proj", "part": "secrets"},
        {"project": "proj", "path": "/etc/passwd"},
        {"project": "../etc"},
        {"project": "-x"},
        {"project": "proj", "part": "all", "file": "~/.ssh/id_rsa"},
    ]
    leaked = [s for s in shapes
              if tools.dispatch("project_context", s, "buddy")["success"] is not False]
    assert not leaked, leaked
    assert project == []
