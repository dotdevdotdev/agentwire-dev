"""The beta gate that lets the voice layer ship to main (owner ruling 2026-08-10).

The acceptance bar is exact and it is the reason this file exists: **with the
flag off, a non-voice user's rendered system prompt must be byte-identical to
what ``origin/main`` produces today.** Proved against a snapshot of main's role
files (``tests/fixtures/main_roles/``), never by inspection — the whole failure
mode is text that *looks* the same.

The snapshot is itself checked against ``origin/main`` when git can reach that
ref, so the fixture cannot quietly drift into agreeing with the branch.
"""

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from agentwire import config as config_mod
from agentwire import roles as roles_mod

ROLES_DIR = Path(roles_mod.__file__).parent
FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "main_roles"

#: Every role file this branch touched. The list is the claim: any other role
#: file the voice layer edits later must be added here or the bar is not met.
GATED_ROLE_FILES = ["agentwire.md", "orchestrator.md", "worker.md", "worker-worktree.md"]


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def _shipped(name: str) -> str:
    return (ROLES_DIR / name).read_text()


class TestFixtureIsHonest:
    """The snapshot must be main's text, not a copy of the branch's."""

    def test_every_fixture_matches_origin_main(self):
        for name in GATED_ROLE_FILES:
            proc = subprocess.run(
                ["git", "show", f"origin/main:agentwire/roles/{name}"],
                cwd=Path(__file__).parent.parent.parent,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                pytest.skip("origin/main not fetched in this checkout")
            assert proc.stdout == _fixture(name), (
                f"tests/fixtures/main_roles/{name} has drifted from origin/main — "
                "regenerate it, do not edit it by hand"
            )

    def test_the_fixture_is_not_just_the_branch(self):
        """A control: without it, a fixture regenerated from the BRANCH would
        make every byte-identity assertion below pass while proving nothing."""
        differing = [n for n in GATED_ROLE_FILES if _fixture(n) != _shipped(n)]
        assert differing == GATED_ROLE_FILES, (
            "every gated role file should differ from main's text on this branch"
        )


class TestFlagOffIsByteIdenticalToMain:
    def test_raw_role_text_with_the_gate_off_equals_main(self):
        for name in GATED_ROLE_FILES:
            rendered = roles_mod.apply_beta_blocks(_shipped(name), enabled=set())
            assert rendered == _fixture(name), f"{name} is not byte-identical to main"

    def test_parsed_instructions_with_the_gate_off_equal_main(self, monkeypatch):
        """The prompt as it actually renders — through ``parse_role_file``,
        which is what every role reader in the tree goes through."""
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        for name in GATED_ROLE_FILES:
            shipped = roles_mod.parse_role_file(ROLES_DIR / name)
            expected = roles_mod.parse_role_file(FIXTURE_DIR / name)
            assert shipped.instructions == expected.instructions, name

    def test_merged_prompt_with_the_gate_off_equals_main(self, monkeypatch):
        """Merged across a real role set — a per-file check cannot see a
        separator the merge introduces."""
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        names = ["agentwire.md", "worker-worktree.md"]
        shipped = roles_mod.merge_roles(
            [roles_mod.parse_role_file(ROLES_DIR / n) for n in names]
        )
        expected = roles_mod.merge_roles(
            [roles_mod.parse_role_file(FIXTURE_DIR / n) for n in names]
        )
        assert shipped.instructions == expected.instructions

    def test_no_marker_comment_survives_into_the_prompt(self, monkeypatch):
        """Off *or* on, the markers are scaffolding — shipping them would cost
        the tokens the gate exists to save, and read as noise to the model."""
        for flags in (set(), {"voice_layer"}):
            monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda f=flags: f)
            for name in GATED_ROLE_FILES:
                text = roles_mod.parse_role_file(ROLES_DIR / name).instructions
                assert "beta:" not in text, f"{name} leaked a marker with flags={flags}"


class TestFlagOnRestoresTheVoiceLines:
    def test_the_voice_sections_are_present_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        for name in ["orchestrator.md", "worker.md", "worker-worktree.md"]:
            text = roles_mod.parse_role_file(ROLES_DIR / name).instructions
            assert "Replying to the voice buddy" in text, name
            assert "msg send --to buddy --kind done" in text, name

    def test_enabling_the_flag_changes_the_prompt(self, monkeypatch):
        """The must-fail control for the byte-identity tests above: if the gate
        stripped nothing, off and on would render the same and every assertion
        in this file would be vacuous."""
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        off = {n: roles_mod.parse_role_file(ROLES_DIR / n).instructions for n in GATED_ROLE_FILES}
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        on = {n: roles_mod.parse_role_file(ROLES_DIR / n).instructions for n in GATED_ROLE_FILES}
        assert all(off[n] != on[n] for n in GATED_ROLE_FILES)


