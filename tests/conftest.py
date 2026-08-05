"""Shared test fixtures for the AgentWire test suite."""

import os
import sys
from pathlib import Path

import pytest
import yaml

from tests import home_guard

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: The owner's real config directory. Nothing in the suite may write here.
#: Re-exported from the single owner so both halves agree on what "real" is.
REAL_AGENTWIRE_HOME = home_guard.REAL_AGENTWIRE_HOME

home_guard.install()


def _agentwire_modules():
    """Loaded agentwire modules, snapshotted (import can mutate sys.modules)."""
    return [m for name, m in list(sys.modules.items())
            if name == "agentwire" or name.startswith("agentwire.")
            if m is not None]


def _import_every_agentwire_module():
    """Import the whole package once, BEFORE any test redirects ``$HOME``.

    Load-bearing, and subtle. The redirect below works by rebinding
    module-level constants that were computed at import time — but it can only
    rebind modules that are *already imported*. Much of this codebase imports
    lazily inside functions (``from agentwire.__main__ import
    build_agent_command`` inside a test helper), so without this the first
    test to trigger such an import does it while ``$HOME`` already points at
    that test's tmp directory. The module then computes
    ``CONFIG_DIR = Path.home() / ".agentwire"`` against the *fake* home and
    freezes it there — permanently, for the rest of the session, because
    monkeypatch never patched it and so has nothing to restore.

    The symptom is a constant stuck at some early test's tmp path, which is
    exactly the kind of cross-test bleed this fixture exists to prevent. Doing
    the imports up front means every constant is computed against the real
    home, so the per-test walk both rebinds and restores it.

    Best-effort: a submodule that cannot import (optional dependency, platform
    guard) is skipped rather than failing collection.
    """
    import importlib
    import pkgutil

    import agentwire

    # Importing the package touches the real config dir: `agentwire_dir()`
    # both resolves AND mkdirs, and some modules call it at import. That is the
    # package's own import-time behaviour, not a test polluting the store, and
    # attributing it to whichever test happened to be first would be a lie. It
    # is sanctioned rather than silenced, so it still shows up in
    # SANCTIONED_WRITES if anyone wants to look.
    with home_guard.sanctioned_real_home_write():
        for info in pkgutil.walk_packages(agentwire.__path__, prefix="agentwire."):
            try:
                importlib.import_module(info.name)
            except Exception:
                continue


_import_every_agentwire_module()


@pytest.fixture(autouse=True)
def _isolate_agentwire_home(request, tmp_path_factory, monkeypatch):
    """No test may read or write the real ``~/.agentwire`` — ever (#893).

    Found the hard way: ``~/.agentwire/sessions/resumed/metadata.json`` was a
    live record in the owner's config directory, written by this suite and
    grown to 80 fabricated conversation ids — one appended per full-suite run,
    because ``conversation_ids`` is a chain by design (#871). Beyond tests
    mutating real user state being a defect on its own, it corrupted a
    measurement: sizing #871's orphaned-history doctor check against the real
    store surfaced 28 recorded ids with no transcript, *all* from that one
    record, with zero genuine orphans behind them.

    Two levers, because there are two ways a path gets computed:

    1. **``$HOME``** — ``Path.home()`` resolves through ``expanduser``, which
       reads the variable, so redirecting it catches everything computed at
       *call* time, including modules imported later by a lazy import.
    2. **A walk over loaded modules** — roughly forty constants across ~25
       modules are computed at *import* time
       (``COHORT_ROOT = Path.home() / ".agentwire" / "cohorts"`` and friends)
       and are already frozen before any fixture runs. Rebinding them by
       walking beats enumerating them: a hand-written list would rot the first
       time someone adds a constant, which is exactly how this class of bug
       recurs. ``test_home_isolation.py`` asserts the walk missed nothing.

    Per-test rather than per-session, so no test can observe another's writes.
    Tests that need their own location re-patch the same attributes via
    ``monkeypatch``, which overrides this.
    """
    # Escape hatch for the rare test that must see the REAL deployment paths
    # to assert on them — e.g. "role prompts are not in a directory macOS
    # garbage-collects", which this fixture would otherwise make vacuous by
    # relocating them into exactly such a directory. Read-only by intent, and
    # not a hole: the session-scoped backstop below still fails the run if an
    # opted-out test writes anything.
    if request.node.get_closest_marker("real_agentwire_home"):
        return REAL_AGENTWIRE_HOME

    fake_home = tmp_path_factory.mktemp("home")
    fake_config = fake_home / ".agentwire"
    fake_config.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AGENTWIRE_HOME", str(fake_config))

    for module in _agentwire_modules():
        for attr, value in list(vars(module).items()):
            if not isinstance(value, Path):
                continue
            if value == REAL_AGENTWIRE_HOME:
                monkeypatch.setattr(module, attr, fake_config, raising=False)
            elif REAL_AGENTWIRE_HOME in value.parents:
                relocated = fake_config / value.relative_to(REAL_AGENTWIRE_HOME)
                monkeypatch.setattr(module, attr, relocated, raising=False)

    # ``agentwire_dir()`` both resolves AND mkdirs, and callers bound it with
    # ``from .utils.paths import agentwire_dir`` — a per-module copy that
    # patching the definition site would not reach.
    def _fake_agentwire_dir() -> Path:
        fake_config.mkdir(parents=True, exist_ok=True)
        return fake_config

    for module in _agentwire_modules():
        if callable(vars(module).get("agentwire_dir")):
            monkeypatch.setattr(module, "agentwire_dir", _fake_agentwire_dir, raising=False)

    return fake_config


