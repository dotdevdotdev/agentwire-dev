"""Upgrade test: a REAL older release, upgraded in place to this branch (#1276).

One 1.75 -> 1.78 upgrade produced three bugs at once (#1268 migrate ran without the AgentWire
profile and retired nothing, #1269 it deleted the only copy of a rule file the bundle no longer
ships, #1270 doctor's hints pointed at fixes that do nothing). Every one was invisible to unit tests
because they run one process against hand-built state. This is the "test a SECOND launch" lesson
applied to upgrades: a fresh venv gets the previous release from PyPI, that release writes its OWN
legacy state into a fresh HOME (copied rules, per-kind hook scripts, matcher registrations), a few
shapes are seeded on top, the venv is upgraded to the checkout, and the fresh-process commands a
human runs afterwards (`doctor`, `safety migrate`) are asserted on.

Marked ``slow`` AND ``upgrade``: it needs PyPI and builds two venvs, so the PR gate never runs it.
The `upgrade` job in .github/workflows/pytest.yml selects it on pushes to main and on PRs into a
release branch; the generic full run deselects it so it is not paid twice. Budget: under 3 minutes
on a hosted runner (two `uv pip install`s dominate). Pin the starting release with
``AGENTWIRE_UPGRADE_FROM`` (default: the oldest shape the migrate path must still handle).

Hermetic to the machine: every agentwire process runs with ``HOME`` pointed at a temp directory,
and the test asserts that nothing landed outside ``.agentwire`` / ``.claude`` there and that the
real ``~/.damage-control`` did not change.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from agentwire.history import encode_project_path

pytestmark = [pytest.mark.slow, pytest.mark.upgrade]

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upgrade"
FROM_VERSION = os.environ.get("AGENTWIRE_UPGRADE_FROM", "1.75.0")
REAL_DAMAGE_CONTROL = Path(os.path.expanduser("~")) / ".damage-control"

# A rule file the bundle shipped once and no longer does (#1143): pristine by hash, unbundled by name.
UNBUNDLED = "gws.yaml"

# Run in the upgraded venv, fresh process: where does each recorded session's history resolve?
_RESOLVE = textwrap.dedent(
    """
    import json
    from pathlib import Path
    from agentwire.core import load_session_metadata, recorded_sessions
    from agentwire.history import locate_conversation

    out = {}
    for name in recorded_sessions():
        meta = load_session_metadata(name)
        chain = meta.get("conversation_ids") or []
        locs = [locate_conversation(cid, meta["cwd_at_launch"]).status for cid in chain]
        out[name] = {"chain": chain, "statuses": locs}
    print(json.dumps(out))
    """
)

# `safety migrate` is tty-gated for a human; the function under it is what a human's run executes.
_MIGRATE = "from agentwire.safety_migrate import migrate; raise SystemExit(migrate(allow_foreign=True))"


def _tree(root: Path) -> dict[str, str]:
    """Every file under *root* -> content hash (symlinks by target), keyed relative to it."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = f"-> {os.readlink(path)}"
        elif path.is_file():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[rel] = "<dir>"
    return out


