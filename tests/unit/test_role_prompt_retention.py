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

from agentwire import role_prompts

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


class TestSweep:
    def test_reachable_prompt_survives_any_age(self, store, sessions):
        """An orchestrator conversation running for months stays resumable."""
        cid = _prompt(store, age_days=400)
        _record(sessions, "orch", conversation_ids=[cid])
        result = role_prompts.sweep(store, sessions, now=NOW)
        assert result["deleted"] == []
        assert result["kept_reachable"] == 1
        assert _names(store) == [f"{cid}.txt"]

    def test_unreachable_and_aged_out_is_deleted(self, store, sessions):
        """The pane case: nothing will ever reference it again."""
        cid = _prompt(store, age_days=45)
        result = role_prompts.sweep(store, sessions, now=NOW)
        assert result["deleted"] == [f"{cid}.txt"]
        assert result["bytes_freed"] > 0
        assert _names(store) == []
