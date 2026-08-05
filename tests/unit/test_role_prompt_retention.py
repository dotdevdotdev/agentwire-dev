"""Retention rule for the durable role-prompt store (#884).

SAFETY NOTE FOR ANYONE EDITING THIS FILE: every test here builds its own store
and sessions directory under ``tmp_path`` and passes them EXPLICITLY. Nothing
in this module may call :func:`role_prompts.tick` or resolve a path from
``core.CONFIG_DIR`` — a deletion pass aimed at the real store would strip the
system prompt from running agents, which is the exact failure #881 fixed.
"""

import json
import os
import uuid

import pytest

from agentwire import core, role_prompts

DAY = 86400
NOW = 1_800_000_000.0  # fixed clock — no Date.now()-style flakiness


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "store" / "role-prompts"
    d.mkdir(parents=True, mode=0o700)
    return d


@pytest.fixture
def sessions(tmp_path):
    d = tmp_path / "store" / "sessions"
    d.mkdir(parents=True)
    return d


def _prompt(store, *, age_days=0.0, cid=None, body="you are a worker"):
    """Write a prompt file with a controlled mtime. Returns its conversation id."""
    cid = cid or str(uuid.uuid4())
    path = store / f"{cid}.txt"
    path.write_text(body)
    path.chmod(0o600)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return cid


def _record(sessions, name, *, conversation_ids=None, role_prompt_path=None):
    d = sessions / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"posture": "bypass"}
    if conversation_ids is not None:
        meta["conversation_ids"] = conversation_ids
    if role_prompt_path is not None:
        meta["role_prompt_path"] = role_prompt_path
    (d / "metadata.json").write_text(json.dumps(meta))


def _names(store):
    return sorted(p.name for p in store.iterdir())


class TestReachability:
    def test_collects_every_id_in_every_chain(self, sessions):
        _record(sessions, "orch", conversation_ids=["a", "b"])
        _record(sessions, "worker", conversation_ids=["c"])
        assert role_prompts.reachable_conversation_ids(sessions) == {"a", "b", "c"}

    def test_role_prompt_path_stem_counts_too(self, sessions, tmp_path):
        _record(sessions, "s", role_prompt_path=str(tmp_path / "role-prompts" / "z.txt"))
        assert role_prompts.reachable_conversation_ids(sessions) == {"z"}

    def test_a_remote_mirror_path_still_yields_its_id(self, sessions):
        _record(sessions, "s", role_prompt_path="$HOME/.agentwire/role-prompts/q.txt")
        assert "q" in role_prompts.reachable_conversation_ids(sessions)

    def test_unreadable_record_contributes_nothing(self, sessions):
        _record(sessions, "good", conversation_ids=["a"])
        (sessions / "bad").mkdir()
        (sessions / "bad" / "metadata.json").write_text("{ truncated")
        assert role_prompts.reachable_conversation_ids(sessions) == {"a"}

    def test_missing_sessions_dir_is_empty_not_an_error(self, tmp_path):
        assert role_prompts.reachable_conversation_ids(tmp_path / "nope") == set()

    def test_a_nested_project_branch_record_is_reachable(self, sessions):
        """Session names contain SLASHES, so records NEST — the standard case.

        ``project/branch`` is what every ``agentwire worktree`` and every
        scheduler dispatch is named (``tmux_safe_name`` deliberately preserves
        ``/``; it only rewrites ``.`` and ``:``), and
        ``core.session_metadata_path`` joins that name straight onto the path
        — so the record lands at ``sessions/project/branch/metadata.json``, one
        level deeper than a flat glob looks.

        This is the shape 34 green tests missed by using only flat fixture
        names: on the real store the flat glob saw 469 of 1106 records, so 58%
        of live conversations read as unreachable — and a sweep deletes exactly
        the prompt a stranded worktree session would have been recovered with.
        """
        _record(sessions, "documentscribe/fix-942-importer-types",
                conversation_ids=["nested-cid"])
        assert "nested-cid" in role_prompts.reachable_conversation_ids(sessions)

    def test_records_at_both_depths_are_collected_together(self, sessions):
        """Flat and nested coexist in one store — neither may shadow the other."""
        _record(sessions, "agentwire", conversation_ids=["flat"])
        _record(sessions, "agentwire-dev/fix-884", conversation_ids=["nested"])
        _record(sessions, "proj/sub/deeper", conversation_ids=["deeper"])
        assert role_prompts.reachable_conversation_ids(sessions) == {
            "flat", "nested", "deeper"}


