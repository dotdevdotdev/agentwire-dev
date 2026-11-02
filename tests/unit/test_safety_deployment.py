"""Deployment correctness for the damage-control heal (#936, #916, reshaped by #1209 M4).

Two incidents shaped this file, and both survive the package move as lessons about WHICH
PATH EXECUTES:

* **provenance** -- a checkout that is not the installed tool must never write machine-global
  files. A worktree's venv is deleted with the worktree, and ``onFailure: block`` then refuses
  every tool call, so the guard now covers the registration and the interpreter it records.
* **stale copies** -- before the package, heal COPIED the rules into ``~/.agentwire`` and the
  hooks read the copies, so every rule fix shipped was inert on every existing install. The
  package carries the rules now, but a leftover copy still REPLACES the bundled file of the
  same name by design (it is the host's overlay), so retiring stale copies is what keeps a
  fix reaching the machine. A deployment claim must name the path that EXECUTES: the end-to-end
  case drives the registered hook command as a subprocess and reads the rule id out of the
  audit row it wrote, not ``heal_damage_control`` returning a happy summary.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentwire import safety_commands
from agentwire.safety import host_profile
from agentwire.safety import provenance as prov
from damage_control import engine, packs
from tests.unit.test_safety_heal import fake_env as machine  # noqa: F401  (fixture)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


BUNDLED_RULES = engine.bundled_rules_dir()
BUNDLED_TOOLDEFS = engine.bundled_tooldefs_dir()


# ===========================================================================
# Provenance -- WHICH checkout may write machine-global files (#936)
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
        # Nothing at all was written: no registration, no interpreter record, no profile, no break-glass.
        assert not (machine / ".claude").exists()
        assert not safety_commands.INSTALL_RECORD.exists()
        assert not safety_commands.BREAKGLASS_DIR.exists()
        assert not host_profile.manifest_path().exists()
        assert not safety_commands.RULES_DIR.exists()

    def test_hooks_install_refuses_from_foreign_package(self, machine, monkeypatch, tmp_path):
        """`hooks install` links machine-global hooks at its own checkout."""
        from agentwire import hooks_cli

        other = tmp_path / "installed" / "agentwire"
        other.mkdir(parents=True)
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: other.resolve())

        results = hooks_cli.install_hooks()
        assert results
        assert set(results.values()) == {"refused-foreign"}
        assert not (Path.home() / ".claude" / "hooks").exists()
        assert not (Path.home() / ".claude" / "settings.json").exists()

        # Mutation: same call, override on -> it installs.
        results = hooks_cli.install_hooks(allow_foreign=True)
        assert "refused-foreign" not in set(results.values())


# ===========================================================================
# The shipped-hash manifest decides which overlay copies are stale (#916)
# ===========================================================================


@pytest.fixture
def baselines(monkeypatch, tmp_path):
    """A writable shipped-hash manifest for tests to register 'old releases' in."""
    path = tmp_path / "rule_baselines.json"
    path.write_text(json.dumps({"rules": {}, "tooldefs": {}}))
    monkeypatch.setattr(safety_commands, "_BASELINES_PATH", path)

    def register(section: str, name: str, content: bytes):
        current = json.loads(path.read_text())
        current[section].setdefault(name, []).append(sha256(content))
        path.write_text(json.dumps(current))

    return register


class TestShippedBaselineManifest:
    def test_current_bundled_content_is_registered(self):
        """CI guard: forget to regenerate and the NEXT release cannot retire this one's copies.

        A bundled file whose current hash is absent from the manifest looks hand-edited to the
        version after it, so a stale copy of it would be kept as 'yours' forever.
        """
        manifest = safety_commands.load_rule_baselines()
        missing = []
        for section, dirs in (("rules", (BUNDLED_RULES, host_profile.RULES_DIR)), ("tooldefs", (BUNDLED_TOOLDEFS,))):
            for srcdir in dirs:
                for src in sorted(srcdir.glob("*.yaml")):
                    if sha256(src.read_bytes()) not in manifest[section].get(src.name, []):
                        missing.append(f"{section}/{src.name}")
        assert not missing, f"not in rule_baselines.json (run: uv run python scripts/gen_rule_baselines.py): {missing}"


# ===========================================================================
# Stale overlay copies -- retire the pristine ones, keep (and name) the edited ones
# ===========================================================================

OLD_RELEASE = "# an older shipped release\nbashToolPatterns: []\n"


class TestRetiringStaleCopies:
    def test_a_registered_old_release_is_retired(self, machine, baselines):
        target = safety_commands.RULES_DIR / "core.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(OLD_RELEASE)
        baselines("rules", "core.yaml", OLD_RELEASE.encode())

        summary = safety_commands.heal_damage_control(quiet=True)

        assert "core.yaml" in summary["rules_retired"] and not target.exists()

    def test_mutation_unregistered_content_is_kept_and_reported(self, machine, baselines):
        """Must-fail control: the SAME bytes, minus the baseline registration.

        If this also retired, the test above would prove nothing about the hash comparison.
        """
        target = safety_commands.RULES_DIR / "core.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(OLD_RELEASE)  # deliberately NOT registered as a shipped baseline
        baselines("rules", "core.yaml", b"# some other release\n")  # the NAME is known; these bytes are not

        summary = safety_commands.heal_damage_control(quiet=True)

        assert "core.yaml" in summary["rules_kept_edited"]
        assert target.read_text() == OLD_RELEASE
        assert safety_commands.shadowed_bundled_files()["rules"] == ["core"]


# ===========================================================================
# Doctor -- names what a kept overlay replaces
# ===========================================================================


class TestDoctorNamesShadowing:
    def test_an_edited_overlay_that_replaces_a_bundled_file_is_named(self, machine, capsys):
        from agentwire.doctor_cli import _render_damage_control_section

        safety_commands.heal_damage_control(quiet=True)
        (safety_commands.RULES_DIR).mkdir(parents=True, exist_ok=True)
        (safety_commands.RULES_DIR / "cloud-hosting.yaml").write_text(OLD_RELEASE)

        _render_damage_control_section()
        out = capsys.readouterr().out
        assert "overlay replaces bundled files: cloud-hosting" in out


# ===========================================================================
# The acceptance that matters: drive the command Claude Code EXECUTES
# ===========================================================================

# `netlify sites:delete` is a hard block in the bundled cloud-hosting rules and, verified
# below with no filter, is caught by NOTHING else, so its absence is observable.
NETLIFY_CMD = "netlify sites:delete my-production-site"
NETLIFY_RULE_ID = "cloud-hosting.netlify-sites-delete-deletes-entire-site"


def _run_registered_hook(home: Path, command: str):
    """Run the hook exactly as heal registered it: the recorded interpreter and arguments."""
    pre = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    (hook,) = [h for e in pre if e["matcher"] == "Bash" for h in e["hooks"]]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "AGENTWIRE_DIR": str(home / ".agentwire")}
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(home)})
    return subprocess.run([hook["command"], *hook["args"]], input=payload, capture_output=True, text=True, env=env)


def _audit_rule_ids(home: Path) -> list:
    log_dir = home / ".agentwire" / "logs" / "damage-control"
    ids = []
    for f in sorted(log_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                ids.append(json.loads(line).get("rule_id"))
            except ValueError:
                pass
    return ids


class TestRegisteredPathBehaviour:
    def test_stale_copy_is_retired_and_the_registered_hook_blocks_by_rule_id(self, machine, baselines):
        """End to end, in the order the incident happened.

        1. the machine carries a previously shipped cloud-hosting.yaml missing the netlify guard,
        2. the registered hook does NOT block it (verified against the FULL live rule set, so a
           negative proves it looked),
        3. heal,
        4. the SAME registered command now blocks, by the expected rule id, read out of the audit
           row that hook itself wrote.
        """
        safety_commands.heal_damage_control(quiet=True)
        packs.write_state(safety_commands.DAMAGECONTROL_FILE, packs=["cloud-hosting"])

        bundled = (BUNDLED_RULES / "cloud-hosting.yaml").read_text()
        stale = bundled.replace(r"    pattern: '\bnetlify\s+sites:delete\b'", "    pattern: 'ZZZ-NEVER'")
        assert stale != bundled, "mutation did not change the corpus"
        target = safety_commands.RULES_DIR / "cloud-hosting.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(stale)
        baselines("rules", "cloud-hosting.yaml", stale.encode())

        live = engine.load_effective_config()
        assert len(live["bashToolPatterns"]) >= 140, "live corpus is suspiciously small"  # vendor packs off but cloud-hosting
        assert engine.check_command(NETLIFY_CMD, live)["decision"] != "block"
        before = _run_registered_hook(machine, NETLIFY_CMD)
        assert before.returncode == 0, before.stderr
        assert "SECURITY: Blocked" not in before.stderr

        summary = safety_commands.heal_damage_control(quiet=True)
        assert "cloud-hosting.yaml" in summary["rules_retired"]

        after = _run_registered_hook(machine, NETLIFY_CMD)
        assert after.returncode == 2, (after.returncode, after.stdout, after.stderr)
        assert "SECURITY: Blocked" in after.stderr
        assert NETLIFY_RULE_ID in _audit_rule_ids(machine)

    def test_registered_command_runs_under_the_recorded_interpreter(self, machine):
        safety_commands.heal_damage_control(quiet=True)
        record = json.loads(safety_commands.INSTALL_RECORD.read_text())
        assert record["interpreter"] == sys.executable
        assert safety_commands.interpreter_state()["state"] == "ok"
