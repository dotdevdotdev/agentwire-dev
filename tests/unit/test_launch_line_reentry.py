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
from agentwire.roles import RoleConfig

# The launch shell is whatever the operator uses. zsh is the default on macOS
# (and the one that does NOT word-split unquoted expansions, which is why the
# flags travel in an array); bash covers the Linux side and CI.
SHELLS = [s for s in ("zsh", "bash") if shutil.which(s)]

#: What makes a transcript a conversation rather than a metadata stub.
TURN = '{"type":"user","message":{"role":"user"}}\n'
#: The 5-line file a restart of a MOVED session leaves at the new key. Neither
#: flag will take an id in this state — see TestDeadIds.
STUB = ('{"type":"last-prompt"}\n{"type":"ai-title"}\n'
        '{"type":"mode","mode":"normal"}\n')

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
        root=tmp_path, home=home, bin=bin_dir, work=work,
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
        [shell, "-c", _LAUNCH_EVAL], env=env, cwd=str(sandbox.root),
        capture_output=True, text=True, timeout=30,
    )


def invocations(sandbox):
    if not sandbox.log.exists():
        return []
    return [json.loads(line) for line in sandbox.log.read_text().splitlines()]


def conversation_flags(invocation):
    """The flags the shell chose, i.e. everything before the posture flags."""
    argv = invocation["argv"]
    end = next(
        (i for i, a in enumerate(argv)
         if a.startswith("--") and a not in ("--session-id", "--resume", "--fork-session")),
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

    def test_the_role_survives_re_entry(self, sandbox, shell):
        """The other half of the trade: a fix that drops the system prompt on
        re-entry swaps #901 for #881's silent role loss."""
        role = RoleConfig(name="worker", instructions="YOU-ARE-A-WORKER-MARKER")
        agent = build_agent_command("bypass", [role])

        launch(sandbox, shell, agent)
        launch(sandbox, shell, agent)

        for inv in invocations(sandbox):
            assert "YOU-ARE-A-WORKER-MARKER" in inv["argv"], inv["argv"]
            # Multiline prompt content breaks any flag that follows it, so it
            # has to stay last on BOTH launches (core.py's build comment).
            assert inv["argv"][-2] == "--append-system-prompt"

    def test_posture_flags_survive_re_entry(self, sandbox, shell):
        agent = build_agent_command("bypass")

        launch(sandbox, shell, agent)
        launch(sandbox, shell, agent)

        for inv in invocations(sandbox):
            assert "--dangerously-skip-permissions" in inv["argv"]

    def test_an_explicit_resume_forks_once_then_re_enters_the_fork(self, sandbox, shell):
        """`restart`'s shape. The fork must happen exactly once: on re-entry
        the new conversation already exists, so forking again would mint a
        second one and orphan the first."""
        agent = build_agent_command("bypass", resume_session_id="old-conversation")
        cid = agent.conversation_id
        hist = sandbox.home / ".claude" / "projects"
        (hist / _key(sandbox.work)).mkdir(parents=True, exist_ok=True)
        (hist / _key(sandbox.work) / "old-conversation.jsonl").write_text(TURN)

        launch(sandbox, shell, agent)
        launch(sandbox, shell, agent)
        first, second = invocations(sandbox)

        assert conversation_flags(first) == [
            "--resume", "old-conversation", "--fork-session", "--session-id", cid,
        ]
        assert conversation_flags(second) == ["--resume", cid]

    def test_an_explicit_resume_whose_history_vanished_starts_fresh(self, sandbox, shell):
        """A recorded id does not guarantee a resumable conversation (#871):
        the transcript may never have been written, or Claude may have evicted
        it. Degrade to a fresh conversation with the role intact — never to
        'No conversation found' and a bare shell."""
        agent = build_agent_command("bypass", resume_session_id="never-existed")

        result = launch(sandbox, shell, agent)

        assert result.returncode == 0, result.stderr
        assert conversation_flags(invocations(sandbox)[0]) == [
            "--session-id", agent.conversation_id,
        ]

    def test_a_symlinked_cwd_resolves_to_the_physical_history_key(self, sandbox, shell):
        """Claude keys history by the PHYSICAL cwd — measured by launching
        from a symlink and reading back which directory it wrote. `$PWD`
        keeps the link as typed, so re-entry would look in a directory that
        never exists and try `--session-id` again. On macOS every `/tmp/...`
        path is such a symlink."""
        link = sandbox.root / "via-link"
        link.symlink_to(sandbox.work)
        agent = build_agent_command("bypass")

        launch(sandbox, shell, agent, path=link)
        second = launch(sandbox, shell, agent, path=link)

        assert second.returncode == 0, second.stderr
        assert conversation_flags(invocations(sandbox)[1]) == [
            "--resume", agent.conversation_id,
        ]

    def test_a_missing_directory_still_stops_before_the_agent(self, sandbox, shell):
        """The prelude runs between the cd guard and `claude`, so it must not
        break the guard's chain (#739): a failed cd still means no agent."""
        agent = build_agent_command("bypass")

        result = launch(sandbox, shell, agent, path=sandbox.root / "not-there")

        assert result.returncode != 0
        assert invocations(sandbox) == []


def _key(path):
    from agentwire.history import encode_project_path

    return encode_project_path(str(Path(path).resolve()))


class TestGeneratedLine:
    """Shape assertions that don't need a shell."""

    def test_bare_posture_has_no_prelude(self):
        assert build_agent_command("bare").command == ""

    def test_the_line_carries_no_bare_session_id_flag(self):
        """If `--session-id <uuid>` is ever emitted unconditionally again,
        the stored line is single-use once more."""
        agent = build_agent_command("bypass")
        assert f"claude --session-id {agent.conversation_id}" not in agent.command
        assert 'claude "${aw_flags[@]}"' in agent.command

    def test_the_prelude_mirrors_the_python_encoder(self):
        from agentwire.history import HISTORY_DIR_SHELL

        assert HISTORY_DIR_SHELL in build_agent_command("bypass").command


@pytest.mark.parametrize("shell", SHELLS)
class TestDeadIds:
    """A transcript that exists but holds no turn is dead to BOTH flags.

    Measured on real Claude Code 2.1.222, in the state a restart of a moved
    session leaves behind — a metadata stub at the new key while the
    conversation stays under the old one:

        claude --resume <id>      -> "No conversation found with session ID"
        claude --session-id <id>  -> "Session ID <id> is already in use."

    So an `[ -f ]` check is not enough: whichever flag the line picked, claude
    would refuse to start and the pane would sit at a bare shell — #901 again,
    reached by a different route.
    """

    def test_a_stub_launches_with_no_conversation_flag(self, sandbox, shell):
        agent = build_agent_command("bypass")
        d = sandbox.home / ".claude" / "projects" / _key(sandbox.work)
        d.mkdir(parents=True)
        (d / f"{agent.conversation_id}.jsonl").write_text(STUB)

        result = launch(sandbox, shell, agent)

        assert result.returncode == 0, result.stderr
        assert conversation_flags(invocations(sandbox)[0]) == []

    def test_the_role_still_survives_a_dead_id(self, sandbox, shell):
        """The degradation is the RECORD going stale, never the role."""
        role = RoleConfig(name="worker", instructions="ROLE-MARKER")
        agent = build_agent_command("bypass", [role])
        d = sandbox.home / ".claude" / "projects" / _key(sandbox.work)
        d.mkdir(parents=True)
        (d / f"{agent.conversation_id}.jsonl").write_text(STUB)

        launch(sandbox, shell, agent)

        assert "ROLE-MARKER" in invocations(sandbox)[0]["argv"]

    def test_a_stub_for_the_resume_target_does_not_break_the_launch(self, sandbox, shell):
        """`restart` degrades to fresh rather than resuming a dead id."""
        agent = build_agent_command("bypass", resume_session_id="stubbed-old")
        d = sandbox.home / ".claude" / "projects" / _key(sandbox.work)
        d.mkdir(parents=True)
        (d / "stubbed-old.jsonl").write_text(STUB)

        result = launch(sandbox, shell, agent)

        assert result.returncode == 0, result.stderr
        assert conversation_flags(invocations(sandbox)[0]) == [
            "--session-id", agent.conversation_id,
        ]
