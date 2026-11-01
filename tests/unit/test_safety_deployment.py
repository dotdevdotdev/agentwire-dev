"""Deployment correctness for the damage-control heal (#936 + #916).

``heal_damage_control`` is one function that failed in opposite directions:

* **hook scripts** — ``shutil.copy2`` on ANY difference, with no ordering, from
  ``Path(__file__).parent``. A session on a stale branch reinstalled a pre-fix
  security hook machine-wide, and ``copy2`` preserved the source mtime so the
  downgrade looked older than the deployment it replaced (#936).
* **rules / tooldefs** — ``if not target.exists()``, so an existing file was
  never updated by any command, ever. Every rule fix this repo ships was inert
  on every existing install (#916).

The acceptance bar these tests are written to: *a deployment claim must name the
path that EXECUTES*. So the end-to-end case drives the INSTALLED hook script as
a subprocess and reads the rule id out of the audit record that hook wrote —
not ``heal_damage_control`` returning a happy summary, which is the bug.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentwire import safety_commands
from agentwire.safety import provenance as prov
from agentwire.safety._core import load_config

BUNDLED_RULES = Path(safety_commands.__file__).parent / "hooks" / "damage-control" / "rules"
BUNDLED_TOOLDEFS = Path(safety_commands.__file__).parent / "tooldefs"
RUNNING_PKG = Path(safety_commands.__file__).parent


def _heal_with_cloud_hosting(all_packs=False):
    """Heal, then enable the cloud-hosting pack (or every pack): the tests use its file as the vehicle."""
    from agentwire.safety import packs
    from agentwire.safety.groups import REGISTRY
    safety_commands.heal_damage_control(quiet=True)
    on = [n for n, e in REGISTRY.items() if e.kind == "pack"] if all_packs else ["cloud-hosting"]
    packs.write_state(safety_commands.DAMAGECONTROL_FILE, packs=on)
    safety_commands.heal_damage_control(quiet=True)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A throwaway ~/.agentwire. Never the real one — that is the incident."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    # Pin provenance to CANONICAL by default so every heal test measures the
    # heal, not the guard — and so the suite behaves identically in a worktree
    # (where the package root's .git is a FILE) and in CI's plain clone.
    # Tests about the guard itself override this.
    #
    # Patched at the FUNCTION, not via an env var: there is deliberately no
    # environment override in the shipped code, because a leading `VAR=value`
    # assignment is masked by `masked_subcommands` and so cannot be seen by a
    # command-position damage-control rule (#946 review, F1).
    monkeypatch.setattr(prov, "canonical_package_dir", lambda: RUNNING_PKG.resolve())

    cfg = home / ".agentwire"
    monkeypatch.setattr(safety_commands, "CONFIG_DIR", cfg)
    monkeypatch.setattr(safety_commands, "HOOKS_DIR", cfg / "hooks" / "damage-control")
    monkeypatch.setattr(safety_commands, "LOGS_DIR", cfg / "logs" / "damage-control")
    monkeypatch.setattr(safety_commands, "RULES_DIR", cfg / "damage-control")
    monkeypatch.setattr(safety_commands, "TOOLDEFS_DIR", cfg / "tooldefs")
    monkeypatch.setattr(safety_commands, "DAMAGECONTROL_FILE", cfg / "damagecontrol.yml")
    return home


# ===========================================================================
# Provenance — WHICH checkout may write machine-global files (#936)
# ===========================================================================


class TestProvenance:


    def test_foreign_heal_refuses_and_writes_nothing(self, machine, monkeypatch, tmp_path, capsys):
        other = tmp_path / "installed" / "agentwire"
        other.mkdir(parents=True)
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: other.resolve())

        summary = safety_commands.heal_damage_control()
        out = capsys.readouterr().out

        assert summary["refused"] is True
        assert summary["provenance"] == prov.FOREIGN
        assert "REFUSED" in out
        # Nothing at all was written — not even the config dirs.
        assert not safety_commands.HOOKS_DIR.exists()
        assert not safety_commands.RULES_DIR.exists()


    def test_hooks_install_refuses_from_foreign_package(self, machine, monkeypatch, tmp_path):
        """`hooks install` SYMLINKS machine-global hooks at its own checkout."""
        from agentwire import hooks_cli

        other = tmp_path / "installed" / "agentwire"
        other.mkdir(parents=True)
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: other.resolve())

        results = hooks_cli.install_hooks()
        assert results
        assert set(results.values()) == {"refused-foreign"}
        assert not (Path.home() / ".claude" / "hooks").exists()

        # Mutation: same call, override on -> it installs.
        results = hooks_cli.install_hooks(allow_foreign=True)
        assert "refused-foreign" not in set(results.values())


# ===========================================================================
# Hook ordering — the installed copy can be NEWER than the package (#936)
# ===========================================================================


STAMP_BEGIN = "# === BEGIN AGENTWIRE HOOK STAMP (generated — do not edit) ==="
STAMP_END = "# === END AGENTWIRE HOOK STAMP ==="


def _restamp(text: str, generated_at: str) -> str:
    """Rewrite a hook's stamp date, leaving everything else byte-identical."""
    head, _, rest = text.partition(STAMP_BEGIN)
    body, _, tail = rest.partition(STAMP_END)
    stamp = json.loads(body.split("=", 1)[1].strip())
    stamp["generated_at"] = generated_at
    return (
        f"{head}{STAMP_BEGIN}\nAGENTWIRE_HOOK_STAMP = "
        f"{json.dumps(stamp, sort_keys=True)}\n{STAMP_END}{tail}"
    )