class TestSweep:
    def test_reachable_prompt_survives_any_age(self, store, sessions):
        """An orchestrator conversation running for months stays resumable."""
        cid = _prompt(store, age_days=400)
        _record(sessions, "orch", conversation_ids=[cid])
        result = role_prompts.sweep(store, sessions, now=NOW)
        assert result["deleted"] == []
        assert result["kept_reachable"] == 1
        assert _names(store) == [f"{cid}.txt"]

    def test_a_worktree_sessions_prompt_survives_any_age(self, store, sessions):
        """The data-loss case, end to end.

        A long-running worktree session (``project/branch`` — nested record)
        whose prompt has aged past the threshold. If reachability misses the
        nested record, the sweep deletes the prompt of a LIVE session, and the
        two bugs compose: #901 strands the session at a bare shell, recovery
        re-runs its stored launch line, and the ``--append-system-prompt``
        file it reads by path is gone — so it comes back role-less, which is
        #881's original bug made permanent by its own cleanup.
        """
        cid = _prompt(store, age_days=90)
        _record(sessions, "documentscribe/fix-911-deploy-gates",
                conversation_ids=[cid])

        result = role_prompts.sweep(store, sessions, now=NOW)

        assert result["deleted"] == []
        assert result["kept_reachable"] == 1
        assert _names(store) == [f"{cid}.txt"]

    def test_reachable_via_an_older_link_in_the_chain(self, store, sessions):
        """--fork-session appends; an earlier id is still resumable."""
        old = _prompt(store, age_days=400)
        new = _prompt(store, age_days=1)
        _record(sessions, "orch", conversation_ids=[old, new])
        assert role_prompts.sweep(store, sessions, now=NOW)["deleted"] == []

    def test_unreachable_and_aged_out_is_deleted(self, store, sessions):
        """The pane case: nothing will ever reference it again."""
        cid = _prompt(store, age_days=45)
        result = role_prompts.sweep(store, sessions, now=NOW)
        assert result["deleted"] == [f"{cid}.txt"]
        assert result["bytes_freed"] > 0
        assert _names(store) == []

    def test_unreachable_but_young_is_kept(self, store, sessions):
        """A live pane's prompt must not vanish out from under it."""
        cid = _prompt(store, age_days=3)
        result = role_prompts.sweep(store, sessions, now=NOW)
        assert result["deleted"] == []
        assert result["kept_young"] == 1
        assert _names(store) == [f"{cid}.txt"]

    def test_exactly_at_the_boundary_is_kept(self, store, sessions):
        _prompt(store, age_days=role_prompts.DEFAULT_MAX_AGE_DAYS)
        assert role_prompts.sweep(store, sessions, now=NOW)["deleted"] == []

    def test_max_age_is_configurable(self, store, sessions):
        cid = _prompt(store, age_days=3)
        assert role_prompts.sweep(
            store, sessions, max_age_days=1, now=NOW)["deleted"] == [f"{cid}.txt"]

    def test_dry_run_reports_the_same_set_and_deletes_nothing(self, store, sessions):
        cid = _prompt(store, age_days=45)
        result = role_prompts.sweep(store, sessions, now=NOW, dry_run=True)
        assert result["deleted"] == [f"{cid}.txt"]
        assert result["dry_run"] is True
        assert _names(store) == [f"{cid}.txt"]

    def test_a_mixed_store_keeps_exactly_the_right_files(self, store, sessions):
        live = _prompt(store, age_days=200)
        pane_old = _prompt(store, age_days=90)
        pane_new = _prompt(store, age_days=2)
        _record(sessions, "orch", conversation_ids=[live])
        result = role_prompts.sweep(store, sessions, now=NOW)
        assert result["deleted"] == [f"{pane_old}.txt"]
        assert _names(store) == sorted([f"{live}.txt", f"{pane_new}.txt"])

    def test_missing_store_is_a_no_op(self, tmp_path, sessions):
        absent = tmp_path / "not-there"
        result = role_prompts.sweep(absent, sessions, now=NOW)
        assert result["deleted"] == []
        assert not absent.exists()  # never created as a side effect

    def test_an_unlink_failure_is_reported_not_claimed_as_deleted(
            self, store, sessions, monkeypatch):
        _prompt(store, age_days=45)

        def boom(self, **kwargs):
            raise PermissionError("nope")

        monkeypatch.setattr("pathlib.Path.unlink", boom)
        result = role_prompts.sweep(store, sessions, now=NOW)
        assert result["deleted"] == []
        assert len(result["failed"]) == 1
        assert "nope" in result["failed"][0]


