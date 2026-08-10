"""The beta gate that lets the voice layer ship to main (owner ruling 2026-08-10).

The acceptance bar is exact and it is the reason this file exists: **with the
flag off, a non-voice user's rendered system prompt must be byte-identical to
what ``origin/main`` produces today.** Proved against a snapshot of main's role
files (``tests/fixtures/main_roles/``), never by inspection — the whole failure
mode is text that *looks* the same.

The snapshot is itself checked against ``origin/main`` when git can reach that
ref, so the fixture cannot quietly drift into agreeing with the branch.
"""

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from agentwire import beta as beta_mod
from agentwire import config as config_mod
from agentwire import roles as roles_mod

ROLES_DIR = Path(roles_mod.__file__).parent
FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "main_roles"

#: Every role file this branch touched. The list is the claim: any other role
#: file the voice layer edits later must be added here or the bar is not met.
GATED_ROLE_FILES = ["agentwire.md", "orchestrator.md", "worker.md", "worker-worktree.md"]


@pytest.fixture(autouse=True)
def _reset_beta_cache():
    """The flag set is cached for the life of the process (N3), so every test
    here brackets itself: patching ``enabled_beta_flags`` is invisible behind a
    warm cache, and a cache warmed under a patch would leak into the next test.
    """
    beta_mod.reset_cache()
    yield
    beta_mod.reset_cache()


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def _shipped(name: str) -> str:
    return (ROLES_DIR / name).read_text()


class TestFixtureIsHonest:
    """The snapshot must be main's text, not a copy of the branch's."""

    def test_every_fixture_matches_origin_main(self):
        """**Expect this to SKIP in CI**, and do not read a green run as this
        check having passed there: ``actions/checkout`` clones at depth 1 with
        no other ref, so ``git show origin/main:…`` fails and the skip fires
        (37 skipped in CI vs 36 locally). It is a local-development guard
        against hand-editing the fixture.

        What runs everywhere is ``test_the_fixture_is_not_just_the_branch``
        below — the control that would catch the failure that actually matters,
        a fixture regenerated from this branch — plus every byte-identity
        assertion against the committed fixture itself.
        """
        for name in GATED_ROLE_FILES:
            proc = subprocess.run(
                ["git", "show", f"origin/main:agentwire/roles/{name}"],
                cwd=Path(__file__).parent.parent.parent,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                pytest.skip("origin/main not fetched in this checkout")
            assert proc.stdout == _fixture(name), (
                f"tests/fixtures/main_roles/{name} has drifted from origin/main — "
                "regenerate it, do not edit it by hand"
            )

    def test_the_fixture_is_not_just_the_branch(self):
        """A control: without it, a fixture regenerated from the BRANCH would
        make every byte-identity assertion below pass while proving nothing."""
        differing = [n for n in GATED_ROLE_FILES if _fixture(n) != _shipped(n)]
        assert differing == GATED_ROLE_FILES, (
            "every gated role file should differ from main's text on this branch"
        )


class TestFlagOffIsByteIdenticalToMain:
    def test_raw_role_text_with_the_gate_off_equals_main(self):
        for name in GATED_ROLE_FILES:
            rendered = beta_mod.apply_beta_blocks(_shipped(name), enabled=set())
            assert rendered == _fixture(name), f"{name} is not byte-identical to main"

    def test_parsed_instructions_with_the_gate_off_equal_main(self, monkeypatch):
        """The prompt as it actually renders — through ``parse_role_file``,
        which is what every role reader in the tree goes through."""
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        for name in GATED_ROLE_FILES:
            shipped = roles_mod.parse_role_file(ROLES_DIR / name)
            expected = roles_mod.parse_role_file(FIXTURE_DIR / name)
            assert shipped.instructions == expected.instructions, name

    def test_merged_prompt_with_the_gate_off_equals_main(self, monkeypatch):
        """Merged across a real role set — a per-file check cannot see a
        separator the merge introduces."""
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        names = ["agentwire.md", "worker-worktree.md"]
        shipped = roles_mod.merge_roles(
            [roles_mod.parse_role_file(ROLES_DIR / n) for n in names]
        )
        expected = roles_mod.merge_roles(
            [roles_mod.parse_role_file(FIXTURE_DIR / n) for n in names]
        )
        assert shipped.instructions == expected.instructions

    def test_no_marker_comment_survives_into_the_prompt(self, monkeypatch):
        """Off *or* on, the markers are scaffolding — shipping them would cost
        the tokens the gate exists to save, and read as noise to the model."""
        for flags in (set(), {"voice_layer"}):
            monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda f=flags: f)
            beta_mod.reset_cache()
            for name in GATED_ROLE_FILES:
                text = roles_mod.parse_role_file(ROLES_DIR / name).instructions
                assert "beta:" not in text, f"{name} leaked a marker with flags={flags}"