HOOK = "bash-tool-damage-control.py"


class TestHookOrdering:

    def test_installed_newer_is_reported_and_refused(self, machine, capsys):
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.HOOKS_DIR / HOOK
        future = _restamp(target.read_text(), "2099-01-01T00:00:00Z")
        target.write_text(future)

        assert safety_commands.damage_control_hook_drift()[HOOK] == "newer"

        summary = safety_commands.heal_damage_control()
        out = capsys.readouterr().out
        assert HOOK in summary["hooks_downgrade_refused"]
        assert HOOK not in summary["hooks_updated"]
        assert "REFUSED to downgrade" in out
        # The deployed guard is untouched — the whole point.
        assert target.read_text() == future


# ===========================================================================
# Three-way rule/tooldef sync (#916)
# ===========================================================================


@pytest.fixture
def baselines(monkeypatch, tmp_path):
    """A writable shipped-hash manifest for tests to register 'old releases' in."""
    path = tmp_path / "rule_baselines.json"
    data = {"rules": {}, "tooldefs": {}}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(safety_commands, "_BASELINES_PATH", path)

    def register(section: str, name: str, content: bytes):
        current = json.loads(path.read_text())
        current[section].setdefault(name, []).append(sha256(content))
        path.write_text(json.dumps(current))

    return register


OLD_RELEASE = "# an older shipped release\nbashToolPatterns: []\n"


