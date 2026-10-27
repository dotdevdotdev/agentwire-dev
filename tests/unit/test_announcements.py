"""The bundled announcements.json must parse and match the modal's schema.

It ships in the wheel as the offline fallback AND is the remote source the
portal fetches from `main`, so a malformed entry would reach every user.
This guards the shape the frontend (announcement-modal.js) renders.
"""

import json
from pathlib import Path

import pytest

ANNOUNCEMENTS = Path(__file__).resolve().parents[2] / "agentwire" / "static" / "announcements.json"


@pytest.fixture(scope="module")
def data():
    assert ANNOUNCEMENTS.exists(), f"missing {ANNOUNCEMENTS}"
    return json.loads(ANNOUNCEMENTS.read_text())


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
