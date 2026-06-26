"""Tests for agentwire/templating.py — Template expansion and variables."""

import os
import re

import pytest

from agentwire.templating import (
    TemplateContext,
    TemplateError,
    expand_all,
    expand_env_vars,
    expand_template,
    preview_template,
)

# --- TemplateContext ---

class TestTemplateContext:
    def test_builtin_date_time(self):
        ctx = TemplateContext()
        date = ctx.get("date")
        assert date is not None
        assert re.match(r"\d{4}-\d{2}-\d{2}", date)

        time_val = ctx.get("time")
        assert time_val is not None
        assert re.match(r"\d{2}:\d{2}:\d{2}", time_val)

        dt = ctx.get("datetime")
        assert dt is not None
        assert "T" in dt

    def test_attributes_as_str(self):
        ctx = TemplateContext(session="my-session", task="my-task", attempt=3)
        assert ctx.get("session") == "my-session"
        assert ctx.get("task") == "my-task"
        assert ctx.get("attempt") == "3"

    def test_pre_outputs(self):
        ctx = TemplateContext()
        ctx.set_pre_output("weather", "Sunny 72F")
        assert ctx.get("weather") == "Sunny 72F"

    def test_unknown_returns_none(self):
        ctx = TemplateContext()
        assert ctx.get("nonexistent") is None

    def test_has_known(self):
        ctx = TemplateContext(session="test")
        assert ctx.has("session") is True
        assert ctx.has("date") is True
        assert ctx.has("nonexistent") is False

    def test_empty_string_attribute_returns_none(self):
        # Empty strings return None (they're falsy for has())
        ctx = TemplateContext(status="")
        # status is "" which is falsy — get returns None for empty attrs
        # Actually let's check: hasattr returns True, value is "", str("") is ""
        # But the code checks `if value is not None: return str(value)`
        # "" is not None, so it returns "". Let me check...
        val = ctx.get("status")
        assert val == ""  # empty string is not None


# --- expand_template ---

class TestExpandTemplate:
    def test_simple_substitution(self):
        ctx = TemplateContext(session="myapp", task="build")
        result = expand_template("Session: {{ session }}, Task: {{ task }}", ctx)
        assert result == "Session: myapp, Task: build"

    def test_whitespace_tolerance(self):
        ctx = TemplateContext(session="myapp")
        assert expand_template("{{session}}", ctx) == "myapp"
        assert expand_template("{{ session }}", ctx) == "myapp"
        assert expand_template("{{  session  }}", ctx) == "myapp"

    def test_undefined_raises_error(self):
        ctx = TemplateContext()
        with pytest.raises(TemplateError, match="Undefined variable"):
            expand_template("{{ nonexistent_var }}", ctx)

    def test_pre_output_expansion(self):
        ctx = TemplateContext()
        ctx.set_pre_output("weather", "Rainy")
        result = expand_template("Forecast: {{ weather }}", ctx)
        assert result == "Forecast: Rainy"

    def test_multiple_vars(self):
        ctx = TemplateContext(session="app", task="test")
        ctx.set_pre_output("data", "OK")
        result = expand_template("{{ session }}/{{ task }}: {{ data }}", ctx)
        assert result == "app/test: OK"


# --- expand_env_vars ---

class TestExpandEnvVars:
    def test_defined_var_replaced(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "hello-world")
        result = expand_env_vars("Value: ${MY_TEST_VAR}")
        assert result == "Value: hello-world"

    def test_undefined_passes_through(self):
        # Ensure it doesn't exist
        os.environ.pop("DEFINITELY_UNSET_XYZZY", None)
        result = expand_env_vars("Value: ${DEFINITELY_UNSET_XYZZY}")
        assert result == "Value: ${DEFINITELY_UNSET_XYZZY}"

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        result = expand_env_vars("${A} and ${B}")
        assert result == "1 and 2"


# --- expand_all ---

class TestExpandAll:
    def test_both_types(self, monkeypatch):
        monkeypatch.setenv("HOME_DIR", "/home/user")
        ctx = TemplateContext(session="test")
        result = expand_all("{{ session }} at ${HOME_DIR}", ctx)
        assert result == "test at /home/user"

    def test_template_error_before_env(self):
        ctx = TemplateContext()
        with pytest.raises(TemplateError):
            expand_all("{{ missing }} and ${WHATEVER}", ctx)


# --- preview_template ---

class TestPreviewTemplate:
    def test_undefined_shows_placeholder(self):
        result = preview_template("Weather: {{ weather }}")
        assert result == "Weather: <pre:weather>"

    def test_defined_vars_expanded(self):
        ctx = TemplateContext(session="app")
        result = preview_template("Session: {{ session }}, Data: {{ data }}", ctx)
        assert "Session: app" in result
        assert "<pre:data>" in result

    def test_none_ctx_uses_empty(self):
        result = preview_template("{{ session }}", None)
        # session is "" in empty context — empty string is not None, so it should expand
        # Actually TemplateContext() has session="" which is not None...
        # Let me check: ctx.get("session") -> hasattr(self, "session") is True,
        # value = "" which is not None, so returns str("") = ""
        assert result == ""

    def test_no_error_on_undefined(self):
        # Should not raise, unlike expand_template
        result = preview_template("{{ unknown_var }}")
        assert "<pre:unknown_var>" in result