class TestFlagOnRestoresTheVoiceLines:
    def test_the_voice_sections_are_present_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        for name in ["orchestrator.md", "worker.md", "worker-worktree.md"]:
            text = roles_mod.parse_role_file(ROLES_DIR / name).instructions
            assert "Replying to the voice buddy" in text, name
            assert "msg send --to buddy --kind done" in text, name

    def test_enabling_the_flag_changes_the_prompt(self, monkeypatch):
        """The must-fail control for the byte-identity tests above: if the gate
        stripped nothing, off and on would render the same and every assertion
        in this file would be vacuous."""
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        beta_mod.reset_cache()
        off = {n: roles_mod.parse_role_file(ROLES_DIR / n).instructions for n in GATED_ROLE_FILES}
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        beta_mod.reset_cache()
        on = {n: roles_mod.parse_role_file(ROLES_DIR / n).instructions for n in GATED_ROLE_FILES}
        assert all(off[n] != on[n] for n in GATED_ROLE_FILES)


class TestMarkerMechanics:
    def test_an_unknown_flag_fails_closed(self):
        """A marker naming a flag nothing knows about removes its block. The
        other direction — shipping text no gate can turn off — is the failure
        this whole file is about."""
        text = "keep\n<!-- beta:not_a_flag -->\ndrop\n<!-- /beta:not_a_flag -->\nkeep2\n"
        assert beta_mod.apply_beta_blocks(text, enabled={"voice_layer"}) == "keep\nkeep2\n"

    def test_every_marker_in_the_shipped_roles_names_a_known_flag(self):
        """Pairs with the rule above: fail-closed is only safe if a typo is
        caught here rather than silently deleting a section forever."""
        for path in sorted(ROLES_DIR.glob("*.md")):
            for flag, _body in beta_mod.BETA_BLOCK_RE.findall(path.read_text()):
                assert flag in beta_mod.flag_names(), (
                    f"{path.name}: unknown beta flag {flag!r}"
                )

    def test_an_unclosed_marker_is_left_alone(self):
        text = "a\n<!-- beta:voice_layer -->\nb\n"
        assert beta_mod.apply_beta_blocks(text, enabled=set()) == text


