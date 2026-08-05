"""The suite must never write into the real ~/.agentwire (#893).

This is the regression pin for a defect found by accident: the record at
``~/.agentwire/sessions/resumed/metadata.json`` was written by the test suite
and had grown to 80 fabricated conversation ids, one chain entry per full-suite
run. It was not merely untidy — it corrupted a measurement. Sizing #871's
orphaned-history doctor check against the real store showed 28 recorded ids
with no transcript, and every one of them came from that single polluted
record. Genuinely orphaned: zero. A threshold calibrated on that would have
been tuned entirely against noise the tests invented.

These tests exercise the writers directly rather than asserting on the store's
current contents, so they stay meaningful after the stale record is deleted.
"""

from pathlib import Path

import pytest

REAL_HOME = Path.home() / ".agentwire"


def _snapshot() -> set:
    if not REAL_HOME.exists():
        return set()
    return {(str(p), p.stat().st_mtime_ns if p.exists() else None)
            for p in REAL_HOME.rglob("*")}


class TestRealHomeIsUntouched:
    def test_home_env_points_away_from_the_real_home(self):
        """The single lever that redirects every call-time ``Path.home()``.

        ``Path.home()`` resolves through ``expanduser``, which reads ``$HOME``,
        so redirecting the variable catches every path computed at call time —
        the module-level constants frozen at import are handled separately.
        """
        import os

        # The real home is where REAL_HOME (captured at import) still lives.
        assert Path.home() != REAL_HOME.parent, "HOME still resolves to the real user home"
        assert Path(os.environ["HOME"]) == Path.home()
        # expanduser goes the same way, so `~`-relative paths are covered too.
        assert Path("~/.agentwire").expanduser() != REAL_HOME

    def test_a_freshly_computed_config_path_is_redirected(self):
        """What a lazily-imported module would compute on first import."""
        assert (Path.home() / ".agentwire") != REAL_HOME

    def test_config_dir_is_not_the_real_one(self):
        from agentwire import core

        assert core.CONFIG_DIR != REAL_HOME
        assert not str(core.CONFIG_DIR).startswith(str(REAL_HOME))

    def test_recording_a_session_launch_writes_nothing_real(self):
        """The exact writer that produced the polluted record.

        ``cmd_history_resume`` calls ``record_session_launch``, which appends
        to ``conversation_ids`` — a chain by design (#871), so every suite run
        added another fabricated id.
        """
        from agentwire import core

        before = _snapshot()
        agent = core.AgentCommand(
            command="claude", posture="bypass", roles=["orchestrator"],
            conversation_id="00000000-0000-4000-8000-000000000000",
            role_prompt_path=None,
        )
        core.record_session_launch("resumed", agent, Path.cwd(), role="orchestrator")
        assert _snapshot() == before

        # ...and it did write, to the redirected location.
        assert (core.CONFIG_DIR / "sessions" / "resumed" / "metadata.json").is_file()

    def test_no_agentwire_module_still_points_at_the_real_home(self):
        """Static check: catches a NEW module constant the moment it appears.

        Import-time constants (``Path.home() / ".agentwire" / ...``) are frozen
        before any fixture runs, so redirecting ``$HOME`` alone does not move
        them. Roughly forty exist across ~25 modules; enumerating them by hand
        would rot immediately, so the isolation fixture rebinds them by walking
        loaded modules, and this asserts the walk actually covered everything.
        """
        import sys

        leaked = []
        for module in list(sys.modules.values()):
            name = getattr(module, "__name__", "")
            if not name.startswith("agentwire"):
                continue
            for attr, value in list(vars(module).items()):
                if isinstance(value, Path) and (
                    value == REAL_HOME or REAL_HOME in value.parents
                ):
                    leaked.append(f"{name}.{attr} = {value}")
        assert not leaked, "still pointing at the real ~/.agentwire:\n  " + "\n  ".join(leaked)


class TestLazyImportsCannotFreezeAFakeHome:
    """The subtle failure the redirect had to be fixed for.

    Much of this codebase imports lazily inside functions. Before the eager
    import in ``conftest``, the first test to trigger such an import did it
    while ``$HOME`` already pointed at *that test's* tmp directory, so the
    module computed ``CONFIG_DIR = Path.home() / ".agentwire"`` against the
    fake home and froze there for the rest of the session — monkeypatch never
    patched it, so there was nothing to restore. Every later test then read a
    constant belonging to a long-finished test.
    """

    def test_lazily_imported_modules_are_loaded_up_front(self):
        import sys

        # ``agentwire.__main__`` is the one the old bug travelled through:
        # test helpers do `from agentwire.__main__ import build_agent_command`.
        assert "agentwire.__main__" in sys.modules
        assert "agentwire.core" in sys.modules

    def test_no_module_points_at_another_tests_home(self, _isolate_agentwire_home):
        """Every redirected constant belongs to THIS test, not a previous one."""
        import re
        import sys

        mine = str(_isolate_agentwire_home)
        other_home = re.compile(r"/home\d+/\.agentwire")
        stray = []
        for module in list(sys.modules.values()):
            if not getattr(module, "__name__", "").startswith("agentwire"):
                continue
            for attr, value in list(vars(module).items()):
                if not isinstance(value, Path):
                    continue
                text = str(value)
                if other_home.search(text) and not text.startswith(mine):
                    stray.append(f"{module.__name__}.{attr} = {text}")
        assert not stray, "constants frozen to another test's home:\n  " + "\n  ".join(stray)


class TestGuardItself:
    def test_snapshot_detects_a_change(self, tmp_path, monkeypatch):
        """The guard must be capable of failing, not merely of passing."""
        monkeypatch.setattr(f"{__name__}.REAL_HOME", tmp_path, raising=False)
        import tests.unit.test_home_isolation as mod

        monkeypatch.setattr(mod, "REAL_HOME", tmp_path)
        before = mod._snapshot()
        (tmp_path / "intruder.json").write_text("{}")
        assert mod._snapshot() != before


@pytest.mark.parametrize("subsystem,relative", [
    ("inbox", "inbox"),
    ("cohort ledger", "cohorts"),
    ("usage-limit park state", "usage-limit"),
    ("worktree registry", "worktrees.json"),
    ("role prompts", "role-prompts"),
])
def test_subsystem_stores_are_redirected(subsystem, relative):
    """~/.agentwire holds more than sessions/, and tests touch all of it."""
    import agentwire.cohort as cohort
    import agentwire.core as core
    import agentwire.inbox as inbox
    import agentwire.usage_limit as usage_limit

    for mod, attr in (
        (inbox, "INBOX_ROOT"), (cohort, "COHORT_ROOT"),
        (usage_limit, "STATE_DIR"), (core, "ROLE_PROMPTS_DIR"),
    ):
        value = getattr(mod, attr)
        assert REAL_HOME not in Path(value).parents, f"{attr} escapes to the real home"
