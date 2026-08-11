"""Ordinary CLI commands must write NOTHING to stderr (#1018).

Two symptoms, one root cause. ``build_parser()`` imports every ``*_cli``
module, and ``buddy_cli`` reached ``voice_layer.tools`` → ``mcp_core`` for one
subprocess helper. Importing ``mcp_core`` does two things no CLI invocation
asked for:

1. constructs the FastMCP singleton, whose settings model carries an
   unresolved forward reference — pydantic-settings >= 2.15 warns about it on
   every instantiation; and
2. calls ``logging.basicConfig(level=INFO)``, which configures the ROOT logger
   for the whole process, promoting library INFO records (the STT config line)
   into CLI stderr.

**Which of these pins is live depends on the installed dependencies, so say so
rather than claim a blanket guarantee.** Measured against the unfixed tree:

- ``uv.lock`` (pydantic-settings 2.14.2 / mcp 1.27.2 — what CI runs): 5 of the
  7 go red. The two that do not are the ``--version`` and ``roles list``
  stderr assertions; neither command loads config, and 2.14.2 emits no warning,
  so there is nothing for them to catch there. They are the forward-looking
  half — they bite the moment the pinned deps move up.
- pydantic-settings 2.15.0 / mcp 1.29.0 (the versions that reproduce the
  reported symptom): all 7 go red.

Every half of the bug therefore has at least one live control under the locked
set: the STT line via ``projects list`` and the log-level test, and the
pydantic warning via the two structural pins, which key on state and ordering
rather than on the warning and so hold on every version.
"""

import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Commands that touch no tmux server and mutate nothing, but do load config
#: (so the STT code path actually runs) and build the full parser.
REPRESENTATIVE_COMMANDS = [
    ["--version"],
    ["roles", "list", "--json"],
    ["projects", "list", "--json"],
]


def _fake_home(tmp_path: Path) -> Path:
    """A HOME with a config that has an ``stt`` section.

    Without the section the STT log line never executes at all and the stderr
    assertion passes for the wrong reason — the failure mode this suite keeps
    finding (a green test measuring a fixture that cannot express the bug).
    """
    home = tmp_path / "home"
    cfg = home / ".agentwire"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(textwrap.dedent("""\
        stt:
          backend: default
        """))
    return home


@pytest.mark.parametrize("argv", REPRESENTATIVE_COMMANDS,
                         ids=lambda a: " ".join(a))
def test_command_writes_nothing_to_stderr(argv, tmp_path):
    """A healthy command is silent on stderr — no warnings, no INFO records."""
    home = _fake_home(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "agentwire", *argv],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "HOME": str(home),
             "PYTHONPATH": str(REPO_ROOT)},
    )
    assert proc.stderr == "", (
        f"`agentwire {' '.join(argv)}` polluted stderr:\n{proc.stderr}"
    )


def test_stt_config_line_is_not_emitted_at_info(tmp_path, monkeypatch, caplog):
    """The config loader's STT line is DEBUG, and the fixture proves it runs.

    Asserting only "no INFO record" would pass on a config with no ``stt``
    section at all, so the DEBUG assertion is the control: if it is missing,
    the code path never executed and the INFO assertion measured nothing.
    """
    from agentwire import config as config_mod

    home = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))

    with caplog.at_level(logging.DEBUG, logger=config_mod.__name__):
        config_mod.load_config(home / ".agentwire" / "config.yaml")

    stt_records = [r for r in caplog.records if "STT config" in r.getMessage()]
    assert stt_records, "STT config path did not run — fixture is wrong"
    assert all(r.levelno == logging.DEBUG for r in stt_records), (
        f"STT config logged above DEBUG: {[r.levelname for r in stt_records]}"
    )




