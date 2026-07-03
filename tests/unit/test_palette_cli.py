"""Tests for ``agentwire palette`` — user-defined command-palette items (#676).

Covers config validation (ids, fields, undeclared placeholders), and the run
path: template substitution with shell-quoted field values, defaults, missing
values, unknown items/fields, and exit-code propagation.
"""

import json
from types import SimpleNamespace

import pytest

from agentwire import palette_cli
from agentwire.palette_cli import cmd_palette_list, cmd_palette_run, load_palette_items


@pytest.fixture
def config(monkeypatch):
    holder = {}
    monkeypatch.setattr(palette_cli, "load_config", lambda: holder.get("cfg", {}))
    def set_items(items):
        holder["cfg"] = {"palette": {"items": items}}
    return set_items


def _args(**kw):
    defaults = dict(json=True, field=None, id="")
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# --- validation --------------------------------------------------------------

def test_empty_config(config):
    config([])
    items, errors = load_palette_items()
    assert items == [] and errors == []


def test_valid_item_normalized(config):
    config([{
        "id": "quicktask", "label": "Quick task", "icon": "⚡",
        "keywords": "quick", "run": "echo {name}",
        "fields": [{"name": "name", "label": "Name", "default": "x"}],
    }])
    items, errors = load_palette_items()
    assert errors == []
    assert items[0]["id"] == "quicktask"
    assert items[0]["fields"] == [{"name": "name", "label": "Name", "default": "x"}]


@pytest.mark.parametrize("bad", [
    {"label": "no id", "run": "echo hi"},
    {"id": "bad id!", "label": "x", "run": "echo hi"},
    {"id": "x", "run": "echo hi"},                         # no label
    {"id": "x", "label": "x"},                             # no run
    {"id": "x", "label": "x", "run": "echo {oops}"},       # undeclared placeholder
    {"id": "x", "label": "x", "run": "echo hi", "fields": [{"name": "1bad"}]},
])
def test_invalid_items_reported_not_fatal(config, bad):
    config([bad, {"id": "ok", "label": "OK", "run": "echo hi"}])
    items, errors = load_palette_items()
    assert [it["id"] for it in items] == ["ok"]
    assert len(errors) == 1


def test_duplicate_ids_rejected(config):
    config([
        {"id": "a", "label": "A", "run": "echo 1"},
        {"id": "a", "label": "A2", "run": "echo 2"},
    ])
    items, errors = load_palette_items()
    assert len(items) == 1 and "duplicate" in errors[0]


def test_list_json_output(config, capsys):
    config([{"id": "a", "label": "A", "run": "echo 1"}])
    assert cmd_palette_list(_args()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["success"] and out["items"][0]["id"] == "a"


# --- run ---------------------------------------------------------------------

def _run_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_run_substitutes_and_quotes(config, capsys):
    config([{
        "id": "greet", "label": "Greet", "run": "echo hello {who}",
        "fields": [{"name": "who"}],
    }])
    rc = cmd_palette_run(_args(id="greet", field=["who=world; rm x"]))
    out = _run_json(capsys)
    assert rc == 0 and out["success"]
    # Injection attempt stays a literal argument, not shell syntax.
    assert out["output"].strip() == "hello world; rm x"


def test_run_uses_defaults(config, capsys):
    config([{
        "id": "d", "label": "D", "run": "echo {v}",
        "fields": [{"name": "v", "default": "fallback"}],
    }])
    assert cmd_palette_run(_args(id="d")) == 0
    assert _run_json(capsys)["output"].strip() == "fallback"


def test_run_missing_field_value(config, capsys):
    config([{"id": "m", "label": "M", "run": "echo {v}", "fields": [{"name": "v"}]}])
    assert cmd_palette_run(_args(id="m")) == 1
    out = _run_json(capsys)
    assert not out["success"] and "Missing value" in out["error"]


def test_run_unknown_item(config, capsys):
    config([])
    assert cmd_palette_run(_args(id="nope")) == 1
    assert "Unknown palette item" in _run_json(capsys)["error"]


def test_run_unknown_field(config, capsys):
    config([{"id": "a", "label": "A", "run": "echo hi"}])
    assert cmd_palette_run(_args(id="a", field=["bogus=1"])) == 1
    assert "Unknown field" in _run_json(capsys)["error"]


def test_run_propagates_failure_exit(config, capsys):
    config([{"id": "f", "label": "F", "run": "sh -c 'echo boom >&2; exit 3'"}])
    assert cmd_palette_run(_args(id="f")) == 1
    out = _run_json(capsys)
    assert not out["success"] and out["exit_code"] == 3 and "boom" in out["output"]