class TestThreeWaySync:

    def test_mutation_unregistered_content_is_left_alone(self, machine, baselines):
        """Must-fail control: the SAME bytes, minus the baseline registration.

        If this also updated, the test above would be proving nothing about the
        three-way comparison.
        """
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.RULES_DIR / "core.yaml"
        target.write_text(OLD_RELEASE)
        # deliberately NOT registered as a shipped baseline

        assert safety_commands.rules_drift()["core.yaml"] == "unknown"
        summary = safety_commands.heal_damage_control(quiet=True)
        assert "core.yaml" not in summary["rules_updated"]
        assert "core.yaml" in summary["rules_unknown"]
        assert target.read_text() == OLD_RELEASE


    def test_force_replaces_unrecognized_and_keeps_a_backup(self, machine, baselines):
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.RULES_DIR / "core.yaml"
        target.write_text("# mine\n")

        summary = safety_commands.heal_damage_control(quiet=True, force=True)
        assert "core.yaml" in summary["rules_updated"]
        assert target.read_bytes() == (BUNDLED_RULES / "core.yaml").read_bytes()
        backups = list(safety_commands.RULES_DIR.glob("core.yaml.local-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text() == "# mine\n"


class TestShippedBaselineManifest:
    def test_current_bundled_content_is_registered(self):
        """CI guard: forget to regenerate and the NEXT release cannot heal.

        A bundled file whose current hash is absent from the manifest looks
        hand-edited to the version after it, so the heal would refuse exactly
        the update it exists to perform.
        """
        manifest = safety_commands.load_rule_baselines()
        for section, srcdir in (("rules", BUNDLED_RULES), ("tooldefs", BUNDLED_TOOLDEFS)):
            for src in sorted(srcdir.glob("*.yaml")):
                digest = sha256(src.read_bytes())
                assert digest in manifest[section].get(src.name, []), (
                    f"{section}/{src.name} is not in rule_baselines.json.\n"
                    f"Run: uv run python scripts/gen_rule_baselines.py"
                )


# ===========================================================================
# Duplicate rule ids — the only detector for a half-applied sync (#916)
# ===========================================================================


# ===========================================================================
# Doctor — direction, not "differs" (#916 + #936)
# ===========================================================================


def _render_doctor():
    from agentwire.doctor_cli import _render_damage_control_section
    return _render_damage_control_section()


class TestDoctorDirection:
    @pytest.fixture(autouse=True)
    def _enabled(self, machine, baselines):
        _heal_with_cloud_hosting()
        safety_commands.DAMAGECONTROL_FILE.write_text("enabled: true\npacks: [cloud-hosting]\n")


    def test_removed_protections_are_named(self, capsys):
        """"Differ from bundled" cannot tell a tweak from a disabled deploy tier."""
        bundled = (BUNDLED_RULES / "cloud-hosting.yaml").read_text()
        stripped = bundled.replace(r"    pattern: '\bnetlify\s+sites:delete\b'", "    pattern: 'ZZZ-NEVER'")
        assert stripped != bundled, "mutation did not change the corpus"
        (safety_commands.RULES_DIR / "cloud-hosting.yaml").write_text(stripped)

        issues = _render_doctor()
        out = capsys.readouterr().out
        assert "MISSING bundled protections" in out
        assert r"\bnetlify\s+sites:delete\b" in out
        assert issues >= 1


# ===========================================================================
# The acceptance that matters: drive the file Claude Code EXECUTES
# ===========================================================================


# `netlify sites:delete` is a hard block in the bundled cloud-hosting rules and
# — verified below with no filter — is caught by NOTHING else, so its absence is
# observable rather than masked.
NETLIFY_CMD = "netlify sites:delete my-production-site"
NETLIFY_RULE_ID = "cloud-hosting.netlify-sites-delete-deletes-entire-site"


def _run_installed_hook(machine_home: Path, command: str):
    """Execute the INSTALLED hook script, as Claude Code does.

    Not ``check_command`` in-process, and not the packaged source: the claim
    under test is about the bytes at ``~/.agentwire/hooks/damage-control/``, so
    the test drives exactly those, with ``AGENTWIRE_DIR`` pointed at the
    throwaway machine.
    """
    hook = safety_commands.HOOKS_DIR / HOOK
    env = dict(os.environ)
    env["AGENTWIRE_DIR"] = str(machine_home / ".agentwire")
    env.pop("AGENTWIRE_UNATTENDED", None)
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(machine_home),
    })
    return subprocess.run(
        [sys.executable, str(hook)],
        input=payload, capture_output=True, text=True, env=env,
    )


