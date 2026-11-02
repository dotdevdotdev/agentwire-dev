"""The suite judges by AgentWire's profile; nothing may leave the generic one active (#1209 M5).

Runs after the installer tests (alphabetical order is the point): they call
``engine.install_profile`` through the CLI, which no monkeypatch can undo.
"""

from damage_control import engine


def test_the_agentwire_profile_is_still_active():
    assert engine.PROFILE.name == "agentwire", engine.PROFILE.name
