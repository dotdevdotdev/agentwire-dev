"""The stored launch line must survive being RE-RUN (#901).

``AGENTWIRE_LAUNCH_CMD`` exists to be re-evaluated (#856/#866). #881 put
``claude --session-id <uuid>`` in it, and that flag is single-use — it
hard-errors once the conversation's transcript exists. So any session that
took one turn and then exited could never be relaunched from its own launch
line: claude refused to start and the pane sat at a bare shell. Thirteen live
sessions were stranded that way.

**These tests evaluate the real generated line TWICE.** That is the whole
point: a single-launch test cannot see this bug, which is exactly why a green
suite shipped it. ``claude`` is stubbed — by a stub that reproduces the two
flag behaviours as MEASURED, not as assumed:

- ``--session-id <id>`` refuses when ``<id>.jsonl`` already EXISTS under the
  cwd's history key ("Session ID <id> is already in use.")
- ``--resume <id>`` refuses unless that file holds an actual TURN ("No
  conversation found with session ID"). The two flags disagree on a
  metadata-only stub, which is what makes such an id dead to both.
- the transcript is written on the first turn, keyed by the PHYSICAL cwd.
"""

import json
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from agentwire import core
from agentwire.core import (
    _LAUNCH_EVAL,
    LAUNCH_CMD_ENV,
    _guarded_launch_command,
    build_agent_command,
)

# The launch shell is whatever the operator uses. zsh is the default on macOS
# (and the one that does NOT word-split unquoted expansions, which is why the
# flags travel in an array); bash covers the Linux side and CI.
SHELLS = [s for s in ("zsh", "bash") if shutil.which(s)]

#: What makes a transcript a conversation rather than a metadata stub.
TURN = '{"type":"user","message":{"role":"user"}}\n'
#: The 5-line file a restart of a MOVED session leaves at the new key. Neither
#: flag will take an id in this state — see TestDeadIds.

STUB_CLAUDE = '''#!/usr/bin/env python3
"""Stand-in for `claude`, reproducing the measured behaviour of the two
conversation flags. Logs every invocation so the test can assert on which
flags the shell actually chose."""
import json
import os
import re
import sys

args = sys.argv[1:]
hist = os.path.join(
    os.environ["HOME"], ".claude", "projects",
    re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()),   # getcwd() is physical
)


def flag(name):
    return args[args.index(name) + 1] if name in args else None


sid, resume = flag("--session-id"), flag("--resume")
with open(os.path.join(os.environ["HOME"], "invocations.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\\n")


def transcript(cid):
    return os.path.join(hist, cid + ".jsonl")


def has_turns(cid):
    """--resume needs an actual turn; --session-id only needs the file. The
    two flags disagreeing is what makes a metadata stub a DEAD id."""
    try:
        with open(transcript(cid)) as fh:
            return any('"type":"user"' in line for line in fh)
    except IOError:
        return False


if sid and os.path.exists(transcript(sid)):
    sys.exit("Error: Session ID %s is already in use." % sid)
if resume and not has_turns(resume):
    sys.exit("No conversation found with session ID %s" % resume)

os.makedirs(hist, exist_ok=True)                  # the first turn writes it
with open(transcript(sid or resume or "self-minted"), "a") as fh:
    fh.write('{"type":"user","message":{"role":"user"}}\\n')
'''


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(STUB_CLAUDE)
    stub.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    # #902 replaced the ROLE_PROMPTS_DIR constant with role_prompts_dir(), which
    # resolves through CONFIG_DIR at CALL time — so redirect the store by patching
    # CONFIG_DIR, not by rebinding a constant that no longer exists.
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    return types.SimpleNamespace(
        root=tmp_path,
        home=home,
        bin=bin_dir,
        work=work,
        log=home / "invocations.jsonl",
    )


def launch(sandbox, shell, agent, path=None):
    """Run the stored launch line exactly as a pane does: eval the env var."""
    line = _guarded_launch_command(str(path or sandbox.work), agent.command)
    env = {
        **os.environ,
        "HOME": str(sandbox.home),
        "PATH": f"{sandbox.bin}:{os.environ['PATH']}",
        LAUNCH_CMD_ENV: line,
    }
    return subprocess.run(
        [shell, "-c", _LAUNCH_EVAL],
        env=env,
        cwd=str(sandbox.root),
        capture_output=True,
        text=True,
        timeout=30,
    )


def invocations(sandbox):
    if not sandbox.log.exists():
        return []
    return [json.loads(line) for line in sandbox.log.read_text().splitlines()]


def conversation_flags(invocation):
    """The flags the shell chose, i.e. everything before the posture flags."""
    argv = invocation["argv"]
    end = next(
        (
            i
            for i, a in enumerate(argv)
            if a.startswith("--") and a not in ("--session-id", "--resume", "--fork-session")
        ),
        len(argv),
    )
    return argv[:end]


def history_transcripts(sandbox, cwd):
    from agentwire.history import encode_project_path

    d = sandbox.home / ".claude" / "projects" / encode_project_path(str(Path(cwd).resolve()))
    return sorted(p.name for p in d.glob("*.jsonl")) if d.exists() else []


@pytest.mark.parametrize("shell", SHELLS)
class TestReEntry:
    def test_the_stored_line_survives_a_second_evaluation(self, sandbox, shell):
        """#901 itself. Under the old line the second run died with
        'Session ID <id> is already in use.' and left a bare shell."""
        agent = build_agent_command("bypass")

        first = launch(sandbox, shell, agent)
        second = launch(sandbox, shell, agent)

        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert "already in use" not in second.stderr
        assert len(invocations(sandbox)) == 2

    def test_first_launch_claims_the_id_and_re_entry_resumes_it(self, sandbox, shell):
        """The id agentwire minted stays authoritative (#871) AND re-usable."""
        agent = build_agent_command("bypass")
        cid = agent.conversation_id

        launch(sandbox, shell, agent)
        launch(sandbox, shell, agent)
        first, second = invocations(sandbox)

        assert conversation_flags(first) == ["--session-id", cid]
        assert conversation_flags(second) == ["--resume", cid]
        # Re-entry continues the conversation rather than starting a second
        # one — one transcript, not two.
        assert history_transcripts(sandbox, sandbox.work) == [f"{cid}.jsonl"]
