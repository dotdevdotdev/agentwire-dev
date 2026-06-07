"""Tests for agentwire.utils.speech.strip_speech_tags."""

from agentwire.utils.speech import strip_speech_tags


class TestStripSpeechTags:
    def test_plain_text_untouched(self):
        assert strip_speech_tags("hello world") == "hello world"

    def test_bracket_tag_mid_sentence(self):
        assert strip_speech_tags("well [laughter] that worked") == "well that worked"

    def test_leading_tag(self):
        assert strip_speech_tags("[sigh] fine, I'll do it") == "fine, I'll do it"

    def test_multi_word_tag(self):
        assert strip_speech_tags("ok [clears throat] listen up") == "ok listen up"

    def test_angle_emotion_tag(self):
        assert strip_speech_tags("I'm <emotion:happy> thrilled") == "I'm thrilled"

    def test_multiple_tags(self):
        assert (
            strip_speech_tags("[chuckle] sure <style:whisper> why not [gasp]")
            == "sure why not"
        )

    def test_whitespace_collapsed(self):
        assert strip_speech_tags("a [laugh]  b") == "a b"

    def test_code_ish_brackets_preserved(self):
        # Uppercase, digits, punctuation inside brackets → not speech markup
        assert strip_speech_tags("run pytest[3] now") == "run pytest[3] now"
        assert strip_speech_tags("[User said: 'hi']") == "[User said: 'hi']"
        assert strip_speech_tags("list[int] is fine") == "list[int] is fine"

    def test_empty_string(self):
        assert strip_speech_tags("") == ""

    def test_only_tags(self):
        assert strip_speech_tags("[laugh] [sigh]") == ""
