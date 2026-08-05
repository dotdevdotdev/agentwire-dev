"""Tests for ``agentwire history migrate`` argument handling and reporting."""

import json

import pytest

from agentwire import history_cli
from agentwire import history_migrate as hm


class Args:
    def __init__(self, **kw):
        self.session = kw.get("session")
        self.from_path = kw.get("from_path")
        self.to_path = kw.get("to_path")
        self.all = kw.get("all", False)
        self.apply = kw.get("apply", False)
        self.prune_source = kw.get("prune_source", False)
        self.json = kw.get("json", False)


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(hm, "PROJECTS_DIR", root)
    return root


def seed(projects, cwd):
    d = projects / hm.encode_project_path(str(cwd))
    d.mkdir(parents=True)
    (d / "conv.jsonl").write_text('{"type":"user"}\n')
    return d


class TestSelectorValidation:
    @pytest.mark.parametrize("kw,expected", [
        ({}, "exactly one of"),                                 # nothing chosen
        ({"from_path": "/a"}, "must be given together"),         # --from without --to
        ({"to_path": "/b"}, "must be given together"),           # --to without --from
        ({"all": True, "session": "s"}, "exactly one of"),       # two selectors
        ({"all": True, "from_path": "/a", "to_path": "/b"}, "exactly one of"),
    ])
    def test_rejected_with_a_usable_message(self, kw, expected, projects, capsys):
        """Assert on the OUTPUT, not just the exit code.

        Checking only ``== 1`` is what let a wrong ``_output_result`` signature
        through: the message was silently swallowed into the JSON branch and
        never displayed, and every one of these still exited 1.
        """
        assert history_cli.cmd_history_migrate(Args(**kw)) == 1
        captured = capsys.readouterr()
        assert expected in (captured.out + captured.err)

    def test_rejection_in_json_mode_carries_the_error(self, projects, capsys):
        assert history_cli.cmd_history_migrate(Args(json=True)) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert "exactly one of" in payload["error"]
        assert "json_output" not in payload  # a kwarg leaking into the payload


class TestExitCodes:
    def test_dry_run_with_a_migratable_session_succeeds(self, projects, capsys):
        seed(projects, "/old/place")
        rc = history_cli.cmd_history_migrate(Args(from_path="/old/place", to_path="/new/place"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "MIGRATABLE" in out
        assert "--apply" in out
        # A dry run must not have created anything.
        assert not (projects / "-new-place").exists()

    def test_refusal_exits_nonzero(self, projects):
        seed(projects, "/old/place")
        seed(projects, "/new/place")
        rc = history_cli.cmd_history_migrate(
            Args(from_path="/old/place", to_path="/new/place", apply=True)
        )
        assert rc == 1

    def test_absent_source_exits_zero(self, projects, capsys):
        rc = history_cli.cmd_history_migrate(
            Args(from_path="/gone", to_path="/new/place", apply=True)
        )
        assert rc == 0
        assert "nothing to migrate" in capsys.readouterr().out

    def test_apply_performs_the_migration(self, projects, capsys):
        seed(projects, "/old/place")
        rc = history_cli.cmd_history_migrate(
            Args(from_path="/old/place", to_path="/new/place", apply=True)
        )
        assert rc == 0
        assert "MIGRATED" in capsys.readouterr().out
        assert (projects / "-new-place" / "conv.jsonl").exists()


class TestSweepReporting:
    def _store(self, tmp_path, monkeypatch, statuses):
        monkeypatch.setattr(hm, "known_sessions", lambda: [f"s{i}" for i in range(len(statuses))])
        monkeypatch.setattr(
            hm, "resolve_session",
            lambda name: {"session": name, "status": statuses[int(name[1:])],
                          "detail": f"detail for {statuses[int(name[1:])]}"},
        )

    def test_quiet_statuses_are_counted_not_listed(self, projects, tmp_path, monkeypatch, capsys):
        self._store(tmp_path, monkeypatch, [hm.UNDETERMINED] * 5 + [hm.ALIGNED] * 3)
        rc = history_cli.cmd_history_migrate(Args(all=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert "No orphaned history found." in out
        assert "8 session(s) not shown" in out
        # Counted, never silently dropped.
        assert "5" in out and "3" in out
        assert "s0" not in out

    def test_notable_entries_are_listed_in_full(self, projects, tmp_path, monkeypatch, capsys):
        self._store(tmp_path, monkeypatch, [hm.UNDETERMINED] * 4 + [hm.TARGET_EXISTS])
        rc = history_cli.cmd_history_migrate(Args(all=True))
        out = capsys.readouterr().out
        assert rc == 1
        assert "REFUSED" in out and "s4" in out
        assert "4 session(s) not shown" in out

    def test_json_keeps_every_result(self, projects, tmp_path, monkeypatch, capsys):
        self._store(tmp_path, monkeypatch, [hm.UNDETERMINED] * 5 + [hm.ALIGNED])
        history_cli.cmd_history_migrate(Args(all=True, json=True))
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"] is False
        assert len(payload["results"]) == 6