def _listing(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*")) if path.exists() else []


class Upgrade:
    """The scenario, run once: every phase's output is kept for the assertions."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.venv = root / "venv"
        self.project = (root / "project").resolve()
        self.home.mkdir()
        self.project.mkdir()
        self.env = {
            "PATH": f"{self.venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
        }
        self.out: dict[str, subprocess.CompletedProcess] = {}

    def run(self, label: str, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            list(cmd), env=self.env, cwd=self.project, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=300,
        )
        self.out[label] = proc
        if check and proc.returncode:
            raise AssertionError(f"{label} exited {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}")
        return proc

    def python(self, label: str, code: str) -> subprocess.CompletedProcess:
        return self.run(label, str(self.venv / "bin" / "python"), "-I", "-c", code)

    def install(self, label: str, *args: str) -> None:
        # uv resolves with the REAL environment (its cache lives in the real home); only agentwire runs under the fake HOME.
        subprocess.run(["uv", "pip", "install", "-q", "--python", str(self.venv / "bin" / "python"), *args],
                       check=True, timeout=600, capture_output=True, text=True)

    def seed(self) -> None:
        agentwire = self.home / ".agentwire"
        rules = agentwire / "damage-control"
        assert (rules / "core.yaml").is_file(), "the old release did not write its own legacy rule copies"
        shutil.copy(FIXTURES / UNBUNDLED, rules / UNBUNDLED)

        encoded = encode_project_path(str(self.project))
        projects = self.home / ".claude" / "projects" / encoded
        projects.mkdir(parents=True)
        # chain of three: the first transcript is gone (ordinary), the newest is real.
        chain = [str(uuid.uuid4()) for _ in range(3)]
        (projects / f"{chain[2]}.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
        )
        sessions = agentwire / "sessions"
        for name, ids in (("proj-feature", chain), ("solo", [chain[2]])):
            record = sessions / name
            record.mkdir(parents=True)
            (record / "metadata.json").write_text(json.dumps({
                "conversation_ids": ids, "cwd_at_launch": str(self.project), "role": "worker",
            }))

    def execute(self) -> None:
        self.run("venv", "uv", "venv", "-q", "--python", sys.executable, str(self.venv))
        self.install("old", f"agentwire-dev=={FROM_VERSION}")
        self.run("old-install", "agentwire", "safety", "install", "--yes")
        self.seed()
        self.seeded = _tree(self.home)

        self.install("new", str(REPO))
        self.before = _tree(self.home)
        self.run("dry-run", "agentwire", "safety", "migrate", "--dry-run")
        self.after_dry_run = _tree(self.home)
        self.run("doctor-before", "agentwire", "doctor", check=False)  # nonzero on a legacy machine, by design
        self.python("migrate", _MIGRATE)
        self.run("refresh", "agentwire", "safety", "install", "--refresh", "--yes")
        self.run("doctor-after", "agentwire", "doctor", check=False)  # env-dependent sections (portal, mic) may fail; asserted per section
        self.sessions = json.loads(self.python("resolve", _RESOLVE).stdout)


@pytest.fixture(scope="module")
def upgrade(tmp_path_factory) -> Upgrade:
    real_before = _listing(REAL_DAMAGE_CONTROL)
    scenario = Upgrade(tmp_path_factory.mktemp("upgrade"))
    scenario.execute()
    scenario.real_damage_control_untouched = _listing(REAL_DAMAGE_CONTROL) == real_before
    return scenario


def _section(doctor_output: str, heading: str) -> list[str]:
    """Lines of one doctor section (from its `Checking <heading>` line to the next)."""
    lines = doctor_output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Checking ") and heading in line)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("Checking ")), len(lines))
    return lines[start:end]


def test_old_release_really_wrote_legacy_state(upgrade):
    """Guards the scenario itself: without a real pre-package install the rest proves nothing."""
    legacy = ".agentwire/hooks/damage-control/bash-tool-damage-control.py"
    assert legacy in upgrade.seeded, sorted(upgrade.seeded)
    assert ".agentwire/damage-control/core.yaml" in upgrade.seeded


def test_dry_run_plans_the_migration_and_writes_nothing(upgrade):
    out = upgrade.out["dry-run"].stdout
    assert "would remove rule copy core.yaml" in out, out  # #1268: planned against ~/.agentwire, not ~/.damage-control
    assert f"keep rule {UNBUNDLED}" in out, out  # #1269: never offered for removal
    assert "would remove legacy hook script bash-tool-damage-control.py" in out, out
    changed = {k for k in upgrade.before.keys() | upgrade.after_dry_run.keys()
               if upgrade.before.get(k) != upgrade.after_dry_run.get(k)}
    assert not changed, sorted(changed)


def test_migrate_retires_the_redundant_and_keeps_what_the_bundle_lacks(upgrade):
    rules = upgrade.home / ".agentwire" / "damage-control"
    survivors = sorted(p.name for p in rules.glob("*.yaml"))
    assert survivors == [UNBUNDLED], survivors  # #1268 (retires at all) + #1269 (keeps the unbundled one)
    assert not list((upgrade.home / ".agentwire" / "tooldefs").glob("*.yaml"))
    assert not list((upgrade.home / ".agentwire" / "hooks" / "damage-control").glob("*-tool-damage-control.py"))
    assert "-tool-damage-control.py" not in (upgrade.home / ".claude" / "settings.json").read_text()


def test_nothing_written_outside_agentwire_and_claude(upgrade):
    top = {p.name for p in upgrade.home.iterdir()}
    assert top <= {".agentwire", ".claude", ".cache"}, sorted(top)  # .cache: tool caches, not ours  # #1268: the stray ~/.damage-control
    assert upgrade.real_damage_control_untouched


def test_every_recorded_session_still_resolves(upgrade):
    assert sorted(upgrade.sessions) == ["proj-feature", "solo"]
    bad = {name: rec for name, rec in upgrade.sessions.items() if "resumable" not in rec["statuses"]}
    assert not bad, bad  # the newest link of each chain resumes; nothing was moved or deleted by the upgrade
    assert upgrade.sessions["proj-feature"]["statuses"][-1] == "resumable"


def test_doctor_reports_the_legacy_state_before_and_nothing_after(upgrade):
    before = "\n".join(_section(upgrade.out["doctor-before"].stdout, "damage control"))
    assert "pre-package hook scripts" in before and "DUPLICATE rule ids" in before, before
    assert "Fix: agentwire safety migrate" in before, before  # #1270: the hint names the command that works
    for heading in ("damage control", "orphaned conversation history"):
        after = _section(upgrade.out["doctor-after"].stdout, heading)
        failing = [line for line in after if "[!!]" in line]
        assert not failing, failing
