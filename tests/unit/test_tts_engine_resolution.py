"""Tests for TTS engine resolution — _get_tts_engine / _get_venv_for_backend.

The top-level tts.backend is the tier (default|custom); the engine to run
lives in tts.options.backend (issue #261).
"""

from argparse import Namespace

from agentwire.tts_cli import _get_tts_engine


class TestGetTtsEngine:

    def test_cli_flag_wins_over_options(self):
        tts_config = {"backend": "custom", "options": {"backend": "kokoro"}}
        args = Namespace(backend="zonos-transformer")
        assert _get_tts_engine(args, tts_config) == "zonos-transformer"