def _audit_rule_ids(machine_home: Path) -> list:
    log_dir = machine_home / ".agentwire" / "logs" / "damage-control"
    ids = []
    for f in sorted(log_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                ids.append(json.loads(line).get("rule_id"))
            except ValueError:
                pass
    return ids


class TestInstalledPathBehaviour:
    def test_stale_rule_deploys_and_the_installed_hook_blocks_by_rule_id(
        self, machine, baselines
    ):
        """End to end, in the order the incident happened.

        1. machine carries a previously shipped cloud-hosting.yaml missing the
           netlify guard,
        2. the installed hook does NOT block it — verified with no filter, so a
           negative here proves it looked,
        3. heal,
        4. the SAME installed file now blocks, by the expected rule id, read out
           of the audit record that hook itself wrote.
        """
        _heal_with_cloud_hosting(all_packs=True)

        bundled = (BUNDLED_RULES / "cloud-hosting.yaml").read_text()
        stale = bundled.replace(
            r"    pattern: '\bnetlify\s+sites:delete\b'", "    pattern: 'ZZZ-NEVER'"
        )
        assert stale != bundled, "mutation did not change the corpus"
        target = safety_commands.RULES_DIR / "cloud-hosting.yaml"
        target.write_text(stale)
        baselines("rules", "cloud-hosting.yaml", stale.encode())

        # (2) The negative, taken against the FULL live rule set — every rule
        # file, bundled tooldefs, no filtering — so "not blocked" cannot be an
        # artifact of a narrowed corpus.
        live = load_config(safety_commands.RULES_DIR, safety_commands.TOOLDEFS_DIR)
        # floor lowered 260 -> 255 when #924 deleted 8 redundant remote.yaml
        # ssh twins (net -7 rules)
        assert len(live["bashToolPatterns"]) >= 248, "live corpus is suspiciously small"
        before_decision = __import__(
            "agentwire.safety._core", fromlist=["check_command"]
        ).check_command(NETLIFY_CMD, live)
        assert before_decision["decision"] != "block", before_decision

        before = _run_installed_hook(machine, NETLIFY_CMD)
        assert before.returncode == 0, before.stderr
        assert "SECURITY: Blocked" not in before.stderr

        # (3)
        summary = safety_commands.heal_damage_control(quiet=True)
        assert "cloud-hosting.yaml" in summary["rules_updated"]

        # (4) Same executable path, now protected.
        after = _run_installed_hook(machine, NETLIFY_CMD)
        assert after.returncode == 2, (after.returncode, after.stdout, after.stderr)
        assert "SECURITY: Blocked" in after.stderr
        assert NETLIFY_RULE_ID in _audit_rule_ids(machine)


    def _neuter(self, text: str) -> str:
        """Make a hook that ALLOWS everything, preserving its stamp block.

        Stands in for "a hook predating a security fix": the difference has to
        be observable by RUNNING it, or the test measures bytes rather than
        protection.
        """
        neutered = text.replace(
            "    result = check_command(command, config, cwd=input_data.get(\"cwd\"))",
            "    result = {'decision': 'allow', 'reason': 'MUTANT'}",
            1,
        )
        assert neutered != text, "neutering did not apply — corpus unchanged"
        return neutered

    def test_older_hook_is_replaced_and_protection_comes_back(self, machine):
        """The #936 half, end to end, driven through the executing file.

        The rules acceptance above proves rule DEPLOYMENT. This proves HOOK
        SCRIPT deployment, which is the half that actually went backwards.
        """
        _heal_with_cloud_hosting()
        target = safety_commands.HOOKS_DIR / HOOK

        # A hook that predates the guard: allows everything, stamped older.
        target.write_text(_restamp(self._neuter(target.read_text()),
                                   "2000-01-01T00:00:00Z"))
        assert safety_commands.damage_control_hook_drift()[HOOK] == "older"
        before = _run_installed_hook(machine, NETLIFY_CMD)
        assert before.returncode == 0, "the neutered hook should allow — else this proves nothing"

        summary = safety_commands.heal_damage_control(quiet=True)
        assert HOOK in summary["hooks_updated"]

        after = _run_installed_hook(machine, NETLIFY_CMD)
        assert after.returncode == 2, (after.returncode, after.stderr)
        assert NETLIFY_RULE_ID in _audit_rule_ids(machine)

