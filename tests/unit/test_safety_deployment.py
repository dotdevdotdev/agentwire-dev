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
import time
from pathlib import Path

import pytest

from agentwire import safety_commands
from agentwire.safety import provenance as prov
from agentwire.safety._core import load_config

BUNDLED_RULES = Path(safety_commands.__file__).parent / "hooks" / "damage-control" / "rules"
BUNDLED_TOOLDEFS = Path(safety_commands.__file__).parent / "tooldefs"
RUNNING_PKG = Path(safety_commands.__file__).parent

#: Captured before any fixture pins it, so the resolver's own test can assert
#: against the real implementation rather than the fixture's stub.
_REAL_CANONICAL = prov.canonical_package_dir


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
    def test_no_install_and_not_a_worktree_is_bootstrap(self, machine, monkeypatch):
        """A fresh machine must stay installable — refusing here bricks setup."""
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: None)
        monkeypatch.setattr(prov, "in_git_worktree", lambda _p: False)
        state, canonical, _running = prov.install_provenance()
        assert state == prov.BOOTSTRAP
        assert canonical is None

    def test_no_install_but_a_worktree_still_refuses(self, machine, monkeypatch):
        """A task branch is never a legitimate bootstrap source (#936)."""
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: None)
        monkeypatch.setattr(prov, "in_git_worktree", lambda _p: True)
        state, canonical, _running = prov.install_provenance()
        assert state == prov.WORKTREE
        assert canonical is None
        assert state in prov.REFUSING_STATES

        summary = safety_commands.heal_damage_control(quiet=True)
        assert summary["refused"] is True
        assert not safety_commands.HOOKS_DIR.exists()

    def test_rebuild_refuses_to_install_a_worktree_as_the_tool(self, tmp_path, capsys):
        """F2: the guard must not block the one-step and permit the two-step.

        `rebuild` is the only installer-adjacent command that CHANGES the answer
        every other provenance check reads:

            uv run agentwire hooks install  (worktree) -> refused
            uv run agentwire rebuild        (worktree) -> worktree becomes canonical
            agentwire hooks install                    -> proceeds, legitimately

        and that last step is the one CLAUDE.md tells people to run after a code
        change. Guarding `heal` alone is a guard with a door beside it.
        """
        from agentwire import system_cli

        class Args:
            force = False
            allow_foreign_source = False

        reached_install = []

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(prov, "is_worktree_checkout", lambda _root: True)
            # Anything past the guard eventually shells out; make that visible
            # rather than letting a silent pass look like a refusal.
            mp.setattr(system_cli, "_git_behind_origin",
                       lambda _r: (reached_install.append("git-check"), (0, None))[1])
            rc = system_cli.cmd_rebuild(Args())

        err = capsys.readouterr().err
        assert rc == 1
        assert "REFUSED" in err
        assert "WORKTREE" in err
        assert reached_install == [], "rebuild ran on past the worktree guard"

    def test_rebuild_proceeds_from_a_primary_checkout(self, capsys):
        """Must-fail control for the test above: same call, guard says not a
        worktree, and rebuild gets past it to the git-drift check."""
        from agentwire import system_cli

        class Args:
            force = False
            allow_foreign_source = False

        reached = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(prov, "is_worktree_checkout", lambda _root: False)
            mp.setattr(system_cli, "_git_behind_origin",
                       lambda _r: (reached.append("git-check"), (5, None))[1])
            rc = system_cli.cmd_rebuild(Args())

        assert reached == ["git-check"], "the worktree guard blocked a primary checkout"
        assert rc == 1  # stopped by the BEHIND-MAIN guard, a different refusal
        assert "behind origin/main" in capsys.readouterr().out

    def test_rebuild_worktree_guard_is_not_folded_into_force(self):
        """--force means 'behind origin/main'. It must NOT also grant a
        machine-global install from a task branch — that would make the
        documented staleness override silently carry a second meaning."""
        import inspect

        from agentwire import system_cli

        src = inspect.getsource(system_cli.cmd_rebuild)
        guard = src.split("is_worktree_checkout", 1)[1].split("return 1", 1)[0]
        assert "allow_foreign_source" in guard
        assert "force" not in guard

    def test_worktree_detection_reads_the_dot_git_kind(self, tmp_path):
        """worktree -> .git is a FILE; primary checkout -> a DIR; installed -> neither."""
        wt = tmp_path / "wt" / "agentwire"
        wt.mkdir(parents=True)
        (wt.parent / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
        assert prov.in_git_worktree(wt) is True

        primary = tmp_path / "primary" / "agentwire"
        primary.mkdir(parents=True)
        (primary.parent / ".git").mkdir()
        assert prov.in_git_worktree(primary) is False

        installed = tmp_path / "site-packages" / "agentwire"
        installed.mkdir(parents=True)
        assert prov.in_git_worktree(installed) is False

    def test_running_package_is_canonical(self, machine):
        state, canonical, running = prov.install_provenance()
        assert state == prov.CANONICAL
        assert canonical == running == RUNNING_PKG.resolve()

    def test_other_install_is_foreign(self, machine, monkeypatch, tmp_path):
        other = tmp_path / "installed" / "agentwire"
        other.mkdir(parents=True)
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: other.resolve())
        state, canonical, running = prov.install_provenance()
        assert state == prov.FOREIGN
        assert canonical == other.resolve()
        assert running == RUNNING_PKG.resolve()

    def test_uv_tool_layout_is_found(self, machine, monkeypatch, tmp_path):
        """The documented install path, resolved from LAYOUT not from $PATH."""
        # This is the one test that exercises the REAL resolver, so undo the
        # fixture's pin first — otherwise it asserts against its own stub.
        monkeypatch.setattr(prov, "canonical_package_dir", _REAL_CANONICAL)
        tools = tmp_path / "tools"
        pkg = tools / "agentwire-dev" / "lib" / "python3.13" / "site-packages" / "agentwire"
        pkg.mkdir(parents=True)
        monkeypatch.setenv("UV_TOOL_DIR", str(tools))
        assert prov.canonical_package_dir() == pkg.resolve()

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

    def test_mutation_allow_foreign_proves_provenance_was_the_blocker(
        self, machine, monkeypatch, tmp_path
    ):
        """The must-fail control: flip ONLY the override and the heal proceeds.

        Without this the refusal test would also pass if the heal were broken
        for some entirely different reason.
        """
        other = tmp_path / "installed" / "agentwire"
        other.mkdir(parents=True)
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: other.resolve())

        summary = safety_commands.heal_damage_control(quiet=True, allow_foreign=True)
        assert summary["refused"] is False
        assert summary["hooks_installed"]
        assert summary["rules_installed"]

    def test_install_cmd_returns_nonzero_when_refused(self, machine, monkeypatch, tmp_path):
        """A refusal must never be reportable as a successful install."""
        other = tmp_path / "installed" / "agentwire"
        other.mkdir(parents=True)
        monkeypatch.setattr(prov, "canonical_package_dir", lambda: other.resolve())
        assert safety_commands.safety_install_cmd(assume_yes=True) == 1

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
    def test_every_generated_hook_carries_a_stamp(self):
        """No stamp, no ordering — this is the precondition for all of #936."""
        for fn in safety_commands.DAMAGE_CONTROL_FILES:
            if fn == "audit_logger.py":
                continue  # hand-written, deliberately unstamped
            stamp = safety_commands.read_hook_stamp(BUNDLED_RULES.parent / fn)
            assert stamp, f"{fn} has no AGENTWIRE_HOOK_STAMP"
            assert len(stamp["core_sha256"]) == 64
            assert stamp["generated_at"].endswith("Z")

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

    def test_mutation_force_proves_ordering_was_the_blocker(self, machine):
        """Must-fail control for the test above."""
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.HOOKS_DIR / HOOK
        target.write_text(_restamp(target.read_text(), "2099-01-01T00:00:00Z"))

        summary = safety_commands.heal_damage_control(quiet=True, force=True)
        assert HOOK in summary["hooks_updated"]
        assert safety_commands.damage_control_hook_drift()[HOOK] == "ok"

    def test_installed_older_is_updated(self, machine):
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.HOOKS_DIR / HOOK
        target.write_text(_restamp(target.read_text(), "2000-01-01T00:00:00Z"))

        assert safety_commands.damage_control_hook_drift()[HOOK] == "older"
        summary = safety_commands.heal_damage_control(quiet=True)
        assert HOOK in summary["hooks_updated"]
        assert safety_commands.damage_control_hook_drift()[HOOK] == "ok"

    def test_unstamped_difference_is_stale_not_ordered(self, machine):
        """An unorderable difference must say so, not guess a direction."""
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.HOOKS_DIR / HOOK
        target.write_text("# hand-mangled, no stamp\n")
        assert safety_commands.damage_control_hook_drift()[HOOK] == "stale"
        summary = safety_commands.heal_damage_control(quiet=True)
        assert HOOK in summary["hooks_updated"]

    def test_install_does_not_preserve_source_mtime(self, machine):
        """`copy2` preserving mtime is what made the #936 downgrade invisible."""
        started = time.time()
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.HOOKS_DIR / HOOK
        source = BUNDLED_RULES.parent / HOOK
        assert target.stat().st_mtime >= started - 1
        # And the mutation that would reintroduce the bug: copy2 would make
        # these equal.
        assert target.stat().st_mtime != source.stat().st_mtime


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
    def test_previously_shipped_version_is_brought_forward(self, machine, baselines):
        """The #916 core: an out-of-date rule file finally updates."""
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.RULES_DIR / "core.yaml"
        bundled = (BUNDLED_RULES / "core.yaml").read_bytes()

        target.write_text(OLD_RELEASE)
        baselines("rules", "core.yaml", OLD_RELEASE.encode())

        assert safety_commands.rules_drift()["core.yaml"] == "outdated"
        summary = safety_commands.heal_damage_control(quiet=True)
        assert "core.yaml" in summary["rules_updated"]
        assert target.read_bytes() == bundled

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

    def test_unrecognized_reported_loudly_not_silently_skipped(self, machine, baselines, capsys):
        safety_commands.heal_damage_control(quiet=True)
        (safety_commands.RULES_DIR / "core.yaml").write_text("# mine\n")
        safety_commands.heal_damage_control()
        out = capsys.readouterr().out
        assert "core.yaml" in out
        assert "matches NO version we ever shipped" in out

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

    def test_install_cmd_returns_nonzero_when_files_held_back(self, machine, baselines):
        safety_commands.heal_damage_control(quiet=True)
        (safety_commands.RULES_DIR / "core.yaml").write_text("# mine\n")
        assert safety_commands.safety_install_cmd(assume_yes=True) == 1

    def test_tooldefs_sync_the_same_way(self, machine, baselines):
        """Tooldefs drifted identically and lost DEFAULT_UNATTENDED_ALLOW."""
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.TOOLDEFS_DIR / "git.yaml"
        old = "# older shipped tooldef\ncommands: []\n"
        target.write_text(old)
        baselines("tooldefs", "git.yaml", old.encode())

        assert safety_commands.tooldefs_drift()["git.yaml"] == "outdated"
        summary = safety_commands.heal_damage_control(quiet=True)
        assert "git.yaml" in summary["tooldefs_updated"]
        assert target.read_bytes() == (BUNDLED_TOOLDEFS / "git.yaml").read_bytes()

    def test_tooldef_refresh_announces_the_permissions_change(self, machine, baselines, capsys):
        """A tooldef refresh reads as a chore and lands as a grant (#916)."""
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.TOOLDEFS_DIR / "git.yaml"
        old = "# older shipped tooldef\ncommands: []\n"
        target.write_text(old)
        baselines("tooldefs", "git.yaml", old.encode())

        safety_commands.heal_damage_control()
        out = capsys.readouterr().out
        assert "PERMISSIONS CHANGE" in out
        assert "git.commit" in out
        assert "UNATTENDED" in out


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


