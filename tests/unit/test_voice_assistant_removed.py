"""The portal voice assistant is gone (#1230); the voice surface that remains
(TTS, STT, say, per-session push-to-talk) must still be wired."""

from agentwire.__main__ import build_parser


def test_the_assistant_command_is_gone_and_the_voice_surface_remains():
    parser = build_parser()
    top = next(a for a in parser._actions if hasattr(a, "choices") and a.choices and "new" in a.choices)
    assert "voice" not in top.choices

    bad = []
    for argv in (["tts", "status"], ["stt", "status"], ["say", "hello"],
                 ["list", "--sessions", "--state"], ["new", "-s", "x", "--effort", "high"]):
        try:
            parser.parse_args(argv)
        except SystemExit:
            bad.append(argv)
    assert not bad, bad


def test_the_voice_role_still_renders_and_the_assistant_role_does_not():
    from agentwire.roles import load_roles

    roles, missing = load_roles(["voice", "voice-assistant"])
    assert [r.name for r in roles] == ["voice"] and missing == ["voice-assistant"]
