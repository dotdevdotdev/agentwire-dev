"""Tests for agentwire/history_migrate.py — re-keying orphaned history (#871).

The two constraints that matter most here are that history is never destroyed
and that missing history is a normal outcome rather than a crash, so both get
tested directly rather than inferred from the happy path.
"""

import json
import os

import pytest

from agentwire import history_migrate as hm


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """Point the module at a throwaway ~/.claude/projects."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(hm, "PROJECTS_DIR", root)
    return root


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    """A throwaway ~/.agentwire with a recorded session named "s"."""
    monkeypatch.setattr(hm, "CONFIG_DIR", tmp_path)
    (tmp_path / "sessions" / "s").mkdir(parents=True)
    (tmp_path / "sessions" / "s" / "metadata.json").write_text("{}")
    return tmp_path


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


class TestPublishMechanics:
    """Pins for the properties a passing suite must not be able to lose.

    Each of these was verified to go red under the corresponding sabotage:
    swapping the publish to ``shutil.move``, deleting the pre-publish
    existence re-check, emptying ``_fingerprint``, and following symlinks.
    Without them the code was correct but unheld.
    """

    def test_publish_uses_rename_not_move(self, projects, monkeypatch):
        """``shutil.move`` onto an existing dir nests instead of failing.

        Pinned at the call level because the refusal check normally prevents
        us reaching a populated target — so a move/rename swap is invisible to
        every behavioural test.
        """
        seed(projects, "/old/place")
        monkeypatch.setattr(
            hm.shutil, "move",
            lambda *a, **k: pytest.fail("publish must not use shutil.move — it nests on collision"),
        )
        assert hm.apply("/old/place", "/new/place")["status"] == hm.MIGRATED

    def test_target_is_rechecked_between_plan_and_publish(self, projects, monkeypatch):
        """A concurrent claude run can create the target while we copy."""
        seed(projects, "/old/place")
        real_copytree = hm.shutil.copytree

        def racing(src, dst, **kw):
            out = real_copytree(src, dst, **kw)
            # The target appears after plan() approved, before publication.
            victim = projects / "-new-place"
            victim.mkdir()
            (victim / "other.jsonl").write_text("ARRIVED FIRST")
            return out

        monkeypatch.setattr(hm.shutil, "copytree", racing)
        result = hm.apply("/old/place", "/new/place")
        assert result["status"] == hm.TARGET_EXISTS
        assert (projects / "-new-place" / "other.jsonl").read_text() == "ARRIVED FIRST"
        assert not [p for p in projects.iterdir() if p.name.startswith(hm.STAGING_PREFIX)]

    def test_fingerprint_actually_reads_content(self, projects):
        """An empty fingerprint would make verification vacuously pass."""
        d = seed(projects, "/place")
        (d / "extra.jsonl").write_text("payload")
        fp = hm._fingerprint(d)
        assert len(fp) == 2
        assert any(size == len("payload") for size, _ in fp.values())
        # Content, not just size: same length, different bytes must differ.
        (d / "extra.jsonl").write_text("PAYLOAD")
        assert hm._fingerprint(d) != fp

    def test_symlinks_are_preserved_not_followed(self, projects, tmp_path):
        """Following them would silently inline an outside file as real data."""
        outside = tmp_path / "outside.jsonl"
        outside.write_text("not part of this history")
        d = seed(projects, "/old/place")
        (d / "link.jsonl").symlink_to(outside)
        assert hm.apply("/old/place", "/new/place")["status"] == hm.MIGRATED
        copied = projects / "-new-place" / "link.jsonl"
        assert copied.is_symlink()
        assert os.readlink(copied) == str(outside)


class TestStagingSweep:
    def test_sweeps_abandoned_staging_dirs(self, projects):
        orphan = projects / f"{hm.STAGING_PREFIX}deadbeef"
        orphan.mkdir()
        (orphan / "copy.jsonl").write_text("{}")
        assert hm.sweep_staging() == [orphan.name]
        assert not orphan.exists()

    def test_sweep_leaves_real_history_alone(self, projects):
        seed(projects, "/place")
        hm.sweep_staging()
        assert (projects / "-place" / "conv.jsonl").exists()

    def test_apply_sweeps_before_migrating(self, projects):
        orphan = projects / f"{hm.STAGING_PREFIX}stale"
        orphan.mkdir()
        seed(projects, "/old/place")
        hm.apply("/old/place", "/new/place")
        assert not orphan.exists()


class TestMixedProvenance:
    def _jsonl(self, d, name, cwd):
        (d / name).write_text(json.dumps({"type": "user", "cwd": cwd}) + "\n")

    def test_uniform_source_is_not_flagged(self, projects):
        d = projects / hm.encode_project_path("/p/one")
        d.mkdir(parents=True)
        self._jsonl(d, "a.jsonl", "/p/one")
        self._jsonl(d, "b.jsonl", "/p/one")
        result = hm.plan("/p/one", "/p/moved")
        assert result["status"] == hm.READY
        assert "mixed_provenance" not in result

    def test_foreign_transcripts_are_reported(self, projects):
        """One such directory really exists on the machine this was built on."""
        d = projects / hm.encode_project_path("/p/one")
        d.mkdir(parents=True)
        self._jsonl(d, "a.jsonl", "/p/one")
        self._jsonl(d, "b.jsonl", "/p/elsewhere")
        self._jsonl(d, "c.jsonl", "/p/elsewhere")
        result = hm.plan("/p/one", "/p/moved")
        assert result["status"] == hm.READY
        assert result["mixed_provenance"] == {"/p/one": 1, "/p/elsewhere": 2}
        assert sorted(result["foreign_files"]) == ["b.jsonl", "c.jsonl"]
        assert "/p/elsewhere (2)" in result["detail"]
        assert "LEFT IN PLACE" in result["detail"]

    def test_migration_moves_only_the_matching_transcripts(self, projects):
        """The real shape: 7 of one project + 6 of another under one key.

        Relocating all 13 would orphan the 6 — the exact property this module
        leads with. Only the matching ones move; the rest stay put.
        """
        d = projects / hm.encode_project_path("/p/one")
        d.mkdir(parents=True)
        for i in range(7):
            self._jsonl(d, f"own{i}.jsonl", "/p/one")
        for i in range(6):
            self._jsonl(d, f"other{i}.jsonl", "/p/elsewhere")

        result = hm.apply("/p/one", "/p/moved")
        assert result["status"] == hm.MIGRATED

        moved = {p.name for p in (projects / hm.encode_project_path("/p/moved")).glob("*.jsonl")}
        assert moved == {f"own{i}.jsonl" for i in range(7)}
        # Every original is still where it was — nothing was orphaned.
        assert len(list(d.glob("*.jsonl"))) == 13

    def test_prune_source_refuses_when_transcripts_were_left_behind(self, projects):
        """Otherwise --prune-source destroys exactly what we declined to move."""
        d = projects / hm.encode_project_path("/p/one")
        d.mkdir(parents=True)
        self._jsonl(d, "own.jsonl", "/p/one")
        self._jsonl(d, "other.jsonl", "/p/elsewhere")

        result = hm.apply("/p/one", "/p/moved", prune_source=True)
        assert result["status"] == hm.MIGRATED
        assert result["source_retained"] is True
        assert "NOT pruned" in result["detail"]
        assert (d / "other.jsonl").exists()

    def test_prune_source_still_works_for_a_clean_source(self, projects):
        d = projects / hm.encode_project_path("/p/one")
        d.mkdir(parents=True)
        self._jsonl(d, "own.jsonl", "/p/one")
        result = hm.apply("/p/one", "/p/moved", prune_source=True)
        assert result["source_retained"] is False
        assert not d.exists()

    def test_files_without_a_readable_cwd_travel_with_the_migration(self, projects):
        """No evidence of foreignness, and the source is retained regardless."""
        d = projects / hm.encode_project_path("/p/one")
        d.mkdir(parents=True)
        self._jsonl(d, "own.jsonl", "/p/one")
        (d / "nocwd.jsonl").write_text('{"type":"user"}\n')
        (d / "memory").mkdir()
        (d / "memory" / "MEMORY.md").write_text("# notes")

        hm.apply("/p/one", "/p/moved")
        dest = projects / hm.encode_project_path("/p/moved")
        assert (dest / "nocwd.jsonl").exists()
        assert (dest / "memory" / "MEMORY.md").exists()

    def test_unreadable_transcripts_do_not_block(self, projects):
        d = projects / hm.encode_project_path("/p/one")
        d.mkdir(parents=True)
        (d / "broken.jsonl").write_text("not json at all\n")
        (d / "nocwd.jsonl").write_text('{"type":"user"}\n')
        assert hm.plan("/p/one", "/p/moved")["status"] == hm.READY


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
    def test_unknown_session_says_so(self, projects, tmp_path, monkeypatch):
        """Not "predates #881" — that misreads a typo as a legacy session."""
        monkeypatch.setattr(hm, "CONFIG_DIR", tmp_path)
        result = hm.resolve_session("never-existed")
        assert result["status"] == hm.UNDETERMINED
        assert result["detail"] == "no such session recorded"

    def test_undetermined_without_recorded_cwd(self, projects, tmp_path, monkeypatch):
        monkeypatch.setattr(hm, "CONFIG_DIR", tmp_path)
        (tmp_path / "sessions" / "legacy").mkdir(parents=True)
        (tmp_path / "sessions" / "legacy" / "metadata.json").write_text("{}")
        monkeypatch.setattr(hm, "load_session_metadata", lambda name: {})
        result = hm.resolve_session("legacy")
        assert result["status"] == hm.UNDETERMINED
        assert "#881" in result["detail"]

    def test_undetermined_when_repo_is_gone(self, projects, session_store, monkeypatch):
        monkeypatch.setattr(
            hm, "load_session_metadata",
            lambda name: {"cwd_at_launch": "/old/place", "repo": "/vanished/repo"},
        )
        result = hm.resolve_session("s")
        assert result["status"] == hm.UNDETERMINED
        assert "cannot ask git" in result["detail"]

    def test_main_checkout_session_compares_against_the_repo_path(self, projects, session_store, tmp_path, monkeypatch):
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

    def test_worktree_session_asks_git_for_the_current_path(self, projects, session_store, tmp_path, monkeypatch):
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

    def test_undetermined_when_git_lost_the_worktree(self, projects, session_store, tmp_path, monkeypatch):
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
