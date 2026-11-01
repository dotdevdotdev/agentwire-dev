"""The beta gate machinery, exercised with a SYNTHETIC flag.

No beta feature is in flight (the ``voice_layer`` flag left with the buddy,
#1230), but the machinery is kept for the next one — so these pin its
properties against a flag that exists only inside the tests:

- a gated region ships (markers stripped) when its flag is on, and goes —
  markers and all — when it is off or unknown (fail CLOSED);
- no marker-shaped text survives into a shipped role prompt;
- only a real YAML boolean opens a gate, and a broken config opens nothing.
"""

import dataclasses
from pathlib import Path

import pytest

from agentwire import beta as beta_mod
from agentwire import config as config_mod
from agentwire import roles as roles_mod

ROLES_DIR = Path(roles_mod.__file__).parent

BLOCK = "keep\n<!-- beta:synthetic -->\ngated\n<!-- /beta:synthetic -->\nkeep2\n"


@pytest.fixture(autouse=True)
def synthetic_flag(monkeypatch):
    """Register ``synthetic`` as the one known flag, with a matching config field."""
    synthetic_cfg = dataclasses.make_dataclass("BetaConfig", [("synthetic", bool, False)])
    monkeypatch.setattr(config_mod, "BETA_FLAG_NAMES", frozenset({"synthetic"}))
    monkeypatch.setattr(config_mod, "BetaConfig", synthetic_cfg)
    beta_mod.reset_cache()
    yield
    beta_mod.reset_cache()


class TestMarkerMechanics:
    def test_enabled_flag_ships_the_body_without_markers(self):
        assert beta_mod.apply_beta_blocks(BLOCK, enabled={"synthetic"}) == "keep\ngated\nkeep2\n"

    def test_disabled_flag_removes_the_whole_region(self):
        assert beta_mod.apply_beta_blocks(BLOCK, enabled=set()) == "keep\nkeep2\n"

    def test_an_unknown_flag_fails_closed(self):
        """A marker naming a flag nothing knows about removes its block, even if
        the caller says it is enabled — shipping text no gate can turn off is the
        failure this module exists to prevent."""
        text = BLOCK.replace("synthetic", "not_a_flag")
        assert beta_mod.apply_beta_blocks(text, enabled={"not_a_flag"}) == "keep\nkeep2\n"

    def test_gated_doc_resolves_a_docstring_once_at_decoration(self, monkeypatch):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"synthetic"})

        @beta_mod.gated_doc
        def tool():
            """Head.

            <!-- beta:synthetic -->
            Beta prose.
            <!-- /beta:synthetic -->
            """

        assert "Beta prose." in tool.__doc__ and "beta:" not in tool.__doc__

    def test_a_broken_marker_never_reaches_the_model(self):
        """``render`` strips a stray marker line even though the audit, not the
        render, is what catches the region it failed to wrap."""
        assert "beta:" not in beta_mod.render("a\n<!-- beta:synthetic -->\nb\n", enabled=set())


class TestShippedPrompts:
    def test_no_role_file_carries_a_marker_in_any_flag_state(self):
        """The audit: what the gate leaves behind is exactly the set of broken
        markers, so any marker-shaped text in a shipped role is a defect."""
        bad = []
        for path in sorted(ROLES_DIR.glob("*.md")):
            text = path.read_text()
            for enabled in (set(), {"synthetic"}):
                if beta_mod.LOOSE_MARKER_RE.search(beta_mod.apply_beta_blocks(text, enabled)):
                    bad.append((path.name, sorted(enabled)))
        assert not bad, bad

    def test_every_marker_in_a_shipped_role_names_a_real_flag(self):
        """Against the REAL flag set, restored here: an unknown name would
        silently delete its section forever."""
        import re

        real = frozenset()  # no beta feature is in flight
        marker = re.compile(r"<!-- /?beta:([a-z0-9_]+) -->")
        bad = [
            (p.name, m)
            for p in sorted(ROLES_DIR.glob("*.md"))
            for m in marker.findall(p.read_text())
            if m not in real
        ]
        assert not bad, bad


class TestFlagParsing:
    @pytest.fixture
    def write_config(self, tmp_path, monkeypatch):
        def _write(body: str):
            path = tmp_path / "config.yaml"
            path.write_text(body)
            monkeypatch.setattr(config_mod, "default_config_path", lambda: path)

        return _write

    def test_only_a_real_yaml_boolean_opens_the_gate(self, write_config):
        bad = []
        for body, want in (
            ("beta:\n  synthetic: true\n", {"synthetic"}),
            ('beta:\n  synthetic: "true"\n', set()),
            ('beta:\n  synthetic: "false"\n', set()),
            ("beta:\n  synthetic: 1\n", set()),
            ("beta: nonsense\n", set()),
            ("beta:\n  other: true\n", set()),
            ("{{ not yaml", set()),
            ("", set()),
        ):
            write_config(body)
            got = config_mod.enabled_beta_flags()
            if got != want:
                bad.append((body, got, want))
        assert not bad, bad

    def test_a_missing_config_file_opens_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "default_config_path", lambda: tmp_path / "absent.yaml")
        assert config_mod.enabled_beta_flags() == set()


def test_shipped_build_has_no_beta_flags_and_doctor_says_so(monkeypatch, capsys):
    from agentwire import doctor_cli

    monkeypatch.setattr(config_mod, "BETA_FLAG_NAMES", frozenset())
    assert doctor_cli._render_beta_section() == 0
    assert "no beta features" in capsys.readouterr().out