@pytest.fixture(scope="session", autouse=True)
def _real_agentwire_home_untouched():
    """Backstop: fail the run loudly if anything escaped the redirect (#893).

    The redirect above is prevention; this is detection. Implementation lives
    in :mod:`tests.home_guard` so there is exactly one recorder — see the note
    there about pytest loading this file under two module names.
    """
    yield
    failure = home_guard.report()
    if failure:
        pytest.fail(failure, pytrace=False)


@pytest.fixture(autouse=True)
def _no_real_outbound_email(request, monkeypatch):
    """No test may send real email — ever.

    ``_escalate_dead_letters`` (and friends) call the live Resend wiring, so a
    test that dead-letters a done/request/escalation message without mocking
    ``send_email`` silently emails the owner on every suite run (found the hard
    way: ``test_purge_leaves_ingest_and_dead`` flooded the inbox with
    "undelivered done: x → s" from its fixture names). Tests that assert on
    email re-patch the same target via ``monkeypatch``, which overrides this.

    ``test_channels.py`` is exempt: it tests ``send_email`` itself (against a
    mocked Resend transport), so stubbing the function would test the stub.
    """
    if request.node.fspath.basename == "test_channels.py":
        return
    from types import SimpleNamespace

    monkeypatch.setattr(
        "agentwire.channels.email.send_email",
        lambda **kw: SimpleNamespace(success=True, id="test-stub"),
    )


@pytest.fixture(autouse=True)
def _no_live_portal_stt_query(monkeypatch):
    """Keep tests off the RUNNING portal's /api/voice-status.

    #683 made ``resolve_stt_status`` prefer the live portal's effective STT
    backend over the file config — correct in production, but it makes any
    status test environment-dependent (a portal running ``--no-stt`` on the
    dev box flips every configured-backend assertion to the ``none`` tier;
    test_doctor_voice broke exactly this way). Tests that exercise the live
    override re-patch this attribute themselves.
    """
    import agentwire.voice_status as vs

    monkeypatch.setattr(vs, "_portal_effective_stt_backend", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_role_prompt_sweep(monkeypatch):
    """No test may sweep the REAL role-prompt store — ever.

    ``role_prompts.tick`` is a watchdog stage, so anything that exercises
    ``agentwire limits tick`` end to end reaches it, and it is the one function
    that resolves ``~/.agentwire/role-prompts/`` for a DELETION pass. Those
    files are live agents' system prompts: deleting one strips the role from a
    running session, which is exactly the failure #881 fixed.

    Tests for the retention rule itself call ``role_prompts.sweep`` /
    ``status`` with their own fixture directories (both are required
    parameters, precisely so they can't default to the real one).
    """
    from agentwire import role_prompts

    monkeypatch.setattr(
        role_prompts, "tick",
        lambda: {"skipped": "disabled-in-tests", "deleted": []})


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Temporary ~/.agentwire/ equivalent."""
    config_dir = tmp_path / ".agentwire"
    config_dir.mkdir()
    (config_dir / "locks").mkdir()
    (config_dir / "logs").mkdir()
    return config_dir


@pytest.fixture
def minimal_config_yaml():
    """Minimal valid config dict."""
    return {
        "server": {"host": "0.0.0.0", "port": 8765},
        "projects": {"dir": "~/projects"},
        "tts": {"backend": "default"},
    }


@pytest.fixture
def config_file(tmp_config_dir, minimal_config_yaml):
    """Write a config.yaml and return its path."""
    config_path = tmp_config_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(minimal_config_yaml, f)
    return config_path


@pytest.fixture
def project_dir(tmp_path):
    """Temporary project directory."""
    proj = tmp_path / "test-project"
    proj.mkdir()
    return proj


@pytest.fixture
def project_config_file(project_dir):
    """Write a .agentwire.yml and return its path."""
    config_path = project_dir / ".agentwire.yml"
    data = {
        "posture": "bypass",
        "roles": ["agentwire", "voice"],
        "voice": "default",
        "parent": "main",
    }
    with open(config_path, "w") as f:
        yaml.safe_dump(data, f)
    return config_path


@pytest.fixture
def scheduler_board_file(tmp_config_dir):
    """Write a scheduler.yaml with 3 test tasks, return path."""
    board_path = tmp_config_dir / "scheduler.yaml"
    import shutil
    shutil.copy(FIXTURES_DIR / "sample_scheduler.yaml", board_path)
    return board_path


@pytest.fixture(autouse=True)
def isolated_device_registry(tmp_path, monkeypatch):
    """Point the device registry + pairings at a temp dir for every test.

    Keeps the suite from reading/writing the developer's real
    ~/.agentwire/devices.json, and clears the mtime cache between tests.
    """
    from agentwire import devices

    monkeypatch.setattr(devices, "DEVICES_FILE", tmp_path / "aw-devices.json")
    monkeypatch.setattr(devices, "PAIRINGS_FILE", tmp_path / "aw-pairings.json")
    devices._cache.clear()
    yield
    devices._cache.clear()


@pytest.fixture(autouse=True)
def isolated_cohort_ledgers(tmp_path, monkeypatch):
    """Point the fan-out cohort ledger (#852) at a temp dir for every test.

    ``cmd_new`` enrolls every spawn in the CALLER's cohort, and the caller is
    resolved from the live tmux session — so without this, any test exercising
    ``cmd_new`` writes a real ledger for whatever session is running the suite,
    which would then suppress that session's own idle handling.
    """
    from agentwire import cohort

    monkeypatch.setattr(cohort, "COHORT_ROOT", tmp_path / "aw-cohorts")
    monkeypatch.setattr(cohort, "EVENTS_FILE", tmp_path / "aw-cohort-events.jsonl")


@pytest.fixture
def clean_env(monkeypatch):
    """Remove all AGENTWIRE_* env vars."""
    for key in list(os.environ):
        if key.startswith("AGENTWIRE_"):
            monkeypatch.delenv(key)