class TestConfigFlag:
    def test_default_is_off(self, tmp_path):
        cfg = config_mod.load_config(tmp_path / "nonexistent.yaml")
        assert cfg.beta.voice_layer is False

    def test_reads_the_key_from_yaml(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("beta:\n  voice_layer: true\n")
        assert config_mod.load_config(p).beta.voice_layer is True

    def test_enabled_flags_reflects_the_config(self, tmp_path, monkeypatch):
        p = tmp_path / "config.yaml"
        p.write_text("beta:\n  voice_layer: true\n")
        monkeypatch.setattr(config_mod, "default_config_path", lambda: p)
        assert config_mod.enabled_beta_flags() == {"voice_layer"}

    def test_the_env_override_reaches_the_narrow_read(self, tmp_path, monkeypatch):
        """The narrow read must not lose a feature the full load has. Same
        ``_apply_env_overrides``, so the two paths agree."""
        p = tmp_path / "config.yaml"
        p.write_text("beta:\n  voice_layer: false\n")
        monkeypatch.setattr(config_mod, "default_config_path", lambda: p)
        monkeypatch.setenv("AGENTWIRE_BETA__VOICE_LAYER", "true")
        assert config_mod.enabled_beta_flags() == {"voice_layer"}

    def test_the_narrow_read_and_the_full_load_agree(self, tmp_path, monkeypatch):
        """Two readers of one flag is how a gate ends up open on one path and
        shut on the other. Swept over every shape the config can take."""
        shapes = [
            "", "beta:\n", "beta: null\n", "beta: nonsense\n", "beta: []\n",
            "beta:\n  voice_layer: true\n", "beta:\n  voice_layer: false\n",
            'beta:\n  voice_layer: "true"\n', 'beta:\n  voice_layer: "false"\n',
            "beta:\n  voice_layer: 1\n", "beta:\n  other_flag: true\n",
            "server:\n  port: 9000\n",
        ]
        for i, text in enumerate(shapes):
            p = tmp_path / f"c{i}.yaml"
            p.write_text(text)
            monkeypatch.setattr(config_mod, "default_config_path", lambda p=p: p)
            narrow = config_mod.enabled_beta_flags()
            full = {
                n for n in config_mod.BETA_FLAG_NAMES
                if getattr(config_mod.load_config(p).beta, n, False) is True
            }
            assert narrow == full, f"readers disagree on {text!r}: {narrow} vs {full}"

    def test_a_missing_or_malformed_config_is_off(self, tmp_path, monkeypatch):
        """Every shape a config file can be in, resolving OFF without raising —
        because a gate that opens (or crashes role rendering) when the config is
        broken is not a gate. Malformed YAML included: it reaches the except."""
        for i, text in enumerate([
            None,                       # file absent entirely
            "", "beta:\n", "beta: null\n", "beta: 3\n", "beta: [a, b]\n",
            "beta:\n  voice_layer: [1,\n",       # malformed YAML
            "\tbeta:\n\tvoice_layer: true\n",  # tab indentation — a YAML error
        ]):
            p = tmp_path / f"m{i}.yaml"
            if text is not None:
                p.write_text(text)
            monkeypatch.setattr(config_mod, "default_config_path", lambda p=p: p)
            assert config_mod.enabled_beta_flags() == set(), f"shape {i} opened the gate"

    def test_a_broken_reader_fails_closed(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("unreadable")

        monkeypatch.setattr(config_mod, "default_config_path", boom)
        assert config_mod.enabled_beta_flags() == set()


# =============================================================================
# The buddy CLI refuses when the flag is off — naming BOTH next moves
# =============================================================================


class TestBuddyCliRefusal:
    """A refusal that does not name the next move is the defect this project
    keeps closing, so the message is asserted on rather than merely the exit
    code: the exact config key, and the fact that the OpenAI key must be in the
    secrets file."""

    @pytest.fixture
    def off(self, monkeypatch):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())

    @pytest.fixture
    def on(self, monkeypatch):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})

    def _run(self, subcommand, argv=()):
        from agentwire import buddy_cli

        parser = argparse.ArgumentParser()
        buddy_cli.register_buddy_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["buddy", subcommand, *argv])
        return args.func(args)

    def test_serve_refuses_with_the_flag_off(self, off, capsys):
        assert self._run("serve") == 1
        err = capsys.readouterr().err
        assert "beta.voice_layer" in err or "voice_layer: true" in err

    def test_the_refusal_names_the_config_key_and_the_secret(self, off, capsys):
        self._run("serve")
        err = capsys.readouterr().err
        assert "voice_layer" in err
        assert "config.yaml" in err
        assert "OPENAI_API_KEY" in err
        assert ".env" in err

    def test_every_buddy_subcommand_is_gated(self, off, capsys):
        """The pin against the next subcommand forgetting the decorator — a
        read-only verb that still works is a beta feature that is half-on."""
        from agentwire import buddy_cli

        parser = argparse.ArgumentParser()
        buddy_cli.register_buddy_parser(parser.add_subparsers(dest="command"))
        actions = [
            a for a in parser._subparsers._group_actions[0].choices["buddy"]._actions
            if isinstance(a, argparse._SubParsersAction)
        ]
        names = sorted(actions[0].choices)
        assert names, "no buddy subcommands found — the walk is wrong, not the gate"
        for name in names:
            func = actions[0].choices[name].get_default("func")
            assert getattr(func, "_beta_gated", False), f"buddy {name} is not gated"

    def test_json_mode_refuses_as_json(self, off, capsys):
        assert self._run("serve", ["--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert payload["beta_flag"] == "beta.voice_layer"
        assert payload["secret"] == "OPENAI_API_KEY"

    def test_the_gate_lets_the_command_through_when_on(self, on, monkeypatch):
        """The must-fail control: without it, a gate that refuses unconditionally
        would pass every assertion above."""
        from agentwire import buddy_cli

        seen = {}
        monkeypatch.setattr(
            buddy_cli.identity, "list_buddies", lambda: seen.setdefault("called", []) or []
        )
        assert self._run("list", ["--json"]) == 0
        assert "called" in seen


# =============================================================================
# doctor reports the flag — and never the key
# =============================================================================


class TestDoctorSection:
    def _render(self):
        from agentwire.doctor_cli import _render_beta_section

        return _render_beta_section()

    def test_off_is_reported_and_is_not_an_issue(self, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        assert self._render() == 0
        out = capsys.readouterr().out
        assert "voice_layer" in out
        assert "off" in out.lower()

    def test_on_with_a_key_is_green(self, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-SUPERSECRET-abcdef")
        assert self._render() == 0
        out = capsys.readouterr().out
        assert "[ok]" in out

    def test_on_without_a_key_is_an_issue_with_a_fix(self, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert self._render() == 1
        out = capsys.readouterr().out
        assert "[!!]" in out
        assert "OPENAI_API_KEY" in out
        assert ".env" in out

    def test_the_key_is_never_printed_not_even_a_prefix(self, monkeypatch, capsys):
        secret = "sk-proj-ZZZZQQQQ9999wwww"
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        self._render()
        out = capsys.readouterr().out
        for n in (4, 6, 8, len(secret)):
            assert secret[:n] not in out, f"doctor leaked the first {n} chars of the key"
        assert secret[-4:] not in out


# =============================================================================
# B1 — the MCP tool schema is a model-facing surface too
# =============================================================================
#
# It was missed the first time, and the miss is instructive: the brief said the
# role prompts were "the one surface", the implementation gated exactly that,
# and the commit message then asserted the claim it had inherited. `msg_send`'s
# description grew ~316 characters of voice-buddy prose that loads into every
# agent session in every install. So the proof below is not "and also gate that
# docstring" — it is the WHOLE schema, name by name, so the next description
# cannot ride in the same way.


def _branch_tool_docstrings() -> dict:
    """{tool_name: raw docstring} for every ``@mcp.tool()`` in the branch.

    Read from SOURCE via ast rather than from the imported modules, so the test
    can render both gate states in one process — the import-time resolution has
    already happened by the time a test runs, and can only reflect one.
    """
    import ast

    pkg = Path(config_mod.__file__).parent
    out = {}
    for path in sorted(pkg.glob("mcp_*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                f = dec.func if isinstance(dec, ast.Call) else dec
                if getattr(f, "attr", None) == "tool":
                    out[node.name] = ast.get_docstring(node, clean=False) or ""
    return out


def _main_tool_docstrings() -> dict:
    return json.loads(
        (Path(__file__).parent.parent / "fixtures" / "main_mcp_tools.json").read_text()
    )


class TestMcpSchemaIsByteIdenticalToMain:
    def test_the_extraction_is_not_fiction(self):
        """The load-bearing check under every assertion in this class: what
        ``ast`` pulls out of the source is what FastMCP actually PUBLISHES.

        Not verbatim, and the difference had to be measured rather than
        assumed: registration rewrites ``fn.__doc__`` to
        ``inspect.cleandoc(doc) + "\n"`` and publishes that string, so the
        description is the DEDENTED form — 1912 raw → 1800 published for
        ``msg_send`` with the gate off, which is the 1800 the review measured
        on main. Since that transform is deterministic, equality of the RAW
        docstrings implies equality of the published ones, which is what lets
        the fixture store raw text; the implication is pinned here rather than
        reasoned about.
        """
        import asyncio
        import inspect

        from agentwire import mcp_msg
        from agentwire.mcp_core import mcp

        registered = {t.name: t.description for t in asyncio.run(mcp.list_tools())}
        assert registered["msg_send"] == mcp_msg.msg_send.__doc__
        assert registered["msg_send"] == inspect.cleandoc(
            beta_mod.render(_branch_tool_docstrings()["msg_send"])
        ) + "\n"

    def test_no_published_description_carries_a_marker(self):
        """Every tool in the LIVE registry, not just the one we know about.

        Decorator ORDER is load-bearing and silently so: ``@gated_doc`` must
        sit BELOW ``@mcp.tool()``, because decorators apply bottom-up and
        FastMCP snapshots the docstring when it registers. Inverted, the raw
        text publishes — markers and all (2165 chars, verified by building both
        orders and reading the registry) — and the only thing that noticed was
        a check naming ``msg_send`` explicitly. A SECOND gated tool with
        inverted decorators would ship its gated prose to every session with
        nothing red.

        So the property is asserted over the whole registry rather than per
        tool: a marker in a published description means the gate did not run,
        whatever the reason. This would also have caught the original B1, since
        gating the prose is the only way it leaves the description at all.
        """
        import asyncio

        from agentwire import mcp_server  # noqa: F401  (registers every domain)
        from agentwire.mcp_core import mcp

        published = {t.name: t.description or "" for t in asyncio.run(mcp.list_tools())}
        assert len(published) > 100, "registry looks unpopulated — the sweep is not real"
        leaking = sorted(n for n, d in published.items() if "beta:" in d)
        assert leaking == [], (
            f"these published MCP descriptions carry an unresolved beta marker "
            f"— check that @gated_doc sits BELOW @mcp.tool(): {leaking}"
        )

    def test_no_tool_was_added_or_removed(self):
        assert sorted(_branch_tool_docstrings()) == sorted(_main_tool_docstrings())

    def test_every_description_with_the_gate_off_equals_main(self):
        """The whole schema, name by name — not just the docstring we know
        about. This is the assertion the next ungated description trips."""
        off = {
            name: beta_mod.apply_beta_blocks(doc, enabled=set())
            for name, doc in _branch_tool_docstrings().items()
        }
        main = _main_tool_docstrings()
        differing = sorted(n for n in off if off[n] != main.get(n))
        assert differing == [], (
            f"these MCP tool descriptions differ from origin/main with the gate "
            f"off: {differing} — gate the added prose with <!-- beta:... -->"
        )

    def test_the_published_description_with_the_gate_off_equals_main(self):
        """The same claim one layer out, in the form the model receives."""
        import inspect

        main = _main_tool_docstrings()
        for name, doc in _branch_tool_docstrings().items():
            got = inspect.cleandoc(beta_mod.render(doc, enabled=set()))
            assert got == inspect.cleandoc(main[name]), name

    def test_the_gate_is_actually_wired_into_the_shipped_docstring(self):
        """The regex being right proves nothing if nothing calls it. This is
        the LIVE description, as imported, under this process's real flags."""
        import inspect

        from agentwire import beta as live_beta
        from agentwire import mcp_msg

        expected = inspect.cleandoc(
            beta_mod.render(
                _branch_tool_docstrings()["msg_send"], enabled=live_beta.enabled_flags()
            )
        )
        assert (mcp_msg.msg_send.__doc__ or "").strip() == expected.strip()

    def test_the_voice_prose_is_present_when_enabled(self):
        on = beta_mod.render(
            _branch_tool_docstrings()["msg_send"], enabled={"voice_layer"}
        )
        assert "voice buddy" in on
        assert "beta:" not in on

    def test_the_fixture_matches_origin_main(self):
        """**Expect this to SKIP in CI** — same reason as the role-file
        snapshot: ``actions/checkout`` clones at depth 1 with no ``origin/main``
        ref. A local guard against hand-editing the fixture; what runs
        everywhere is the control below plus the assertions against the
        committed fixture.
        """
        import ast
        import subprocess as sp

        root = Path(__file__).parent.parent.parent
        main = _main_tool_docstrings()
        for path in sorted((Path(config_mod.__file__).parent).glob("mcp_*.py")):
            proc = sp.run(
                ["git", "show", f"origin/main:agentwire/{path.name}"],
                cwd=root, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                pytest.skip("origin/main not fetched in this checkout")
            for node in ast.walk(ast.parse(proc.stdout)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(getattr(d.func if isinstance(d, ast.Call) else d, "attr", None) == "tool"
                       for d in node.decorator_list):
                    assert main.get(node.name) == (ast.get_docstring(node, clean=False) or ""), (
                        f"fixture drifted from origin/main for {node.name}"
                    )

    def test_the_fixture_is_not_just_the_branch(self):
        """The control. If the fixture had been regenerated from this branch,
        every assertion above would pass while proving nothing."""
        assert _branch_tool_docstrings()["msg_send"] != _main_tool_docstrings()["msg_send"]


# =============================================================================
# N1 — a gate whose failure direction is ON is the wrong shape
# =============================================================================


class TestQuotedScalarsFailClosed:
    @pytest.mark.parametrize("literal", ['"false"', '"no"', '"off"', '"0"', '""', "'False'"])
    def test_a_quoted_falsey_string_leaves_the_gate_off(self, tmp_path, literal):
        """``bool("false")`` is True. A user who wrote quotes around the value
        they meant to disable must not thereby enable a beta feature."""
        p = tmp_path / "config.yaml"
        p.write_text(f"beta:\n  voice_layer: {literal}\n")
        assert config_mod.load_config(p).beta.voice_layer is False

    @pytest.mark.parametrize("literal", ['"true"', '"yes"', '"on"', '"1"', "1"])
    def test_a_non_boolean_truthy_value_also_leaves_it_off(self, tmp_path, literal):
        """Fail-closed in both directions: only a real YAML boolean opens the
        gate, so there is exactly one spelling to reason about."""
        p = tmp_path / "config.yaml"
        p.write_text(f"beta:\n  voice_layer: {literal}\n")
        assert config_mod.load_config(p).beta.voice_layer is False

    def test_the_bare_boolean_still_works(self, tmp_path):
        """The must-fail control for the two above — without it, a gate welded
        shut would pass every assertion in this class."""
        p = tmp_path / "config.yaml"
        p.write_text("beta:\n  voice_layer: true\n")
        assert config_mod.load_config(p).beta.voice_layer is True


# =============================================================================
# N2 — marker shapes that fail OPEN, and the audit that sees them
# =============================================================================


class TestMarkerShapesThatUsedToFailOpen:
    def test_indented_markers_are_resolved(self):
        """The shape a docstring forces: markers inside an indented block."""
        text = "    keep\n    <!-- beta:voice_layer -->\n    drop\n    <!-- /beta:voice_layer -->\n    keep2\n"
        assert "drop" not in beta_mod.apply_beta_blocks(text, enabled=set())

    def test_a_close_tag_at_eof_with_no_trailing_newline_is_resolved(self):
        text = "keep\n<!-- beta:voice_layer -->\ndrop\n<!-- /beta:voice_layer -->"
        out = beta_mod.render(text, enabled=set())
        assert "drop" not in out and "beta:" not in out

    def test_a_stray_marker_line_never_reaches_the_model(self):
        """A mistyped close tag pairs with nothing, so the region is not gated —
        that is what the audit below exists to catch. What is fixed HERE is the
        smaller half: the scaffolding itself must not ship as prose."""
        text = "keep\n<!-- beta:voice_layer -->\nungated\n<!-- /beta:voice-layer -->\n"
        out = beta_mod.render(text, enabled=set())
        assert "beta:" not in out
        assert "ungated" in out  # honest: still ungated, and the audit says so

    def test_the_audit_sees_every_marker_in_every_shipped_role(self):
        """The coverage gap: ``findall`` only ever saw markers that already
        PAIRED, so a mistyped, indented or EOF-terminated marker was invisible
        to it — and 20 of the 24 shipped role files carry no markers at all, so
        nothing was looking at them either.

        Asks the question the other way round: after resolving with every flag
        enabled, no marker-shaped text may remain anywhere. An unpaired open
        tag survives that and turns this red.
        """
        stray = []
        for path in sorted(ROLES_DIR.glob("*.md")):
            out = beta_mod.apply_beta_blocks(
                path.read_text(), enabled=set(beta_mod.flag_names())
            )
            stray += [f"{path.name}: {m}" for m in beta_mod.LOOSE_MARKER_RE.findall(out)]
        assert stray == [], f"unresolved beta markers (unpaired or misspelt): {stray}"

    def test_the_audit_control_catches_a_mistyped_close_tag(self):
        """Must-fail control for the audit: a file it cannot see through."""
        broken = "<!-- beta:voice_layer -->\nx\n<!-- /beta:voice-layer -->\n"
        out = beta_mod.apply_beta_blocks(broken, enabled={"voice_layer"})
        assert beta_mod.LOOSE_MARKER_RE.findall(out)


# =============================================================================
# N3 — the gate must not re-read config once per role file
# =============================================================================


class TestConfigIsReadOncePerProcess:
    def test_parsing_every_role_loads_the_config_at_most_once(self, monkeypatch):
        """22x on a hot path every session touches, plus a stderr INFO line per
        role file, for a feature almost nobody has enabled.

        Counts calls to ``enabled_beta_flags`` — the function the cache
        actually wraps — and NOT ``load_config``, which is what this test
        counted when it was written. The narrow-read refactor in the same
        commit stopped routing through ``load_config``, so deleting the cache
        outright left this test, the one named after the cache, GREEN: the
        83ms fix silently disarmed the pin guarding the 242ms fix. A pin whose
        subject moved out from under it is worse than no pin, because its name
        still claims the coverage.
        """
        from agentwire import beta as beta_live

        beta_live.reset_cache()
        calls = []
        real = config_mod.enabled_beta_flags
        monkeypatch.setattr(
            config_mod, "enabled_beta_flags",
            lambda *a, **k: (calls.append(1), real(*a, **k))[1],
        )
        for path in sorted(ROLES_DIR.glob("*.md")):
            roles_mod.parse_role_file(path)
        assert len(calls) <= 1, f"flags re-read {len(calls)}x while parsing roles"

    def test_the_pin_above_can_see_a_deleted_cache(self, monkeypatch):
        """The control the original lacked. Simulates exactly what the review
        did — resolve every marker-bearing role file with no cache in the way —
        and asserts the count it would produce is one the pin REJECTS. Without
        this, "at most once" can silently become "at most once because nothing
        asks more than once"."""
        from agentwire import beta as beta_live

        beta_live.reset_cache()
        calls = []
        real = config_mod.enabled_beta_flags
        monkeypatch.setattr(
            config_mod, "enabled_beta_flags",
            lambda *a, **k: (calls.append(1), real(*a, **k))[1],
        )
        for path in sorted(ROLES_DIR.glob("*.md")):
            beta_live.reset_cache()  # stand in for "there is no cache"
            roles_mod.parse_role_file(path)
        assert len(calls) > 1, (
            "an uncached gate consulted the flags at most once — the pin above "
            "cannot distinguish a working cache from a gate nothing calls"
        )

    def test_the_cache_can_be_reset(self, monkeypatch):
        """The escape hatch the tests themselves depend on — without it, the
        first flag state a process observes is the only one it can ever see."""
        from agentwire import beta as beta_live

        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        beta_live.reset_cache()
        assert beta_live.enabled_flags() == {"voice_layer"}
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        assert beta_live.enabled_flags() == {"voice_layer"}  # cached
        beta_live.reset_cache()
        assert beta_live.enabled_flags() == set()


# =============================================================================
# N5 — the same defect class, one door over
# =============================================================================


class TestUnregisteredBuddyRefusalsNameTheNextMove:
    """Pre-existing, and fixed because it is exactly the class the beta refusal
    was written to close: ``buddy inbox``/``mint``/``serve`` said "No voice
    buddy named 'buddy'." and stopped there. ``status`` already named the
    register command, so the fleet of refusals disagreed with itself about
    whether the user gets told what to do."""

    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: {"voice_layer"})
        beta_mod.reset_cache()

    @pytest.mark.parametrize("subcommand", ["status", "inbox", "mint", "serve"])
    def test_the_refusal_names_the_register_command(self, subcommand, capsys, monkeypatch):
        from agentwire import buddy_cli

        monkeypatch.setattr(buddy_cli.identity, "is_registered", lambda name: False)
        monkeypatch.setattr(
            buddy_cli.identity, "status",
            lambda name: {"registered": False, "name": name},
        )
        parser = argparse.ArgumentParser()
        buddy_cli.register_buddy_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["buddy", subcommand, "nosuch"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "agentwire buddy register nosuch" in err, (
            f"buddy {subcommand} refuses without naming the next move: {err!r}"
        )


class TestMarkerFreeTextIsFree:
    def test_text_with_no_marker_never_consults_the_config(self, monkeypatch):
        """Most gated surfaces contain no marker at all — 20 of 24 role files,
        107 of 108 MCP descriptions, several resolved at IMPORT time. They must
        not pay for a flag lookup that cannot change their content."""
        from agentwire import beta as beta_live

        beta_live.reset_cache()
        monkeypatch.setattr(
            config_mod, "enabled_beta_flags",
            lambda: pytest.fail("config consulted for marker-free text"),
        )
        assert beta_live.render("plain prose, no markers\n") == "plain prose, no markers\n"

    def test_text_with_a_marker_still_consults_it(self, monkeypatch):
        """The must-fail control: a fast path that swallowed everything would
        make the gate a no-op and pass every test above."""
        from agentwire import beta as beta_live

        beta_live.reset_cache()
        monkeypatch.setattr(config_mod, "enabled_beta_flags", lambda: set())
        text = "keep\n<!-- beta:voice_layer -->\ndrop\n<!-- /beta:voice_layer -->\n"
        assert beta_live.render(text) == "keep\n"