class TestSweepRefusesUnrecognizedEntries:
    """Even aimed at the wrong directory, the sweep can't delete a stranger."""

    def test_non_uuid_filenames_are_never_deleted(self, store, sessions):
        stranger = store / "important-notes.txt"
        stranger.write_text("not a role prompt")
        os.utime(stranger, (NOW - 400 * DAY, NOW - 400 * DAY))
        result = role_prompts.sweep(store, sessions, now=NOW)
        assert result["deleted"] == []
        assert result["skipped_unrecognized"] == ["important-notes.txt"]
        assert stranger.exists()

    def test_wrong_suffix_is_never_deleted(self, store, sessions):
        cid = str(uuid.uuid4())
        other = store / f"{cid}.json"
        other.write_text("{}")
        os.utime(other, (NOW - 400 * DAY, NOW - 400 * DAY))
        assert role_prompts.sweep(store, sessions, now=NOW)["deleted"] == []
        assert other.exists()

    def test_directories_are_never_deleted(self, store, sessions):
        sub = store / f"{uuid.uuid4()}.txt"
        sub.mkdir()
        os.utime(sub, (NOW - 400 * DAY, NOW - 400 * DAY))
        assert role_prompts.sweep(store, sessions, now=NOW)["deleted"] == []
        assert sub.is_dir()

    def test_symlinks_are_never_followed_or_deleted(self, store, sessions, tmp_path):
        target = tmp_path / "elsewhere.txt"
        target.write_text("someone else's file")
        link = store / f"{uuid.uuid4()}.txt"
        link.symlink_to(target)
        assert role_prompts.sweep(store, sessions, now=NOW)["deleted"] == []
        assert link.is_symlink()
        assert target.exists()


class TestStatus:
    def test_counts_and_never_deletes(self, store, sessions):
        live = _prompt(store, age_days=100, body="x" * 10)
        _prompt(store, age_days=90, body="y" * 20)
        _prompt(store, age_days=1, body="z" * 30)
        (store / "stray").write_text("!")
        _record(sessions, "orch", conversation_ids=[live])

        s = role_prompts.status(store, sessions, now=NOW)
        assert (s["total"], s["reachable"], s["unreachable"], s["expired"]) == (3, 1, 2, 1)
        assert s["bytes"] == 60
        assert s["expired_bytes"] == 20
        assert s["unrecognized"] == ["stray"]
        assert len(_names(store)) == 4  # nothing removed

    def test_missing_store_reports_empty(self, tmp_path, sessions):
        s = role_prompts.status(tmp_path / "nope", sessions, now=NOW)
        assert (s["exists"], s["total"]) == (False, 0)


class TestStoreLocationFollowsTheTestSeam:
    """The trap this design exists to defuse (#884).

    ``ROLE_PROMPTS_DIR`` was a module constant computed at IMPORT time, so
    patching ``core.CONFIG_DIR`` — this repo's established isolation seam —
    did not redirect it. Harmless for a writer; a data-destruction trap the
    moment anything sweeps that directory.
    """

    def test_role_prompts_dir_follows_a_patched_config_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path / "fake-home")
        assert core.role_prompts_dir() == tmp_path / "fake-home" / "role-prompts"

    def test_write_role_prompt_lands_under_the_patched_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path / "fake-home")
        path = core.write_role_prompt(str(uuid.uuid4()), "be a worker")
        assert path.parent == tmp_path / "fake-home" / "role-prompts"
        assert path.read_text() == "be a worker"
