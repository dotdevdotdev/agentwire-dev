"""`agentwire say` on the local-speaker path (#1198): one lane turn per
utterance, and the voice assistant's speech capped to a headline."""

import argparse

import pytest

from agentwire import channels_cli, playback_lane


@pytest.fixture
def local_say(tmp_path, monkeypatch):
    """Drive cmd_say down the no-browser path with a stub player."""
    played: list[str] = []
    toasts: list[str] = []
    monkeypatch.setattr(channels_cli, "load_config", lambda: {"tts": {"backend": "default"}})
    monkeypatch.setattr(channels_cli, "get_voice_from_config", lambda: None)
    monkeypatch.setattr(channels_cli, "_get_portal_url", lambda: "https://x")
    monkeypatch.setattr(channels_cli, "_check_portal_connections", lambda s, u: (False, s, 0))
    monkeypatch.setattr(channels_cli, "_handle_voice_notifications", lambda *a: None)
    monkeypatch.setattr(channels_cli, "_post_desktop_notification",
                        lambda text, **k: toasts.append(text) or True)
    monkeypatch.setattr(channels_cli, "_local_say_dispatch",
                        lambda chunk, *a, **k: (played.append(chunk) or 0, "local-speakers (kokoro)"))
    monkeypatch.setattr("agentwire.fleet_activity.note_spoke", lambda *a, **k: {})
    lane_dir = tmp_path / "lane"
    monkeypatch.setattr(playback_lane, "default_dir", lambda: lane_dir)
    return played, toasts, lane_dir


def _say(text, session, kind="say", display=None):
    return channels_cli.cmd_say(argparse.Namespace(
        text=[text], json=False, voice=None, exaggeration=None, cfg=None,
        session=session, kind=kind, display=display,
    ))


def test_the_voice_assistant_speaks_a_headline_and_shows_the_rest(local_say):
    played, toasts, _ = local_say
    long = ("Three sessions are busy. The rest is a long tail of detail that nobody "
            "wants read aloud to them.")
    assert _say(long, "agentwire-voice") == 0
    assert " ".join(played) == "Three sessions are busy."
    assert toasts == [long]


def test_other_sessions_are_not_capped(local_say):
    played, toasts, _ = local_say
    long = "One sentence here. " * 10
    _say(long, "some-session")
    assert " ".join(played).split() == long.split()
    assert toasts == []


def test_a_turn_the_lane_dropped_plays_nothing(local_say, monkeypatch):
    played, _, lane_dir = local_say
    lane = playback_lane.Lane(directory=lane_dir)
    head = lane.enqueue("say", "playing elsewhere", "x")
    lane.status(head)

    # While this ack waits its turn, the session's report arrives: the ack is
    # obsolete and must never reach the speakers.
    def report_arrives(_seconds):
        lane.enqueue("report", "Done.", "agentwire-voice", pid=999_999_999)

    lane.sleep = report_arrives
    monkeypatch.setattr(channels_cli, "Lane", lambda: lane)
    assert _say("On it.", "agentwire-voice", kind="ack") == 0
    assert played == []
    assert [i["kind"] for i in lane._read()["items"]] == ["say", "report"]
