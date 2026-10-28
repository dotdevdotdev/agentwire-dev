"""The env-file loader (#1184): an EMPTY inherited variable must not shadow the file.

python-dotenv's ``load_dotenv`` treats ``export OPENAI_API_KEY=`` as "already
set" and skips the file's real value — which is how the buddy reported the key
missing while it sat in ~/.agentwire/.env. ``os.environ`` is replaced by a
plain dict so nothing here touches the real environment or any real file.
"""

import os

import pytest

from agentwire import env_loader
from agentwire.voice_layer import realtime


@pytest.fixture
def environ(monkeypatch):
    env: dict = {}
    monkeypatch.setattr(os, "environ", env)
    return env


def _write(tmp_path, body: str):
    path = tmp_path / "dotenv"
    path.write_text(body)
    return path


def test_empty_inherited_value_does_not_shadow_a_real_file_value(environ, tmp_path):
    environ.update({"EMPTY": "", "BLANK": "   ", "SET": "inherited", "BOTH_EMPTY": ""})
    path = _write(tmp_path, "EMPTY=fromfile\nBLANK=fromfile\nSET=fromfile\n"
                            "ABSENT=fromfile\nBOTH_EMPTY=\n")

    unshadowed = env_loader.load_env_file(path)

    assert {k: environ.get(k) for k in ("EMPTY", "BLANK", "SET", "ABSENT", "BOTH_EMPTY")} == {
        "EMPTY": "fromfile",       # the bug: an empty export used to win
        "BLANK": "fromfile",
        "SET": "inherited",        # a real inherited value still wins
        "ABSENT": "fromfile",
        "BOTH_EMPTY": "",          # nothing better to fill it with
    }
    assert sorted(unshadowed) == ["BLANK", "EMPTY"]


def test_missing_file_is_a_no_op(environ, tmp_path):
    environ["EMPTY"] = ""
    assert env_loader.load_env_file(tmp_path / "nope") == []
    assert env_loader.load_env_file("") == []
    assert environ.get("EMPTY") == "" and "nope" not in environ


def test_not_set_error_says_when_an_empty_export_is_what_it_found(environ):
    key = realtime.API_KEY_ENV

    with pytest.raises(realtime.RealtimeError) as absent:
        realtime.api_key()
    environ[key] = ""
    with pytest.raises(realtime.RealtimeError) as empty:
        realtime.api_key()

    assert "is not set" in str(absent.value) and "empty" not in str(absent.value)
    assert "empty string" in str(empty.value)
    assert f"export {key}=" in str(empty.value)
