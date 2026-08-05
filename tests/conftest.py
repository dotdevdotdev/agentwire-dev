"""Shared test fixtures for the AgentWire test suite."""

import os
import sys
from pathlib import Path

import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: The owner's real config directory. Nothing in the suite may write here.
REAL_AGENTWIRE_HOME = Path.home() / ".agentwire"


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


#: Populated by the audit hook below: (test id, path) for every write that
#: escaped the redirect. Module-level so the hook, which cannot be removed
#: once installed, stays a pure recorder.
_REAL_HOME_WRITES: list = []
_CURRENT_TEST: list = [None]

#: Audit events that create, modify or delete a path. ``open`` is checked for
#: a writing mode; the rest are unconditional.
_WRITE_EVENTS = {
    "os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.link",
    "os.symlink", "os.truncate", "os.chmod", "shutil.copyfile", "shutil.move",
}


def _install_real_home_audit_hook():
    """Record any in-process write under the real ~/.agentwire.

    Deliberately an audit hook rather than a before/after filesystem
    snapshot. A snapshot cannot tell *this process* from the rest of the
    machine, and on the owner's box ~/.agentwire is written continuously by
    the live system — the watchdog, the message inbox, damage-control logs. A
    snapshot-based guard flagged `logs/damage-control/<today>.jsonl` on its
    first run, which was an agent's shell command in another process, not the
    suite. A guard that cries wolf gets switched off, and then it is not a
    guard.

    An audit hook sees only this interpreter, so concurrent activity is
    invisible to it, and it fires on the syscall — which means it names the
    test that did it instead of reporting that *something*, *somewhere* in the
    session escaped.
    """
    import sys

    real = str(REAL_AGENTWIRE_HOME)

    def hook(event, args):
        if event == "open":
            if len(args) < 2 or not args[1] or not any(c in str(args[1]) for c in "wax+"):
                return
            target = args[0]
        elif event in _WRITE_EVENTS:
            target = args[0] if args else None
        else:
            return
        try:
            path = str(target)
        except Exception:
            return
        if path.startswith(real):
            _REAL_HOME_WRITES.append((_CURRENT_TEST[0], event, path))

    sys.addaudithook(hook)


_install_real_home_audit_hook()


@pytest.fixture(autouse=True)
def _track_current_test(request):
    """Name the test currently running, so a violation can be attributed."""
    _CURRENT_TEST[0] = request.node.nodeid
    yield
    _CURRENT_TEST[0] = None


@pytest.fixture(scope="session", autouse=True)
def _real_agentwire_home_untouched():
    """Backstop: fail the run loudly if anything escaped the redirect (#893).

    The redirect above is prevention; this is detection, and it exists because
    the failure it guards against went unnoticed long enough to accumulate 80
    fabricated conversation ids.
    """
    yield
    if not _REAL_HOME_WRITES:
        return
    seen, lines = set(), []
    for test_id, event, path in _REAL_HOME_WRITES:
        key = (test_id, path)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {test_id or '<session>'}\n      {event}  {path}")
    pytest.fail(
        f"the test suite wrote into the REAL ~/.agentwire ({len(seen)} write(s), #893)\n"
        + "\n".join(lines[:25]),
        pytrace=False,
    )


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
