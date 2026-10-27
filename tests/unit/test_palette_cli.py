"""Tests for ``agentwire palette`` — user-defined command-palette items (#676).

Covers config validation (ids, fields, undeclared placeholders), and the run
path: template substitution with shell-quoted field values, defaults, missing
values, unknown items/fields, and exit-code propagation.
"""

import json
from types import SimpleNamespace

import pytest

from agentwire import palette_cli
from agentwire.palette_cli import cmd_palette_run


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










def test_run_propagates_failure_exit(config, capsys):
    config([{"id": "f", "label": "F", "run": "sh -c 'echo boom >&2; exit 3'"}])
    assert cmd_palette_run(_args(id="f")) == 1
    out = _run_json(capsys)
    assert not out["success"] and out["exit_code"] == 3 and "boom" in out["output"]