class TestRuleIdUniqueness:
    def test_bundled_rule_set_has_no_duplicate_ids(self):
        """Measured against BUNDLED rules AND BUNDLED tooldefs, counts asserted."""
        cfg = load_config(BUNDLED_RULES, BUNDLED_TOOLDEFS)
        patterns = cfg["bashToolPatterns"]
        assert len(patterns) == 265
        assert sum(1 for p in patterns if p.get("anchored")) == 101
        assert cfg.get("_duplicate_rule_ids") is None

    def test_pinned_id_collision_is_detected_by_id(self, tmp_path):
        """The exact shape a half-applied sync produces: a new file pinning ids
        that a stale file still carries."""
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "databases.yaml").write_text(
            "bashToolPatterns:\n"
            "  - id: databases.drop-table\n"
            "    pattern: 'DROP TABLE'\n"
            "    reason: drop table\n"
        )
        (rules / "payloads.yaml").write_text(
            "bashToolPatterns:\n"
            "  - id: databases.drop-table\n"
            "    pattern: 'DROP TABLE'\n"
            "    reason: drop table\n"
        )
        cfg = load_config(rules)
        # The ID, not just "some duplicate exists".
        assert cfg["_duplicate_rule_ids"] == ["databases.drop-table"]

    def test_mutation_unpinning_one_clears_it(self, tmp_path):
        """Must-fail control, and it proves the corpus really changed: drop the
        second file's `id:` and the collision goes away."""
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "databases.yaml").write_text(
            "bashToolPatterns:\n"
            "  - id: databases.drop-table\n"
            "    pattern: 'DROP TABLE'\n"
            "    reason: drop table\n"
        )
        (rules / "payloads.yaml").write_text(
            "bashToolPatterns:\n"
            "  - pattern: 'DROP TABLE'\n"
            "    reason: drop table\n"
        )
        cfg = load_config(rules)
        assert cfg.get("_duplicate_rule_ids") is None
        # ...and the corpus genuinely still has both rules, so the cleared
        # verdict is not "nothing loaded".
        assert len(cfg["bashToolPatterns"]) == 2

    def test_derived_ids_still_deconflict(self, tmp_path):
        """Only EXPLICIT ids collide; derived ones keep their -2 suffix."""
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "a.yaml").write_text(
            "bashToolPatterns:\n"
            "  - pattern: 'X'\n    reason: same reason\n"
            "  - pattern: 'Y'\n    reason: same reason\n"
        )
        cfg = load_config(rules)
        assert [p["id"] for p in cfg["bashToolPatterns"]] == [
            "a.same-reason", "a.same-reason-2",
        ]
        assert cfg.get("_duplicate_rule_ids") is None

    def test_status_surfaces_duplicates(self, machine, baselines):
        safety_commands.heal_damage_control(quiet=True)
        (safety_commands.RULES_DIR / "zz-dupe.yaml").write_text(
            "bashToolPatterns:\n"
            "  - id: core.rm-rf\n    pattern: 'ZZZ'\n    reason: dupe\n"
        )
        (safety_commands.RULES_DIR / "aa-dupe.yaml").write_text(
            "bashToolPatterns:\n"
            "  - id: core.rm-rf\n    pattern: 'ZZZ'\n    reason: dupe\n"
        )
        status = safety_commands.get_safety_status()
        assert status["duplicate_rule_ids"] == ["core.rm-rf"]


