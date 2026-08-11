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

The output assertions below are the user-visible pin, but they can only fail on
a machine whose pydantic-settings actually emits that warning. So the
structural invariants are pinned separately: they fail on every version, and
they are what a future refactor would actually break.
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


def test_building_the_parser_does_not_build_an_mcp_server():
    """The structural pin: no CLI import path may reach ``mcp_core``.

    Version-independent, unlike the stderr assertions — this fails the moment
    any ``*_cli`` module reaches for an MCP helper again, which is how both
    symptoms got in.
    """
    probe = textwrap.dedent("""\
        import sys
        import agentwire.__main__ as m
        m.build_parser()
        leaked = sorted(
            n for n in sys.modules
            if n == "agentwire.mcp_core" or n.startswith("mcp.server")
        )
        print(",".join(leaked))
        """)
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "PYTHONPATH": str(REPO_ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        "build_parser() imported MCP server modules: " + proc.stdout.strip()
    )


def test_root_logger_is_untouched_by_building_the_parser():
    """No import may call ``logging.basicConfig`` on the CLI's behalf.

    A handler on the root logger is what turned every library INFO record into
    CLI stderr; asserting on the absence of one catches a re-introduction
    wherever it happens, not just in ``mcp_core``.
    """
    probe = textwrap.dedent("""\
        import logging
        import agentwire.__main__ as m
        m.build_parser()
        print(len(logging.getLogger().handlers))
        """)
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "PYTHONPATH": str(REPO_ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0", (
        f"root logger gained {proc.stdout.strip()} handler(s) at import time"
    )


def test_importing_mcp_core_emits_no_settings_warning():
    """The MCP server path is clean too — its stderr is the client's log.

    ``mcp_core`` resolves the ``lifespan`` forward reference with
    ``Settings.model_rebuild()`` before constructing FastMCP. Vacuous on a
    pydantic-settings old enough not to warn; real on >= 2.15.
    """
    probe = textwrap.dedent("""\
        import warnings
        warnings.simplefilter("error")
        import agentwire.mcp_core  # noqa: F401
        print("ok")
        """)
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "PYTHONPATH": str(REPO_ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
