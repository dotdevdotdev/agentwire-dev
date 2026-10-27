"""Tests for agentwire/templating.py — Template expansion and variables."""

import pytest

from agentwire.templating import (
    TemplateContext,
    TemplateError,
    expand_template,
)

# --- TemplateContext ---


# --- expand_template ---


class TestExpandTemplate:
    def test_undefined_raises_error(self):
        ctx = TemplateContext()
        with pytest.raises(TemplateError, match="Undefined variable"):
            expand_template("{{ nonexistent_var }}", ctx)


# --- expand_env_vars ---


# --- expand_all ---


# --- preview_template ---
