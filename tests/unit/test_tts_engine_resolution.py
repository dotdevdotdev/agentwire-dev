"""Tests for TTS engine resolution — _get_tts_engine / _get_venv_for_backend.

The top-level tts.backend is the tier (default|custom); the engine to run
lives in tts.options.backend (issue #261).
"""

from argparse import Namespace

from agentwire.tts_cli import _get_tts_engine, _get_venv_for_backend


class TestGetTtsEngine:
    def test_reads_options_backend(self):
        tts_config = {"backend": "custom", "options": {"backend": "kokoro"}}
        args = Namespace(backend=None)
        assert _get_tts_engine(args, tts_config) == "kokoro"

    def test_cli_flag_wins_over_options(self):
        tts_config = {"backend": "custom", "options": {"backend": "kokoro"}}
        args = Namespace(backend="zonos-transformer")
        assert _get_tts_engine(args, tts_config) == "zonos-transformer"

    def test_tier_value_is_never_treated_as_engine(self):
        # backend: custom with no options.backend → default engine, not "custom"
        tts_config = {"backend": "custom", "url": "http://localhost:8100"}
        args = Namespace(backend=None)
        assert _get_tts_engine(args, tts_config) == "chatterbox"

    def test_options_none_in_yaml(self):
        # "options:" with no keys parses to None
        tts_config = {"backend": "custom", "options": None}
        args = Namespace(backend=None)
        assert _get_tts_engine(args, tts_config) == "chatterbox"

    def test_empty_config(self):
        assert _get_tts_engine(Namespace(backend=None), {}) == "chatterbox"

    def test_args_without_backend_attr(self):
        tts_config = {"options": {"backend": "zonos-hybrid"}}
        assert _get_tts_engine(Namespace(), tts_config) == "zonos-hybrid"


class TestVenvForResolvedEngine:
    def test_kokoro_engine_maps_to_kokoro_venv(self):
        # The #261 failure: custom tier + kokoro engine landed in the wrong venv
        engine = _get_tts_engine(
            Namespace(backend=None),
            {"backend": "custom", "options": {"backend": "kokoro"}},
        )
        assert _get_venv_for_backend(engine) == "kokoro"

    def test_default_engine_maps_to_chatterbox_venv(self):
        engine = _get_tts_engine(Namespace(backend=None), {"backend": "custom"})
        assert _get_venv_for_backend(engine) == "chatterbox"
