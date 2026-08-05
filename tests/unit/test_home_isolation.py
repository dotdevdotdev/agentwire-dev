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
        from tests import home_guard

        before = len(home_guard.WRITES)
        agent = core.AgentCommand(
            command="claude", posture="bypass", roles=["orchestrator"],
            conversation_id="00000000-0000-4000-8000-000000000000",
            role_prompt_path=None,
        )
        core.record_session_launch("resumed", agent, Path.cwd(), role="orchestrator")

        # Asserted against the audit hook, NOT a before/after snapshot of the
        # real home. This module used to snapshot, and it flaked for precisely
        # the reason this PR argues the guard must be in-process: ~/.agentwire
        # is written continuously by the live fleet, so a snapshot cannot tell
        # this process from the rest of the machine. Sampling it three seconds
        # apart on an idle box still showed entries changing. The argument and
        # the implementation now agree.
        assert home_guard.WRITES[before:] == []

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


@pytest.mark.real_agentwire_home
class TestTheAuditHookCanActuallyFail:
    """The backstop must be provably capable of catching each write primitive.

    This exists because the hook shipped with a hole that no test could see.
    The ``open`` audit event is ``(path, mode, flags)``: ``mode`` is the string
    only for ``builtins.open``/``io.open``, while the low-level ``os.open``
    passes ``mode=None`` and carries the intent in ``flags``. The first version
    checked only ``mode``, so it returned before recording — blind to
    ``os.open`` entirely.

    That is not a corner case. ``os.open`` is how seven production sites create
    files under the config dir, including ``core.write_role_prompt``, which
    uses it precisely so the prompt is never briefly world-readable. One miss
    in the mode check silently disabled detection for all of them.

    The earlier "can it fail" test exercised a ``_snapshot()`` helper local to
    this module — not the hook — so the actual backstop was untested (that
    helper is gone; it had the flakiness this guard exists to avoid). These
    write for real, under the real home, and clean up after themselves; each
    asserts the hook recorded it.
    """

    def _probe(self, name):
        from tests import home_guard

        return home_guard.REAL_AGENTWIRE_HOME / name

    def _recorded_since(self, mark):
        from tests import home_guard

        return home_guard.SANCTIONED_WRITES[mark:]

    def _mark(self):
        from tests import home_guard

        return len(home_guard.SANCTIONED_WRITES)

    @pytest.fixture(autouse=True)
    def _sanctioned(self):
        """These probes write for real; the sanction keeps the backstop from
        failing the run over writes it is being asked to detect. They are still
        recorded — into SANCTIONED_WRITES — so the assertions are real."""
        from tests import home_guard

        with home_guard.sanctioned_real_home_write():
            yield

    def test_catches_builtin_open_for_write(self):
        target, mark = self._probe("zz-probe-open.json"), self._mark()
        try:
            with open(target, "w") as fh:
                fh.write("{}")
        finally:
            target.unlink(missing_ok=True)
        assert any(str(target) == p for _t, _e, p in self._recorded_since(mark))

    def test_catches_os_open_which_reports_no_mode(self):
        """The exact hole: mode is None, intent lives in flags."""
        import os

        target, mark = self._probe("zz-probe-osopen.json"), self._mark()
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
        finally:
            target.unlink(missing_ok=True)
        assert any(str(target) == p for _t, _e, p in self._recorded_since(mark)), (
            "os.open escaped the guard — this is the hole that shipped"
        )

    def test_catches_write_role_prompt_shaped_writes(self):
        """What core.write_role_prompt actually does, end to end."""
        import os

        d, mark = self._probe("zz-probe-prompts"), self._mark()
        try:
            d.mkdir(parents=True, exist_ok=True)
            f = d / "conv.txt"
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("role prompt")
        finally:
            (d / "conv.txt").unlink(missing_ok=True)
            if d.exists():
                d.rmdir()
        assert any(str(d) in p for _t, _e, p in self._recorded_since(mark))

    def test_catches_path_write_text(self):
        target, mark = self._probe("zz-probe-writetext.json"), self._mark()
        try:
            target.write_text("{}")
        finally:
            target.unlink(missing_ok=True)
        assert any(str(target) == p for _t, _e, p in self._recorded_since(mark))

    def test_catches_mkdir(self):
        target, mark = self._probe("zz-probe-dir"), self._mark()
        try:
            target.mkdir()
        finally:
            if target.exists():
                target.rmdir()
        assert any(str(target) == p for _t, _e, p in self._recorded_since(mark))

    def test_catches_unlink(self):
        target, mark = self._probe("zz-probe-unlink.json"), self._mark()
        target.write_text("{}")
        mark = self._mark()
        target.unlink()
        assert any(str(target) == p for _t, _e, p in self._recorded_since(mark))

    def test_catches_atomic_write_via_os_replace(self):
        """``_atomic_write`` publishes with a rename; the temp file is os.open'd."""
        from agentwire import core

        target, mark = self._probe("zz-probe-atomic.json"), self._mark()
        try:
            core._atomic_write(target, "{}")
        finally:
            target.unlink(missing_ok=True)
        assert any(str(target) in p for _t, _e, p in self._recorded_since(mark))

    def test_ignores_reads(self):
        """A guard that flags reads would flood and get switched off."""
        from tests import home_guard

        target = self._probe("zz-probe-read.json")
        target.write_text("{}")
        mark = self._mark()
        target.read_text()
        assert not self._recorded_since(mark), "a plain read was recorded as a write"
        target.unlink()
        assert home_guard.REAL_AGENTWIRE_HOME.exists()

    def test_ignores_writes_outside_the_real_home(self, tmp_path):
        mark = self._mark()
        (tmp_path / "elsewhere.json").write_text("{}")
        assert not self._recorded_since(mark)


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
        (inbox, "INBOX_ROOT"), (cohort, "COHORT_ROOT"), (usage_limit, "STATE_DIR"),
    ):
        value = getattr(mod, attr)
        assert REAL_HOME not in Path(value).parents, f"{attr} escapes to the real home"

    # The role-prompt store is resolved LAZILY (#902 made it a function rather
    # than an import-time constant, to dodge the same frozen-binding trap).
    # A function is invisible to a walk over Path attributes, so this is the
    # check that the redirect still reaches it — via CONFIG_DIR, at call time.
    assert REAL_HOME not in core.role_prompts_dir().parents
    assert REAL_HOME != core.role_prompts_dir()