class TestMarkerMechanics:
    def test_an_unknown_flag_fails_closed(self):
        """A marker naming a flag nothing knows about removes its block. The
        other direction — shipping text no gate can turn off — is the failure
        this whole file is about."""
        text = "keep\n<!-- beta:not_a_flag -->\ndrop\n<!-- /beta:not_a_flag -->\nkeep2\n"
        assert roles_mod.apply_beta_blocks(text, enabled={"voice_layer"}) == "keep\nkeep2\n"

    def test_every_marker_in_the_shipped_roles_names_a_known_flag(self):
        """Pairs with the rule above: fail-closed is only safe if a typo is
        caught here rather than silently deleting a section forever."""
        for path in sorted(ROLES_DIR.glob("*.md")):
            for flag, _body in roles_mod.BETA_BLOCK_RE.findall(path.read_text()):
                assert flag in roles_mod.beta_flag_names(), (
                    f"{path.name}: unknown beta flag {flag!r}"
                )

    def test_an_unclosed_marker_is_left_alone(self):
        text = "a\n<!-- beta:voice_layer -->\nb\n"
        assert roles_mod.apply_beta_blocks(text, enabled=set()) == text


class TestConfigFlag:
    def test_default_is_off(self, tmp_path):
        cfg = config_mod.load_config(tmp_path / "nonexistent.yaml")
        assert cfg.beta.voice_layer is False

    def test_reads_the_key_from_yaml(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("beta:\n  voice_layer: true\n")
        assert config_mod.load_config(p).beta.voice_layer is True

    def test_enabled_flags_reflects_the_config(self, monkeypatch):
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda *a, **k: config_mod.Config(beta=config_mod.BetaConfig(voice_layer=True)),
        )
        assert config_mod.enabled_beta_flags() == {"voice_layer"}

    def test_a_broken_config_fails_closed(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("unreadable")

        monkeypatch.setattr(config_mod, "load_config", boom)
        assert config_mod.enabled_beta_flags() == set()


# =============================================================================
# The buddy CLI refuses when the flag is off — naming BOTH next moves
# =============================================================================


class TestBuddyCliRefusal:
    """A refusal that does not name the next move is the defect this project
    keeps closing, so the message is asserted on rather than merely the exit
    code: the exact config key, and the fact that the OpenAI key must be in the
    secrets file."""

    @pytest.fixture
    def off(self, monkeypatch):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())

    @pytest.fixture
    def on(self, monkeypatch):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})

    def _run(self, subcommand, argv=()):
        from agentwire import buddy_cli

        parser = argparse.ArgumentParser()
        buddy_cli.register_buddy_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["buddy", subcommand, *argv])
        return args.func(args)

    def test_serve_refuses_with_the_flag_off(self, off, capsys):
        assert self._run("serve") == 1
        err = capsys.readouterr().err
        assert "beta.voice_layer" in err or "voice_layer: true" in err

    def test_the_refusal_names_the_config_key_and_the_secret(self, off, capsys):
        self._run("serve")
        err = capsys.readouterr().err
        assert "voice_layer" in err
        assert "config.yaml" in err
        assert "OPENAI_API_KEY" in err
        assert ".env" in err

    def test_every_buddy_subcommand_is_gated(self, off, capsys):
        """The pin against the next subcommand forgetting the decorator — a
        read-only verb that still works is a beta feature that is half-on."""
        from agentwire import buddy_cli

        parser = argparse.ArgumentParser()
        buddy_cli.register_buddy_parser(parser.add_subparsers(dest="command"))
        actions = [
            a for a in parser._subparsers._group_actions[0].choices["buddy"]._actions
            if isinstance(a, argparse._SubParsersAction)
        ]
        names = sorted(actions[0].choices)
        assert names, "no buddy subcommands found — the walk is wrong, not the gate"
        for name in names:
            func = actions[0].choices[name].get_default("func")
            assert getattr(func, "_beta_gated", False), f"buddy {name} is not gated"

    def test_json_mode_refuses_as_json(self, off, capsys):
        assert self._run("serve", ["--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert payload["beta_flag"] == "beta.voice_layer"
        assert payload["secret"] == "OPENAI_API_KEY"

    def test_the_gate_lets_the_command_through_when_on(self, on, monkeypatch):
        """The must-fail control: without it, a gate that refuses unconditionally
        would pass every assertion above."""
        from agentwire import buddy_cli

        seen = {}
        monkeypatch.setattr(
            buddy_cli.identity, "list_buddies", lambda: seen.setdefault("called", []) or []
        )
        assert self._run("list", ["--json"]) == 0
        assert "called" in seen


# =============================================================================
# doctor reports the flag — and never the key
# =============================================================================


class TestDoctorSection:
    def _render(self):
        from agentwire.doctor_cli import _render_beta_section

        return _render_beta_section()

    def test_off_is_reported_and_is_not_an_issue(self, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        assert self._render() == 0
        out = capsys.readouterr().out
        assert "voice_layer" in out
        assert "off" in out.lower()

    def test_on_with_a_key_is_green(self, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-SUPERSECRET-abcdef")
        assert self._render() == 0
        out = capsys.readouterr().out
        assert "[ok]" in out

    def test_on_without_a_key_is_an_issue_with_a_fix(self, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert self._render() == 1
        out = capsys.readouterr().out
        assert "[!!]" in out
        assert "OPENAI_API_KEY" in out
        assert ".env" in out

    def test_the_key_is_never_printed_not_even_a_prefix(self, monkeypatch, capsys):
        secret = "sk-proj-ZZZZQQQQ9999wwww"
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        self._render()
        out = capsys.readouterr().out
        for n in (4, 6, 8, len(secret)):
            assert secret[:n] not in out, f"doctor leaked the first {n} chars of the key"
        assert secret[-4:] not in out