def _probe(code: str, home: Path) -> subprocess.CompletedProcess:
    """Run a probe in a fresh interpreter against an isolated HOME.

    Isolated deliberately: a probe reading the developer's real
    ``~/.agentwire`` makes its result machine-dependent, which is the opposite
    of what a pin is for.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "HOME": str(home),
             "PYTHONPATH": str(REPO_ROOT)},
    )


def test_building_the_parser_does_not_build_an_mcp_server(tmp_path):
    """The structural pin: no CLI import path may reach ``mcp_core``.

    Version-independent, unlike the stderr assertions — this fails the moment
    any ``*_cli`` module reaches for an MCP helper again, which is how both
    symptoms got in.
    """
    proc = _probe("""\
        import sys
        import agentwire.__main__ as m
        m.build_parser()
        leaked = sorted(
            n for n in sys.modules
            if n == "agentwire.mcp_core" or n.startswith("mcp.server")
        )
        print(",".join(leaked))
        """, _fake_home(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        "build_parser() imported MCP server modules: " + proc.stdout.strip()
    )


def test_root_logger_is_untouched_by_building_the_parser(tmp_path):
    """No import may call ``logging.basicConfig`` on the CLI's behalf.

    A handler on the root logger is what turned every library INFO record into
    CLI stderr; asserting on the absence of one catches a re-introduction
    wherever it happens, not just in ``mcp_core``.
    """
    proc = _probe("""\
        import logging
        import agentwire.__main__ as m
        m.build_parser()
        print(len(logging.getLogger().handlers))
        """, _fake_home(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0", (
        f"root logger gained {proc.stdout.strip()} handler(s) at import time"
    )


def test_mcp_core_rebuilds_settings_before_it_constructs_the_server(tmp_path):
    """The MCP server path is clean too — its stderr is the client's log.

    Pinned on ORDERING, and measured at the only instant that discriminates.
    Two facts make the obvious assertions useless here, both verified rather
    than assumed:

    - Only pydantic-settings >= 2.15 warns about the unresolved forward
      reference, and ``uv.lock`` pins 2.14.2 — so a warning-based assertion is
      vacuous under exactly the dependency set CI runs, which is no control at
      all for the headline symptom of #1018.
    - ``Settings.__pydantic_complete__`` read *after* importing ``mcp_core`` is
      True on the unfixed tree too: constructing ``FastMCP()`` completes the
      model as a side effect of validation (which is why upstream warns instead
      of raising). Sampled there, the flag cannot tell the trees apart.

    What actually distinguishes them is whether the model was ALREADY complete
    at the moment ``mcp_core`` constructed the server — i.e. that the rebuild
    runs first. So spy on ``FastMCP.__init__`` and sample the flag inside it.
    Version-independent, and it pins the ordering the fix depends on, which
    nothing else does.

    The ``RENAMED`` branch is not redundant: ``mcp_core`` guards the rebuild
    with ``getattr`` so an upstream rename cannot take the whole tool surface
    down, and that guard degrades to a silent no-op. This is what notices it.
    """
    proc = _probe("""\
        import mcp.server.fastmcp.server as s

        settings = getattr(s, "Settings", None)
        if settings is None:
            print("RENAMED")
        else:
            sampled = {}
            original = s.FastMCP.__init__

            def spy(self, *args, **kwargs):
                # Sample BEFORE delegating: the constructor itself completes
                # the model, so after the call every tree looks identical.
                sampled.setdefault("complete", settings.__pydantic_complete__)
                return original(self, *args, **kwargs)

            s.FastMCP.__init__ = spy
            import agentwire.mcp_core  # noqa: F401  (constructs the singleton)

            if "complete" not in sampled:
                print("NEVER_CONSTRUCTED")
            else:
                print("complete" if sampled["complete"] else "INCOMPLETE")
        """, _fake_home(tmp_path))
    assert proc.returncode == 0, proc.stderr
    verdict = proc.stdout.strip()
    assert verdict != "RENAMED", (
        "upstream renamed FastMCP's Settings model — mcp_core's getattr guard "
        "is now a silent no-op and the lifespan warning is back in every MCP "
        "client's log"
    )
    assert verdict != "NEVER_CONSTRUCTED", (
        "mcp_core no longer constructs FastMCP through FastMCP.__init__ — this "
        "probe is measuring nothing; re-point it before trusting it"
    )
    assert verdict == "complete", (
        "mcp_core constructed FastMCP while its Settings model still had an "
        "unresolved forward reference — Settings.model_rebuild() did not run "
        "first, and pydantic-settings >= 2.15 warns on every instantiation"
    )
