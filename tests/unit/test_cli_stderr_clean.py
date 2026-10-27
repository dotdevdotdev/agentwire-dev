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












