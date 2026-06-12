"""Tests for config validation (agentwire doctor's checks)."""

from agentwire.config import load_config
from agentwire.validation import validate_config


def _validate(tmp_path, mutate=None):
    config = load_config(tmp_path / "nonexistent.yaml")
    if mutate:
        mutate(config)
    machines_file = tmp_path / "machines.json"
    machines_file.write_text('{"machines": []}')
    return validate_config(config, machines_file)


class TestUrlValidation:
    def test_default_tts_tier_has_no_url_and_no_warning(self, tmp_path):
        # Regression: doctor warned "URL missing scheme in tts" on every
        # default-tier install because tts.url is None there by design.
        warnings, errors = _validate(tmp_path)
        assert not [w for w in warnings if "tts" in w.message], [
            w.message for w in warnings
        ]
        assert errors == []

    def test_custom_tts_url_without_scheme_still_warns(self, tmp_path):
        # (urlparse quirk: "localhost:8100" parses 'localhost' as a scheme,
        # so use a host that can't be one.)
        def mutate(config):
            config.tts.backend = "custom"
            config.tts.url = "192.168.2.50:8100"

        warnings, _ = _validate(tmp_path, mutate)
        assert any(
            "URL missing scheme in tts" in w.message for w in warnings
        ), [w.message for w in warnings]

    def test_default_portal_url_is_clean(self, tmp_path):
        warnings, _ = _validate(tmp_path)
        assert not [w for w in warnings if "portal" in w.message]
