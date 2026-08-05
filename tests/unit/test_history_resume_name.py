"""``agentwire history resume`` derives a tmux-LEGAL session name (#870).

Sixth copy of the ``.`` → ``_`` mapping #869 consolidated into
``worktree.tmux_safe_name``. tmux reads ``.`` as its ``session.window``
separator and rewrites it, so for a project directory carrying one
(``~/.claude``) the session that exists is ``_claude-fork-1`` — the raw
``.claude-fork-1`` names a *window* of a session called ``.claude``, which is
what the uniqueness probe and every later ``-t <name>`` were addressing.
"""

from types import SimpleNamespace

import pytest

from agentwire import history_cli


@pytest.fixture
def resume(tmp_path, monkeypatch):
    """Drive cmd_history_resume with tmux + config stubbed out.

    Returns a callable ``(project_dir_name, name=None, existing=()) -> dict``
    with the recorded tmux argv lists and the reported session name. *existing*
    is the set of session names ``tmux has-session`` answers YES for.
    """
    from agentwire import history

    monkeypatch.setattr(history, "resolve_session_id", lambda sid, m: None)
    monkeypatch.setattr(history_cli, "load_project_config", lambda p: None)
    monkeypatch.setattr(history_cli, "load_config", lambda: {})
    monkeypatch.setattr(history_cli, "inject_soul", lambda names, cfg: names)
    monkeypatch.setattr(history_cli, "resolve_roles", lambda kind, project_roles=None: [])
    monkeypatch.setattr(
        history_cli, "build_agent_command",
        lambda posture, roles, resume_session_id=None: SimpleNamespace(command="claude"),
    )
    monkeypatch.setattr(history_cli, "_notify_portal_sessions_changed", lambda: None)
    monkeypatch.setattr(history_cli.time, "sleep", lambda s: None)

    def _run(project_dir_name, name=None, existing=()):
        project = tmp_path / project_dir_name
        project.mkdir(parents=True, exist_ok=True)
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[:2] == ["tmux", "has-session"]:
                # `-t =<name>` — exact-match form. YES only for *existing*.
                target = argv[-1].lstrip("=")
                return SimpleNamespace(returncode=0 if target in existing else 1,
                                       stdout=b"", stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(history_cli.subprocess, "run", fake_run)
        reported = {}
        monkeypatch.setattr(history_cli, "_output_json", reported.update)

        rc = history_cli.cmd_history_resume(SimpleNamespace(
            session_id="abc123", name=name, machine="local",
            project=str(project), json=True,
        ))
        created = [c for c in calls if c[:2] == ["tmux", "new-session"]]
        probed = [c[-1].lstrip("=") for c in calls if c[:2] == ["tmux", "has-session"]]
        return {
            "rc": rc,
            "created": created[0][created[0].index("-s") + 1] if created else None,
            "probed": probed,
            "reported": reported.get("session"),
        }

    return _run


class TestDerivedName:
    @pytest.mark.parametrize("project,expected", [
        (".claude", "_claude-fork-1"),
        ("dotdev.dev", "dotdev_dev-fork-1"),
        ("myapp", "myapp-fork-1"),
    ])
    def test_project_dot_is_sanitized(self, resume, project, expected):
        res = resume(project)
        assert res["rc"] == 0
        assert res["created"] == expected
        assert res["reported"] == expected

    def test_uniqueness_probe_uses_the_sanitized_name(self, resume):
        """The behavior the line exists to serve.

        A raw ``.claude-fork-1`` probe can never match — tmux has no session by
        that name — so the collision loop would exit on the first try and hand
        an occupied name to ``new-session``. Probing the sanitized name is what
        makes the ``-fork-N`` increment work for a dotted project.
        """
        res = resume(".claude", existing={"_claude-fork-1", "_claude-fork-2"})
        assert res["probed"][:2] == ["_claude-fork-1", "_claude-fork-2"]
        assert res["created"] == "_claude-fork-3"


class TestExplicitName:
    def test_operator_supplied_name_is_sanitized_too(self, resume):
        res = resume("myapp", name="my.resume")
        assert res["created"] == "my_resume"
        assert res["reported"] == "my_resume"
        # And the pre-create existence probe checked the same name.
        assert res["probed"] == ["my_resume"]

    def test_legal_name_is_left_alone(self, resume):
        res = resume("myapp", name="hand-picked")
        assert res["created"] == "hand-picked"
        assert res["reported"] == "hand-picked"

    def test_collision_on_sanitized_name_is_refused(self, resume):
        """A dotted --name colliding with the session it would really create
        must fail loudly, not create a duplicate under a different address."""
        res = resume("myapp", name="my.resume", existing={"my_resume"})
        assert res["rc"] == 1
        assert res["created"] is None
