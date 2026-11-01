"""`agentwire say` on the local-speaker path (#1198): one lane turn per
utterance, spoken in full."""

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


def _say(text, session, display=None):
    return channels_cli.cmd_say(argparse.Namespace(
        text=[text], json=False, voice=None, exaggeration=None, cfg=None,
        session=session, display=display,
    ))


def test_say_plays_the_full_text_for_any_session_name(local_say):
    played, toasts, _ = local_say
    long = ("Three sessions are busy. The rest is a long tail of detail that nobody "
            "wants read aloud to them. ")
    capped = []
    for name in ("agentwire-voice", "agentwire-buddy", "some-session"):
        played.clear()
        _say(long, name)
        if " ".join(played).split() != long.split():
            capped.append(name)
    assert not capped, capped
    assert toasts == []


def test_a_turn_the_lane_dropped_plays_nothing(local_say, monkeypatch):
    played, _, lane_dir = local_say
    lane = playback_lane.Lane(directory=lane_dir)
    head = lane.enqueue("playing elsewhere", "x")
    lane.status(head)

    # While this say waits its turn, newer utterances arrive and push the
    # backlog over budget: it is the oldest, so it is dropped and must never
    # reach the speakers.
    def backlog_grows(_seconds):
        lane.enqueue(" ".join(["word"] * 60), "y", pid=999_999_999)

    lane.sleep = backlog_grows
    monkeypatch.setattr(channels_cli, "Lane", lambda: lane)
    assert _say("On it.", "some-session") == 0
    assert played == []
