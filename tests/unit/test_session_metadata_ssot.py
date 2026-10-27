"""``session_metadata_path`` must be the SSOT in fact, not by declaration (#899).

The helper's docstring claimed "one implementation, both directions" while
``load_session_metadata`` and ``agentwire kill`` each rebuilt the path by hand.
That is this repo's most-repeated defect class — ``tmux_safe_name`` took four
rounds (#865 → #868 → #870 → #878) and ``encode_project_path`` shipped a second
incorrect copy beside its callers (#892). In every case the helper existed and
some callers simply did not route through it, and in every case the divergence
was invisible until something behaved wrongly in production.

The pin is behavioural rather than textual: redirect the helper, and every
reader and writer must follow it. A caller that hand-builds the path keeps
writing to the old location and fails here.
"""


import pytest

from agentwire import core


@pytest.fixture
def redirected(tmp_path, monkeypatch):
    """Point the SSOT somewhere unguessable by string-building."""
    store = tmp_path / "somewhere-else"

    def fake_path(session_name: str):
        return store / f"{session_name.split('@')[0]}.json"

    monkeypatch.setattr(core, "session_metadata_path", fake_path)
    return store


class TestContainment:
    """A session name is operator input, and ``kill`` UNLINKS what this returns.

    Pre-existing — the inlined copy in ``pane_cli`` had identical properties —
    but consolidating is the moment the check can be written once rather than
    at four call sites, so it lands with the consolidation.
    """

    def test_traversal_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
        escaped = []
        for evil in ("../../../evil", "..", "a/../../b", "foo/../../../bar"):
            try:
                core.session_metadata_path(evil)
            except ValueError:
                continue
            escaped.append(evil)
        assert not escaped, escaped
