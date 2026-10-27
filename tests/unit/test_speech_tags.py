"""Tests for agentwire.utils.speech.strip_speech_tags."""

from agentwire.utils.speech import strip_speech_tags


class TestStripSpeechTags:
    def test_bracket_tag_mid_sentence(self):
        assert strip_speech_tags("well [laughter] that worked") == "well that worked"
