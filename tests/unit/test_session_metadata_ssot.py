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

import json

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


class TestEveryCallerRoutesThroughTheHelper:
    def test_store_writes_where_the_helper_says(self, redirected):
        core.store_session_metadata("alpha", {"role": "worker"})
        assert (redirected / "alpha.json").is_file()

    def test_load_reads_where_the_helper_says(self, redirected):
        core.store_session_metadata("alpha", {"role": "worker"})
        assert core.load_session_metadata("alpha") == {"role": "worker"}

    def test_load_does_not_fall_back_to_a_hand_built_path(self, redirected, monkeypatch):
        """The exact failure: a reader that ignores the helper.

        Written to the redirected store, but a *different* record is planted at
        the conventional location. A hand-building reader returns the planted
        one; a routed reader returns the real one.
        """
        core.store_session_metadata("alpha", {"role": "worker"})

        decoy = core.CONFIG_DIR / "sessions" / "alpha" / "metadata.json"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text(json.dumps({"role": "DECOY"}))

        assert core.load_session_metadata("alpha")["role"] == "worker"

    def test_kill_removes_the_record_the_helper_names(self, redirected, monkeypatch):
        """``agentwire kill`` unlinks the record; it must unlink the real one."""
        import subprocess

        from agentwire import pane_cli

        core.store_session_metadata("alpha", {"role": "worker"})
        assert (redirected / "alpha.json").is_file()

        decoy = core.CONFIG_DIR / "sessions" / "alpha" / "metadata.json"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text(json.dumps({"role": "DECOY"}))

        monkeypatch.setattr(pane_cli, "session_metadata_path", fake := core.session_metadata_path,
                            raising=False)
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
        pane_cli._drop_session_metadata("alpha")

        assert not (redirected / "alpha.json").exists(), "kill unlinked the wrong path"
        assert decoy.exists(), "kill removed a record it does not own"
        assert fake is core.session_metadata_path

    def test_machine_suffix_is_stripped_once_in_one_place(self, tmp_path, monkeypatch):
        """``name@machine`` keys the same record as ``name``.

        Deliberately NOT using the ``redirected`` fixture: its stand-in does
        its own ``split("@")``, so the assertion would pass even if the real
        helper stopped stripping. Exercises the genuine implementation instead.
        """
        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)

        assert core.session_metadata_path("alpha@remote") == core.session_metadata_path("alpha")
        assert core.session_metadata_path("alpha").parent.name == "alpha"

        core.store_session_metadata("alpha@remote", {"role": "worker"})
        assert core.load_session_metadata("alpha") == {"role": "worker"}
        assert core.load_session_metadata("alpha@other") == {"role": "worker"}


class TestSessionsDirIsAlsoShared:
    """The leaf had a helper; the ROOT was still built by hand in two places."""

    def test_enumeration_follows_the_shared_root(self, tmp_path, monkeypatch):
        from agentwire import history_migrate

        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
        root = core.sessions_dir()
        for name in ("beta", "gamma"):
            (root / name).mkdir(parents=True)
            (root / name / "metadata.json").write_text("{}")

        assert history_migrate.known_sessions() == ["beta", "gamma"]

    def test_sessions_dir_agrees_with_the_metadata_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
        assert core.session_metadata_path("alpha").parent.parent == core.sessions_dir()


def test_no_module_hand_builds_the_metadata_path():
    """Structural backstop: catch a NEW hand-built copy at review time.

    The behavioural tests above only cover callers that exist. This one fails
    the moment someone writes ``CONFIG_DIR / "sessions" / ...`` again, which is
    how every prior round of this defect got in.
    """
    import pathlib
    import re

    # A Path join onto "sessions" — not the bare word, which appears all over
    # the portal's JSON payloads and route names.
    join = re.compile(r'/\s*"sessions"')
    root = pathlib.Path(core.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not join.search(line) or line.lstrip().startswith("#"):
                continue
            # Only the ONE definition is exempt. This used to exempt any
            # core.py line containing the pattern, which is how #898's
            # recorded_sessions() slipped in with its own hand-built root —
            # a blanket exemption on the file that owns the SSOT is a hole
            # exactly where the SSOT lives.
            if path.name == "core.py" and line.strip() == 'return CONFIG_DIR / "sessions"':
                continue
            offenders.append(f"{path.relative_to(root.parent)}:{number}: {line.strip()}")
    assert not offenders, (
        "hand-built session metadata path — route through core.sessions_dir() / "
        "core.session_metadata_path() instead:\n  " + "\n  ".join(offenders)
    )
