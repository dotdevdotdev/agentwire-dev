"""Speech text utilities shared by the portal server and CLI."""

import re

# Inline speech markup that expressive TTS models understand but plain
# backends must never speak literally:
#   [laughter] [sigh] [chuckle] ...      — lowercase word(s) in brackets
#   <emotion:happy> <style:whisper> ...  — tag:value in angle brackets
# Conservative on purpose: tags must be standalone tokens (whitespace or
# string boundary on both sides), and brackets containing digits, uppercase,
# or punctuation are left alone — so code like `list[int]`, citations, and
# "[User said: ...]" wrappers survive.
_TAG_RE = re.compile(
    r"(?<![^\s])"                              # start of string or whitespace before
    r"(?:\[[a-z][a-z _-]{0,30}\]|<[a-z]+:[^>]{0,60}>)"
    r"(?![^\s.,!?;:])"                         # whitespace, end, or punctuation after
)


def strip_speech_tags(text: str) -> str:
    """Remove inline speech markup so plain backends don't read it aloud."""
    stripped = _TAG_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", stripped).strip()
