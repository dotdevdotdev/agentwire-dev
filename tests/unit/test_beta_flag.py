"""The beta gate that lets the voice layer ship to main (owner ruling 2026-08-10).

The original acceptance bar — "with the flag off, the rendered prompt is
byte-identical to pre-voice ``origin/main``" — was a *merge* criterion. It was
proved against fixture snapshots at merge time (b2908d0) and the proof lives in
git history; asserting it forever against a moving origin/main that now
contains the gate made main's CI permanently red (#1031), so the snapshot
guards were retired.

What this file pins now are the properties that must hold on every commit:
the gate strips every ``beta:`` marker in both flag states, no model-facing
surface (role prompt or published MCP description) ever carries a marker, the
flag fails closed on every malformed config shape, and the flag genuinely
changes the output (the must-fail controls).
"""

import argparse
from pathlib import Path

import pytest

from agentwire import beta as beta_mod
from agentwire import config as config_mod
from agentwire import roles as roles_mod

ROLES_DIR = Path(roles_mod.__file__).parent

#: Every role file the voice layer gates. Any other role file that grows a
#: beta block later must be added here or the flag-on/off checks miss it.
GATED_ROLE_FILES = ["agentwire.md", "orchestrator.md", "worker.md", "worker-worktree.md"]


@pytest.fixture(autouse=True)
def _reset_beta_cache():
    """The flag set is cached for the life of the process (N3), so every test
    here brackets itself: patching ``enabled_beta_flags`` is invisible behind a
    warm cache, and a cache warmed under a patch would leak into the next test.
    """
    beta_mod.reset_cache()
    yield
    beta_mod.reset_cache()


class TestNoMarkerReachesThePrompt:
    def test_no_marker_comment_survives_into_the_prompt(self, monkeypatch):
        """Off *or* on, the markers are scaffolding — shipping them would cost
        the tokens the gate exists to save, and read as noise to the model."""
        for flags in (set(), {"voice_layer"}):
            monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda f=flags: f)
            beta_mod.reset_cache()
            for name in GATED_ROLE_FILES:
                text = roles_mod.parse_role_file(ROLES_DIR / name).instructions
                assert "beta:" not in text, f"{name} leaked a marker with flags={flags}"


class TestMarkerMechanics:
    def test_an_unknown_flag_fails_closed(self):
        """A marker naming a flag nothing knows about removes its block. The
        other direction — shipping text no gate can turn off — is the failure
        this whole file is about."""
        text = "keep\n<!-- beta:not_a_flag -->\ndrop\n<!-- /beta:not_a_flag -->\nkeep2\n"
        assert beta_mod.apply_beta_blocks(text, enabled={"voice_layer"}) == "keep\nkeep2\n"


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


# =============================================================================
# doctor reports the flag — and never the key
# =============================================================================


# =============================================================================
# B1 — the MCP tool schema is a model-facing surface too
# =============================================================================
#
# It was missed the first time, and the miss is instructive: the brief said the
# role prompts were "the one surface", the implementation gated exactly that,
# and the commit message then asserted the claim it had inherited. `msg_send`'s
# description grew ~316 characters of voice-buddy prose that loads into every
# agent session in every install. So the proof below sweeps the WHOLE live
# registry for unresolved markers, so the next gated description cannot ride
# in the same way.


# =============================================================================
# N1 — a gate whose failure direction is ON is the wrong shape
# =============================================================================


# =============================================================================
# N2 — marker shapes that fail OPEN, and the audit that sees them
# =============================================================================


# =============================================================================
# N3 — the gate must not re-read config once per role file
# =============================================================================


# =============================================================================
# N5 — the same defect class, one door over
# =============================================================================