# ===========================================================================
# Doctor — direction, not "differs" (#916 + #936)
# ===========================================================================


def _render_doctor():
    from agentwire.doctor_cli import _render_damage_control_section
    return _render_damage_control_section()


class TestDoctorDirection:
    @pytest.fixture(autouse=True)
    def _enabled(self, machine, baselines):
        safety_commands.heal_damage_control(quiet=True)
        safety_commands.DAMAGECONTROL_FILE.write_text("enabled: true\n")

    def test_clean_machine_is_clean(self, capsys):
        issues = _render_doctor()
        out = capsys.readouterr().out
        assert "[ok] DC hook scripts current" in out
        assert "[ok] Damage-control rules installed and match bundled" in out
        assert "[ok] Damage-control tooldefs installed and match bundled" in out
        assert "[ok] Rule ids unique across the loaded rule set" in out
        assert issues == 0

    def test_says_installed_hook_is_older_than_package(self, capsys):
        target = safety_commands.HOOKS_DIR / HOOK
        target.write_text(_restamp(target.read_text(), "2000-01-01T00:00:00Z"))
        issues = _render_doctor()
        out = capsys.readouterr().out
        assert "OLDER than this package" in out
        assert HOOK in out
        assert issues >= 1

    def test_says_installed_hook_is_newer_than_package(self, capsys):
        """The sentence doctor structurally could not say before (#936)."""
        target = safety_commands.HOOKS_DIR / HOOK
        target.write_text(_restamp(target.read_text(), "2099-01-01T00:00:00Z"))
        issues = _render_doctor()
        out = capsys.readouterr().out
        assert "NEWER than this package" in out
        assert "DOWNGRADE" in out
        assert issues >= 1

    def test_outdated_rules_are_loud_not_informational(self, baselines, capsys):
        target = safety_commands.RULES_DIR / "cloud-hosting.yaml"
        target.write_text(OLD_RELEASE)
        baselines("rules", "cloud-hosting.yaml", OLD_RELEASE.encode())
        issues = _render_doctor()
        out = capsys.readouterr().out
        assert "[!!] Damage-control rules are OUT OF DATE" in out
        assert "cloud-hosting.yaml" in out
        assert issues >= 1

    def test_removed_protections_are_named(self, capsys):
        """"Differ from bundled" cannot tell a tweak from a disabled deploy tier."""
        bundled = (BUNDLED_RULES / "cloud-hosting.yaml").read_text()
        stripped = bundled.replace(r"  - pattern: '\bnetlify\s+sites:delete\b'", "  - pattern: 'ZZZ-NEVER'")
        assert stripped != bundled, "mutation did not change the corpus"
        (safety_commands.RULES_DIR / "cloud-hosting.yaml").write_text(stripped)

        issues = _render_doctor()
        out = capsys.readouterr().out
        assert "MISSING bundled protections" in out
        assert r"\bnetlify\s+sites:delete\b" in out
        assert issues >= 1

    def test_local_additions_only_stay_informational(self, capsys):
        """Extra local rules are a legitimate customization — [..], not [!!]."""
        bundled = (BUNDLED_RULES / "cloud-hosting.yaml").read_text()
        extended = bundled + "\n  - pattern: 'MY-OWN-RULE'\n    reason: mine\n"
        (safety_commands.RULES_DIR / "cloud-hosting.yaml").write_text(extended)

        issues = _render_doctor()
        out = capsys.readouterr().out
        assert "[..] Damage-control rules with local additions only" in out
        assert "MISSING bundled protections" not in out
        assert issues == 0

    def test_duplicate_ids_are_flagged(self, capsys):
        (safety_commands.RULES_DIR / "zz-dupe.yaml").write_text(
            "bashToolPatterns:\n"
            "  - id: core.rm-rf\n    pattern: 'ZZZ'\n    reason: dupe\n"
        )
        (safety_commands.RULES_DIR / "aa-dupe.yaml").write_text(
            "bashToolPatterns:\n"
            "  - id: core.rm-rf\n    pattern: 'ZZZ'\n    reason: dupe\n"
        )
        issues = _render_doctor()
        out = capsys.readouterr().out
        assert "DUPLICATE rule ids" in out
        assert "core.rm-rf" in out
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
        safety_commands.heal_damage_control(quiet=True)

        bundled = (BUNDLED_RULES / "cloud-hosting.yaml").read_text()
        stale = bundled.replace(
            r"  - pattern: '\bnetlify\s+sites:delete\b'", "  - pattern: 'ZZZ-NEVER'"
        )
        assert stale != bundled, "mutation did not change the corpus"
        target = safety_commands.RULES_DIR / "cloud-hosting.yaml"
        target.write_text(stale)
        baselines("rules", "cloud-hosting.yaml", stale.encode())

        # (2) The negative, taken against the FULL live rule set — every rule
        # file, bundled tooldefs, no filtering — so "not blocked" cannot be an
        # artifact of a narrowed corpus.
        live = load_config(safety_commands.RULES_DIR, safety_commands.TOOLDEFS_DIR)
        assert len(live["bashToolPatterns"]) >= 260, "live corpus is suspiciously small"
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

    def test_installed_hook_bytes_match_the_package(self, machine):
        """A deployment claim names the path that EXECUTES."""
        safety_commands.heal_damage_control(quiet=True)
        installed = safety_commands.HOOKS_DIR / HOOK
        packaged = BUNDLED_RULES.parent / HOOK
        assert installed.read_bytes() == packaged.read_bytes()
        assert os.access(installed, os.X_OK)

    def _neuter(self, text: str) -> str:
        """Make a hook that ALLOWS everything, preserving its stamp block.

        Stands in for "a hook predating a security fix": the difference has to
        be observable by RUNNING it, or the test measures bytes rather than
        protection.
        """
        neutered = text.replace(
            "    result = check_command(command, config)",
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
        safety_commands.heal_damage_control(quiet=True)
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

    def test_newer_hook_is_not_downgraded_and_keeps_behaving(self, machine):
        """The refusal has teeth at the BEHAVIOUR level, not just the byte level.

        Inverted on purpose: the installed copy BLOCKS and the package would
        replace it with one that allows. If the refusal were cosmetic, the
        protection would be gone after the heal.
        """
        safety_commands.heal_damage_control(quiet=True)
        target = safety_commands.HOOKS_DIR / HOOK
        good = target.read_text()

        # Package side is the neutered one; the machine keeps a NEWER good hook.
        target.write_text(_restamp(good, "2099-01-01T00:00:00Z"))
        packaged = BUNDLED_RULES.parent / HOOK
        original_package = packaged.read_bytes()
        try:
            packaged.write_text(self._neuter(good))
            assert safety_commands.damage_control_hook_drift()[HOOK] == "newer"

            summary = safety_commands.heal_damage_control(quiet=True)
            assert HOOK in summary["hooks_downgrade_refused"]

            after = _run_installed_hook(machine, NETLIFY_CMD)
            assert after.returncode == 2, "the deployed guard was silently downgraded"

            # Mutation: --force accepts the downgrade, and protection IS lost.
            safety_commands.heal_damage_control(quiet=True, force=True)
            forced = _run_installed_hook(machine, NETLIFY_CMD)
            assert forced.returncode == 0, (
                "force did not actually install the older hook — the test above "
                "would then be passing for the wrong reason"
            )
        finally:
            packaged.write_bytes(original_package)
