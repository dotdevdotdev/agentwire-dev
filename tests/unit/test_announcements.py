"""The bundled announcements.json must parse and match the modal's schema.

It ships in the wheel as the offline fallback AND is the remote source the
portal fetches from `main`, so a malformed entry would reach every user.
This guards the shape the frontend (announcement-modal.js) renders.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

ANNOUNCEMENTS = Path(__file__).resolve().parents[2] / "agentwire" / "static" / "announcements.json"


@pytest.fixture(scope="module")
def data():
    assert ANNOUNCEMENTS.exists(), f"missing {ANNOUNCEMENTS}"
    return json.loads(ANNOUNCEMENTS.read_text())


def test_top_level_shape(data):
    assert isinstance(data, dict)
    assert isinstance(data.get("announcements"), list)


def test_ids_present_and_unique(data):
    ids = [a["id"] for a in data["announcements"]]
    assert all(isinstance(i, str) and i for i in ids), "every announcement needs a non-empty string id"
    assert len(ids) == len(set(ids)), "announcement ids must be unique (dedup is by id)"


def test_required_and_typed_fields(data):
    for a in data["announcements"]:
        assert isinstance(a.get("title"), str) and a["title"], f"{a.get('id')}: title required"
        assert isinstance(a.get("body"), str) and a["body"], f"{a.get('id')}: body required"
        if "highlights" in a:
            assert isinstance(a["highlights"], list)
            assert all(isinstance(h, str) for h in a["highlights"])
        if "emoji" in a:
            assert isinstance(a["emoji"], str)
        if "date" in a:
            assert isinstance(a["date"], str)


def test_cta_is_safe_https(data):
    for a in data["announcements"]:
        cta = a.get("cta")
        if cta is None:
            continue
        assert isinstance(cta.get("label"), str) and cta["label"], f"{a['id']}: cta needs a label"
        scheme = urlparse(cta.get("url", "")).scheme
        assert scheme in ("http", "https"), f"{a['id']}: cta url must be http(s)"