class TestComposesWithTheRolePromptSweepStub:
    """Both conftest guards must hold at once (#893 + #884/#902).

    They cover different halves and neither subsumes the other: #902's stub
    PREVENTS one specific destructive operation (the role-prompt sweep's
    deletion pass) from ever addressing the real store, while this PR's guard
    DETECTS any test that writes there at all. Keeping only one would silently
    drop a protection — and the sweep's is the kind nobody notices is gone
    until something has already been deleted.

    They also reinforce each other. The eager package import means
    ``role_prompts`` is loaded before any redirect, so its paths resolve
    through a patched ``CONFIG_DIR``; and the stub holds even where a redirect
    might not reach.
    """

    def test_tick_is_stubbed_and_deletes_nothing(self):
        from agentwire import role_prompts

        result = role_prompts.tick()
        assert result["deleted"] == []
        assert result.get("skipped") == "disabled-in-tests"

    def test_the_sweep_stub_does_not_trip_the_write_guard(self):
        """A no-op stub must not look like a write to the real store."""
        from agentwire import role_prompts
        from tests import home_guard

        before = len(home_guard.WRITES)
        role_prompts.tick()
        assert len(home_guard.WRITES) == before

    def test_no_sweep_stamp_is_written_to_the_real_store(self):
        """#902's reviewer's check: the stub held.

        ``tick`` writes ``role-prompt-sweep.json`` next to the store when it
        actually runs, so the stamp's absence from the REAL config dir is the
        observable proof that no test swept it.
        """
        from agentwire import role_prompts
        from tests import home_guard

        role_prompts.tick()
        stamp = home_guard.REAL_AGENTWIRE_HOME / "role-prompt-sweep.json"
        assert not any(
            str(stamp) == path for _t, _e, path in home_guard.WRITES
        ), "a test wrote the sweep stamp into the real store"

    def test_role_prompt_paths_resolve_inside_the_redirect(self, _isolate_agentwire_home):
        """Where the sweep WOULD look is inside this test's tmp home."""
        from agentwire import core

        assert core.role_prompts_dir() == _isolate_agentwire_home / "role-prompts"
