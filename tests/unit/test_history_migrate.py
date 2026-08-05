"""Tests for agentwire/history_migrate.py — re-keying orphaned history (#871).

The two constraints that matter most here are that history is never destroyed
and that missing history is a normal outcome rather than a crash, so both get
tested directly rather than inferred from the happy path.
"""

import pytest

from agentwire import history_migrate as hm


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """Point the module at a throwaway ~/.claude/projects."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(hm, "PROJECTS_DIR", root)
    return root


def seed(projects, cwd, files=(("conv.jsonl", '{"type":"user"}\n'),)):
    d = projects / hm.encode_project_path(str(cwd))
    d.mkdir(parents=True)
    for name, content in files:
        (d / name).write_text(content)
    return d


class TestHistoryKeyCandidates:
    def test_single_candidate_for_a_plain_path(self):
        assert hm.history_key_candidates("/nowhere/at/all") == ["-nowhere-at-all"]

    def test_symlinked_path_yields_both_forms(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        candidates = hm.history_key_candidates(link)
        assert len(candidates) == 2
        assert hm.encode_project_path(str(real)) in candidates


class TestPlan:
    def test_ready_when_source_exists_and_target_does_not(self, projects):
        seed(projects, "/old/place")
        result = hm.plan("/old/place", "/new/place")
        assert result["status"] == hm.READY
        assert result["files"] == 1
        assert result["target"].endswith("-new-place")

    def test_source_absent_is_a_normal_outcome(self, projects):
        """A recorded id does not guarantee a resumable conversation."""
        result = hm.plan("/gone/missing", "/new/place")
        assert result["status"] == hm.SOURCE_ABSENT
        assert "nothing to migrate" in result["detail"]

    def test_aligned_when_nothing_moved(self, projects):
        seed(projects, "/same/place")
        assert hm.plan("/same/place", "/same/place")["status"] == hm.ALIGNED

    def test_target_exists_refuses(self, projects):
        seed(projects, "/old/place")
        seed(projects, "/new/place")
        result = hm.plan("/old/place", "/new/place")
        assert result["status"] == hm.TARGET_EXISTS
        assert "refusing to merge" in result["detail"]

    def test_plan_writes_nothing(self, projects):
        seed(projects, "/old/place")
        before = sorted(p.name for p in projects.iterdir())
        hm.plan("/old/place", "/new/place")
        assert sorted(p.name for p in projects.iterdir()) == before


class TestApply:
    def test_migrates_and_verifies(self, projects):
        src = seed(projects, "/old/place", files=(("a.jsonl", "one"), ("b.jsonl", "two")))
        result = hm.apply("/old/place", "/new/place")
        assert result["status"] == hm.MIGRATED
        target = projects / "-new-place"
        assert (target / "a.jsonl").read_text() == "one"
        assert (target / "b.jsonl").read_text() == "two"
        # Source is kept: the cheapest recovery from a bad migration.
        assert result["source_retained"] is True
        assert src.exists()

    def test_copies_nested_content(self, projects):
        d = seed(projects, "/old/place")
        (d / "memory").mkdir()
        (d / "memory" / "MEMORY.md").write_text("# notes")
        hm.apply("/old/place", "/new/place")
        assert (projects / "-new-place" / "memory" / "MEMORY.md").read_text() == "# notes"

    def test_refuses_existing_target_without_touching_it(self, projects):
        seed(projects, "/old/place", files=(("a.jsonl", "source"),))
        seed(projects, "/new/place", files=(("a.jsonl", "PRECIOUS"),))
        result = hm.apply("/old/place", "/new/place")
        assert result["status"] == hm.TARGET_EXISTS
        assert (projects / "-new-place" / "a.jsonl").read_text() == "PRECIOUS"
        assert (projects / "-old-place" / "a.jsonl").read_text() == "source"

    def test_absent_source_does_not_raise(self, projects):
        result = hm.apply("/gone/missing", "/new/place")
        assert result["status"] == hm.SOURCE_ABSENT
        assert not (projects / "-new-place").exists()

    def test_prune_source_removes_only_after_success(self, projects):
        seed(projects, "/old/place")
        result = hm.apply("/old/place", "/new/place", prune_source=True)
        assert result["status"] == hm.MIGRATED
        assert result["source_retained"] is False
        assert not (projects / "-old-place").exists()
        assert (projects / "-new-place" / "conv.jsonl").exists()

    def test_prune_source_keeps_source_when_refused(self, projects):
        seed(projects, "/old/place")
        seed(projects, "/new/place")
        hm.apply("/old/place", "/new/place", prune_source=True)
        assert (projects / "-old-place").exists()

    def test_failed_verification_leaves_both_sides_alone(self, projects, monkeypatch):
        seed(projects, "/old/place")
        calls = {"n": 0}
        real = hm._fingerprint

        def drifting(root):
            calls["n"] += 1
            out = real(root)
            return {**out, "phantom.jsonl": (1, "deadbeef")} if calls["n"] == 1 else out

        monkeypatch.setattr(hm, "_fingerprint", drifting)
        result = hm.apply("/old/place", "/new/place")
        assert result["status"] == hm.ERROR
        assert "nothing was changed" in result["detail"]
        assert not (projects / "-new-place").exists()
        assert (projects / "-old-place" / "conv.jsonl").exists()

    def test_no_staging_directory_is_left_behind(self, projects):
        seed(projects, "/old/place")
        hm.apply("/old/place", "/new/place")
        assert not [p for p in projects.iterdir() if p.name.startswith(".agentwire-migrate-")]

    def test_never_nests_the_source_inside_an_existing_target(self, projects):
        """The #868 failure shape: reporting success while doing nothing useful.

        ``shutil.move(src, dst)`` with an existing *dst* does not fail — POSIX
        semantics put src INSIDE dst as ``dst/<basename(src)>``, burying the
        transcripts one level deeper than Claude Code ever looks. We refuse
        instead, so the target keeps exactly the entries it started with.
        """
        seed(projects, "/old/place")
        dest = seed(projects, "/new/place")
        hm.apply("/old/place", "/new/place")
        assert [p.name for p in dest.iterdir()] == ["conv.jsonl"]
        assert not any(p.is_dir() for p in dest.iterdir())

    def test_colliding_cwds_share_a_directory_and_need_no_migration(self, projects):
        """``/p/a_b`` and ``/p/a.b`` are distinct cwds encoding to one name.

        The encoding is non-injective, so this is a real state, not a bug we
        can fix — the two projects genuinely share a history directory. The
        honest answer is that there is nothing to move.
        """
        assert hm.encode_project_path("/p/a_b") == hm.encode_project_path("/p/a.b")
        seed(projects, "/p/a_b")
        assert hm.plan("/p/a_b", "/p/a.b")["status"] == hm.ALIGNED


class TestResumable:
    """``resumable(id, cwd) == exists(<encoded-cwd>/<id>.jsonl)`` — one predicate."""

    def test_true_when_the_transcript_is_there(self, projects):
        d = seed(projects, "/place")
        (d / "abc-123.jsonl").write_text("{}")
        assert hm.resumable("abc-123", "/place") is True

    def test_false_for_a_launched_but_never_prompted_session(self, projects):
        """A valid recorded id with no transcript: the .jsonl is written lazily."""
        assert hm.resumable("abc-123", "/never/prompted") is False

    def test_false_when_the_directory_exists_but_the_id_does_not(self, projects):
        seed(projects, "/place")
        assert hm.resumable("not-here", "/place") is False

    def test_follows_the_history_after_a_migration(self, projects):
        d = seed(projects, "/old/place")
        (d / "abc-123.jsonl").write_text("{}")
        assert hm.resumable("abc-123", "/new/place") is False
        hm.apply("/old/place", "/new/place")
        assert hm.resumable("abc-123", "/new/place") is True


class TestResolveSession:
    def test_undetermined_without_recorded_cwd(self, projects, monkeypatch):
        monkeypatch.setattr(hm, "load_session_metadata", lambda name: {})
        result = hm.resolve_session("nope")
        assert result["status"] == hm.UNDETERMINED
        assert "#881" in result["detail"]

    def test_undetermined_when_repo_is_gone(self, projects, monkeypatch):
        monkeypatch.setattr(
            hm, "load_session_metadata",
            lambda name: {"cwd_at_launch": "/old/place", "repo": "/vanished/repo"},
        )
        result = hm.resolve_session("s")
        assert result["status"] == hm.UNDETERMINED
        assert "cannot ask git" in result["detail"]

    def test_main_checkout_session_compares_against_the_repo_path(self, projects, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        seed(projects, "/old/place")
        monkeypatch.setattr(
            hm, "load_session_metadata",
            lambda name: {"cwd_at_launch": "/old/place", "repo": str(repo), "worktree_path": None},
        )
        result = hm.resolve_session("s")
        assert result["status"] == hm.READY
        assert result["new_cwd"] == str(repo)
        assert result["session"] == "s"

    def test_worktree_session_asks_git_for_the_current_path(self, projects, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        moved = tmp_path / "moved-worktree"
        seed(projects, "/old/place")
        monkeypatch.setattr(
            hm, "load_session_metadata",
            lambda name: {
                "cwd_at_launch": "/old/place", "repo": str(repo),
                "worktree_path": "/old/place", "branch": "feat",
            },
        )
        monkeypatch.setattr(
            "agentwire.worktree.find_git_worktree",
            lambda project_path, **kw: {"path": moved, "branch": "feat"},
        )
        result = hm.resolve_session("s")
        assert result["status"] == hm.READY
        assert result["new_cwd"] == str(moved)

    def test_undetermined_when_git_lost_the_worktree(self, projects, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            hm, "load_session_metadata",
            lambda name: {
                "cwd_at_launch": "/old/place", "repo": str(repo),
                "worktree_path": "/old/place", "branch": "feat",
            },
        )
        monkeypatch.setattr("agentwire.worktree.find_git_worktree", lambda project_path, **kw: None)
        assert hm.resolve_session("s")["status"] == hm.UNDETERMINED


class TestScan:
    def test_scan_covers_every_recorded_session(self, projects, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        for name in ("alpha", "beta"):
            (sessions / name).mkdir(parents=True)
            (sessions / name / "metadata.json").write_text("{}")
        (sessions / "no-metadata").mkdir()
        monkeypatch.setattr(hm, "CONFIG_DIR", tmp_path)
        assert hm.known_sessions() == ["alpha", "beta"]
        assert [r["session"] for r in hm.scan()] == ["alpha", "beta"]

    def test_no_sessions_dir_is_not_an_error(self, projects, tmp_path, monkeypatch):
        monkeypatch.setattr(hm, "CONFIG_DIR", tmp_path / "absent")
        assert hm.scan() == []
