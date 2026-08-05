"""Shared stateless helpers for the AgentWire CLI.

Pure relocation from ``__main__.py`` (issue #495 Phase 0): tmux probes, env /
agent-command construction, config/path lookups, session metadata, session /
machine resolution, and small output/format utilities. ``__main__`` imports
from here — never the reverse — so there is no circular import.
"""

import datetime
import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .history import HISTORY_DIR_SHELL
from .project_config import (
    BARE,
    DEFAULT_POSTURE,
    POSTURES,
    get_parent_from_config,
    resolve_posture,
)
from .roles import RoleConfig, merge_roles
from .worktree import git_common_dir, git_root, main_worktree, parse_session_name

# Default config directory
CONFIG_DIR = Path.home() / ".agentwire"


def role_prompts_dir() -> Path:
    """Durable home for the per-conversation ``--append-system-prompt`` file (#871).

    This used to be a ``tempfile.NamedTemporaryFile`` under /var/folders, which
    macOS garbage-collects. The launch line references the file by path
    (``--append-system-prompt "$(<path>)"``), so once the GC ran, relaunching a
    session older than the GC window substituted an EMPTY string: the
    conversation came back, the role silently did not. Nothing failed loudly —
    the agent just quietly stopped being a worker/orchestrator/reviewer.

    Keyed by conversation id so the prompt a conversation launched with stays
    recoverable even after its session's roles change.

    A FUNCTION, not the module constant it used to be (#884). A constant
    computed at import time does not follow this repo's established test seam,
    ``monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)`` — a test
    that believes it has isolated the config dir would still resolve to the
    operator's REAL store. Harmless while the only operation was "write a
    file"; a live landmine the moment something SWEEPS this directory.
    """
    return CONFIG_DIR / "role-prompts"


def _check_tmux_installed() -> bool:
    """Check tmux is on PATH; print install hint if not. Returns False on miss."""
    if shutil.which("tmux") is None:
        print("Error: tmux is required but not installed.", file=sys.stderr)
        print(file=sys.stderr)
        if sys.platform == "darwin":
            print("Install with: brew install tmux", file=sys.stderr)
        else:
            print("Install with: sudo apt install tmux", file=sys.stderr)
        print(file=sys.stderr)
        print("More info: https://github.com/tmux/tmux", file=sys.stderr)
        return False
    return True


def _tmux_global_option(name: str) -> str | None:
    """Read a global tmux option from the running server.

    Returns the option value ("on"/"off"/...), or None when no server is
    running or the option can't be read.
    """
    try:
        r = subprocess.run(
            ["tmux", "show-option", "-gv", name],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


@dataclass
class AgentCommand:
    """Result of building an agent command.

    Carries not just the shell command but the full launch identity (#871):
    the conversation UUID we minted, the durable role-prompt file, and the
    posture/role names needed to REGENERATE that prompt later. The flag
    builder is the only place that knows all four, so it stamps them here and
    :func:`record_session_launch` copies them onto disk verbatim — no caller
    can pair a conversation id with the wrong prompt or posture.
    """
    command: str  # The shell command to execute
    role_prompt_path: str | None = None  # Durable --append-system-prompt file (see role_prompts_dir())
    env: dict[str, str] = field(default_factory=dict)  # Secrets to inject via tmux set-environment (keeps keys out of `ps`)
    conversation_id: str | None = None  # UUID passed as `claude --session-id` (None for bare)
    resumed_from: str | None = None  # Conversation this one was forked off, if any
    posture: str = BARE
    roles: list[str] = field(default_factory=list)  # Role NAMES, in merge order
    model: str | None = None  # --model override, if the launch chose one


_UNATTENDED_ENV_KEYS = ("AGENTWIRE_UNATTENDED", "AGENTWIRE_UNATTENDED_ALLOW")


def _capture_unattended_env() -> dict[str, str]:
    """Pop the unattended marker OUT of this process's environment.

    The marker must reach new sessions only via the deliberate
    ``tmux new-session -e K=V`` path (``_with_unattended_env`` below). If it
    stays in ``os.environ``, a ``tmux`` client we spawn while no server is
    running boots the shared tmux server WITH the marker in its process env,
    and from then on every session that server creates — interactive human
    sessions included — inherits it and gets falsely treated as unattended
    (#674). Capturing at import time preserves intended propagation while
    guaranteeing no child process of the CLI can inherit the raw var.
    """
    captured: dict[str, str] = {}
    for key in _UNATTENDED_ENV_KEYS:
        val = os.environ.pop(key, None)
        if val:
            captured[key] = val
    return captured


_UNATTENDED_ENV = _capture_unattended_env()


def _with_unattended_env(env: dict[str, str]) -> dict[str, str]:
    """Propagate the unattended marker into a session being created.

    The scheduler is the single place that decides a dispatch is unattended —
    it seeds ``AGENTWIRE_UNATTENDED[=1]`` (and any per-task
    ``AGENTWIRE_UNATTENDED_ALLOW``) into the dispatch subprocess environment,
    captured here at import (see ``_capture_unattended_env``).
    Every session-creation path funnels its env through here on the way to
    ``tmux new-session -e K=V``, so the marker lands in the new session BEFORE
    the agent launches and the damage-control hook can read it. A child session
    an unattended agent spawns inherits the marker too (defense in depth).

    No leak into interactive sessions: a human's ``agentwire new`` has no such
    var in its environment, so nothing is propagated.
    """
    merged = dict(env)
    for key in _UNATTENDED_ENV_KEYS:
        val = _UNATTENDED_ENV.get(key)
        if val and key not in merged:
            merged[key] = val
    return merged


def _build_tmux_env_flags(env: dict[str, str]) -> list[str]:
    """Build `-e KEY=VAL` flag pairs for `tmux new-session`.

    Prefer this over post-creation `inject_session_env` when creating a fresh
    session with secrets: `tmux new-session -e K=V` places the var in the
    session environment BEFORE the initial shell starts, so that shell sees
    it. `tmux set-environment` on an existing session only affects shells
    spawned AFTER the call, which leaves the initial pane's shell without
    the var — and the agent command runs in that initial shell.
    """
    flags: list[str] = []
    for key, value in _with_unattended_env(env).items():
        flags.extend(["-e", f"{key}={value}"])
    return flags


def _build_tmux_env_flags_shell(env: dict[str, str]) -> str:
    """Shell-quoted `-e 'K=V' …` fragment for inlining via SSH. Trailing space when non-empty."""
    merged = _with_unattended_env(env)
    if not merged:
        return ""
    parts = [f"-e {shlex.quote(f'{k}={v}')}" for k, v in merged.items()]
    return " ".join(parts) + " "


def _set_session_name_env(agent: "AgentCommand", session_name: str, created_by: str | None = None) -> None:
    """Stamp ``AGENTWIRE_SESSION_NAME`` (and, when there's a real parent,
    ``AGENTWIRE_CREATED_BY``) onto an ``AgentCommand.env``.

    Every session created via ``cmd_new`` / ``cmd_spawn`` / ``cmd_recreate``
    / ``cmd_fork`` / scheduler-spawn paths gets ``AGENTWIRE_SESSION_NAME`` so
    downstream tooling (notably the worker damage-control rules in
    ``safety/_core.py``) can identify which agentwire session the running
    tool is part of.

    ``created_by`` of ``''`` (root/orchestrator) or ``None`` means no
    parent — ``AGENTWIRE_CREATED_BY`` is deliberately left UNSET rather than
    set to an empty string, so the bare pre-agent shell's launch-crash guard
    (``_guarded_launch_command``) can tell "has a parent to escalate to"
    apart from "root session, email the owner" with a plain ``-n`` test (#743).
    """
    agent.env["AGENTWIRE_SESSION_NAME"] = session_name
    if created_by:
        agent.env["AGENTWIRE_CREATED_BY"] = created_by


def inject_session_env(session: str, env: dict[str, str], remote_host: str | None = None) -> None:
    """Set env vars on an existing tmux session for FUTURE shells in that session.

    Does NOT update the initial pane's shell — that shell was already started
    when the session was created and has a fixed env. Use
    `_build_tmux_env_flags(env)` with `tmux new-session -e K=V` instead if
    the agent command runs in the initial shell.
    """
    if not env:
        return
    for key, value in env.items():
        if remote_host:
            subprocess.run(
                ["ssh", remote_host, "tmux", "set-environment", "-t",
                 shlex.quote(session), shlex.quote(key), shlex.quote(value)],
                check=False,
            )
        else:
            subprocess.run(
                ["tmux", "set-environment", "-t", session, key, value],
                check=False,
            )


def parse_env_args(env_args: list[str] | None) -> dict[str, str]:
    """Parse repeated `--env KEY=VAL` flags into a dict.

    Raises SystemExit via argparse pattern if an entry lacks `=`.
    """
    if not env_args:
        return {}
    result: dict[str, str] = {}
    for entry in env_args:
        if "=" not in entry:
            print(f"Error: --env expects KEY=VAL, got {entry!r}", file=sys.stderr)
            sys.exit(2)
        key, value = entry.split("=", 1)
        if not key:
            print(f"Error: --env KEY cannot be empty (got {entry!r})", file=sys.stderr)
            sys.exit(2)
        result[key] = value
    return result


# Owner-only, matching the posture `~/.agentwire/.env` is DOCUMENTED to have —
# and, since #887, the posture agentwire actually enforces on every file it
# writes there. Both modes are forced rather than requested, because
# `mkdir(mode=)` and `open(mode=)` are masked by umask and neither touches an
# already-existing path: a file or directory created before this rule, or under
# a permissive umask, must heal on the next write rather than stay
# world-readable forever.
_SECRET_FILE_MODE = 0o600
_SECRET_DIR_MODE = 0o700


def write_owner_only(path: Path, text: str) -> None:
    """Write *text* to *path* atomically and never wider than 0600 (#887).

    The ONE implementation of the fchmod-before-any-bytes-land technique — it
    started life in ``security.write_token_file``, was copied into
    ``write_role_prompt`` by #881, and #887 found the same rule missing from
    the ``machines.json`` writers (a bare ``write_text``, hence the 0644
    registry found in the wild). Three copies is where a technique becomes a
    utility.

    Two properties, both load-bearing:

    - The mode is set on the descriptor BEFORE any bytes land, so the content
      is never briefly group/world readable on disk — not even on first
      creation under a permissive umask.
    - ``os.replace`` swaps the inode, so a rewrite HEALS a file that had
      already drifted wide (the new one is 0600 before it becomes visible),
      and a crash mid-write leaves the previous content intact rather than a
      truncated file.

    A parent directory created here is 0700. An EXISTING parent is left alone:
    tightening the whole of ``~/.agentwire`` is a decision for the operator,
    which is what ``agentwire doctor`` (reporting) and ``doctor --yes``
    (healing) are for.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=_SECRET_DIR_MODE)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, _SECRET_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_role_prompt(conversation_id: str, instructions: str) -> Path:
    """Write *instructions* to this conversation's durable role-prompt file.

    The one place a role prompt is written to disk (see :func:`role_prompts_dir`
    for why "durable" is load-bearing). Returns the path the launch line
    should read; the file deliberately OUTLIVES the launch so a later
    relaunch can reuse it, and the recorded ``roles``/``posture`` can
    regenerate it if it's ever gone. Retention is
    :mod:`agentwire.role_prompts`' job, not this function's (#884).

    A role prompt is complete system-prompt text — role content, project
    context, whatever else the prompt carries — so it is written owner-only
    via :func:`write_owner_only`, and never through a window where it isn't.
    The directory mode is re-asserted on every write because ``mkdir`` never
    touches an existing path: a store created before #881 must heal rather
    than stay world-readable forever.
    """
    store = role_prompts_dir()
    store.mkdir(parents=True, exist_ok=True, mode=_SECRET_DIR_MODE)
    os.chmod(store, _SECRET_DIR_MODE)

    path = store / f"{conversation_id}.txt"
    write_owner_only(path, instructions)
    return path


def mirror_role_prompt_remote(agent: "AgentCommand", machine_id: str, agent_cmd: str) -> str:
    """Copy a role prompt to *machine_id* and repoint the launch line at it.

    The launch line references the prompt BY PATH
    (``--append-system-prompt "$(<path>)"``), so a remote session handed a
    local path reads a file that isn't there and starts with an EMPTY system
    prompt — the same silent role-loss the /var/folders GC caused, just
    reached by a different route. The one implementation of the mirror, used
    by every remote launch (``new`` / ``recreate`` / ``fork``); only ``new``
    had it before, and only into ``/tmp``.

    Mutates ``agent.role_prompt_path`` to the remote path so the recorded
    identity names where the prompt actually lives for that session. Returns
    the rewritten command (unchanged on failure — best-effort, matching the
    rest of the remote path's tolerance).
    """
    if not agent.role_prompt_path or not agent_cmd:
        return agent_cmd
    try:
        content = Path(agent.role_prompt_path).read_text()
    except OSError as e:
        print(f"Warning: Failed to read system prompt: {e}", file=sys.stderr)
        return agent_cmd

    # `$HOME`, not `~`: this path is substituted into the launch line mid-word
    # (`"$(<...)"`) where tilde expansion is not guaranteed, and it is
    # recorded verbatim — the remote's home isn't knowable from here.
    remote_dir = "$HOME/.agentwire/role-prompts"
    remote_prompt = f"{remote_dir}/{agent.conversation_id}.txt"
    # Same owner-only posture as the local store, stated the same way: `umask
    # 077` covers the create, and the explicit chmods cover a pre-existing
    # path (which umask never touches) on a remote whose defaults we don't
    # control. The heredoc body starts on the line after the full command.
    write_cmd = (
        f'umask 077 && mkdir -p "{remote_dir}" && chmod 700 "{remote_dir}" && '
        f'cat > "{remote_prompt}" << \'AGENTWIRE_EOF\'\n{content}\nAGENTWIRE_EOF\n'
        f'chmod 600 "{remote_prompt}"'
    )
    result = _run_remote(machine_id, write_cmd)
    if result.returncode != 0:
        print(f"Warning: Failed to write system prompt to {machine_id}: {result.stderr}",
              file=sys.stderr)
        return agent_cmd

    rewritten = agent_cmd.replace(agent.role_prompt_path, remote_prompt)
    agent.role_prompt_path = remote_prompt
    return rewritten


#: Shell variable holding the conversation flags. An array, because the two
#: branches have different arity — and unquoted word-splitting, the usual way
#: to avoid one, does NOT happen in zsh (the default login shell here), so
#: `claude $flags` would pass one mangled argument instead of two.
_SID_VAR = "aw_flags"


def _conversation_flags_shell(conversation_id: str, resume_session_id: str | None) -> str:
    """Shell prelude choosing ``--session-id`` vs ``--resume`` AT RUNTIME (#901).

    ``--session-id`` is single-use: it hard-errors ("Session ID <id> is
    already in use.") once the conversation's transcript exists. But the
    launch line it sits in is stored as ``AGENTWIRE_LAUNCH_CMD`` precisely so
    it can be re-run (#856/#866) — by the pane, by recovery tooling, by an
    operator. So a session that took even one turn and then exited could
    never be relaunched from its own launch line: claude refused to start and
    the pane sat at a bare shell, permanently. That stranded 13 live sessions.

    A launch line that exists to be re-entered cannot carry a single-use flag,
    and it cannot be decided at BUILD time either, since whether the
    transcript exists depends on when the line is run. So the decision is made
    in shell, against the one predicate the Python side uses —
    ``resumable(id, cwd) == exists(<encoded_cwd>/<id>.jsonl)`` — with the
    encoding mirrored from :data:`history.HISTORY_DIR_SHELL`.

    Three cases, written as precedence (last assignment wins):

    1. Nothing on disk → ``--session-id <new>``. First launch stays
       authoritative about the id, which is #871's whole point.
    2. Re-entry, the conversation exists → ``--resume <new>``. Same
       conversation continues; no new id is minted, so the recorded chain
       stays true.
    3. An explicit resume (``restart``) whose fork hasn't happened yet →
       ``--resume <old> --fork-session --session-id <new>``, unchanged. If
       *old* is gone by then, this falls back to case 1 rather than dying on
       "No conversation found" — degrade to a fresh conversation with the
       role intact, never to a bare shell.
    """
    hist = f"{_SID_VAR}_dir"
    lines = [
        f"{hist}={HISTORY_DIR_SHELL}",
        f'{_SID_VAR}=(--session-id "{conversation_id}")',
    ]
    if resume_session_id:
        lines.append(
            f'[ -f "${hist}/{resume_session_id}.jsonl" ] && '
            f'{_SID_VAR}=(--resume "{resume_session_id}" --fork-session '
            f'--session-id "{conversation_id}")'
        )
    lines.append(
        f'[ -f "${hist}/{conversation_id}.jsonl" ] && '
        f'{_SID_VAR}=(--resume "{conversation_id}")'
    )
    return "; ".join(lines)


def build_agent_command(
    posture: str,
    roles: list[RoleConfig] | None = None,
    model: str | None = None,
    resume_session_id: str | None = None,
) -> AgentCommand:
    """Build the shell command + injected env for the given posture.

    The ONE flag-builder (#729): fresh sessions AND history resume both route
    through here, so a posture always launches with the same flags — no
    create-vs-resume drift. Posture switches the permission-mode flags; ``bare``
    is the no-agent sentinel (empty command); ``resume_session_id`` prepends the
    ``--resume/--fork-session`` pair right after ``claude`` so the resumed
    process still gets its posture's grants (incl. auto's tool-allows).

    Conversation identity (#871): agentwire MINTS the conversation UUID here
    and passes it as ``claude --session-id``, rather than discovering it after
    the fact by watching ``~/.claude/projects/<encoded-cwd>/`` for the newest
    ``.jsonl``. The record on disk is therefore authoritative, not a guess.

    Two verified properties of the flag drive the design:

    - ``--session-id`` REFUSES to start on collision ("Session ID <id> is
      already in use.") — but the check is scoped to the launch cwd, since
      that's what keys the history dir. A fresh uuid4 per call is the only
      safe input; never re-pass a previously recorded id here (that's what
      ``resume_session_id`` is for).
    - ``--resume <old> --fork-session --session-id <new>`` composes: the fork
      lands at the id WE chose, so a resumed session's new conversation is
      just as recorded as a fresh one's. That's what makes
      ``conversation_ids`` a chain rather than a scalar that goes stale on
      the first resume.
    """
    if posture == BARE:
        return AgentCommand(command="", posture=BARE)

    merged = merge_roles(roles) if roles else None
    role_names = [r.name for r in roles] if roles else []
    conversation_id = str(uuid.uuid4())

    # The conversation flags are resolved by the shell at launch, not baked in
    # here — see _conversation_flags_shell for why a stored launch line cannot
    # carry a single-use --session-id (#901).
    prelude = _conversation_flags_shell(conversation_id, resume_session_id)
    parts = ["claude", f'"${{{_SID_VAR}[@]}}"']

    # Permission-mode flags (one per posture; prompted adds none — hooks gate it)
    if posture == "bypass":
        parts.append("--dangerously-skip-permissions")
    elif posture == "auto":
        parts.extend(["--enable-auto-mode", "--permission-mode", "auto"])
        # Inject core allows that bypass the classifier entirely (zero token cost)
        core_allows = [
            "Bash(agentwire *)", "Bash(tmux *)", "Bash(git *)",
            "Bash(gh pr create*)", "Bash(gh pr view*)",
            "Read(*)", "Edit(*)", "Write(*)", "Glob(*)", "Grep(*)",
        ]
        parts.extend(["--allowedTools", shlex.quote(",".join(core_allows))])

    # Model override
    if model:
        parts.append(f"--model {model}")

    # Role-based flags (merged roles always apply — no tool-locking posture left)
    role_prompt_path = None
    if merged:
        if merged.tools:
            parts.append(f"--tools {','.join(merged.tools)}")

        if merged.disallowed_tools:
            parts.append(f"--disallowedTools {','.join(merged.disallowed_tools)}")

        if merged.instructions:
            # Written to a file rather than inlined to avoid shell escaping
            # issues (see docs/wiki/internals/shell-escaping.md), and MUST be
            # the last flag — multiline content can break subsequent args.
            role_prompt_path = str(write_role_prompt(conversation_id, merged.instructions))
            parts.append(f'--append-system-prompt "$(<{role_prompt_path})"')

    return AgentCommand(
        command=f"{prelude}; " + " ".join(parts),
        role_prompt_path=role_prompt_path,
        conversation_id=conversation_id,
        resumed_from=resume_session_id,
        posture=posture,
        roles=role_names,
        model=model,
    )


def check_python_version() -> bool:
    """Verify Python is >= 3.10. Returns False after printing install hint."""
    min_version = (3, 10)
    current_version = sys.version_info[:2]

    if current_version < min_version:
        print(f"⚠️  Python {current_version[0]}.{current_version[1]} detected")
        print(f"   AgentWire requires Python {min_version[0]}.{min_version[1]} or higher")
        print()

        if sys.platform == "darwin":
            print("Install Python 3.12 on macOS:")
            print("  brew install python@3.12")
            print("  # or")
            print("  pyenv install 3.12.0 && pyenv global 3.12.0")
        elif sys.platform.startswith("linux"):
            print("Install Python 3.12 on Ubuntu/Debian:")
            print("  sudo apt update && sudo apt install python3.12")
        else:
            print("Install Python 3.12 from:")
            print("  https://www.python.org/downloads/")

        print()
        return False

    return True


def check_pip_environment() -> bool:
    """Detect a PEP 668 externally-managed interpreter; return False if user must act.

    Applies to Homebrew Python on macOS and Debian/Ubuntu system Python alike.
    Inside a virtualenv (or a uv tool / pipx environment) the marker check is
    skipped — installs there are always fine.
    """
    # Virtualenvs are never externally managed.
    if sys.prefix != sys.base_prefix:
        return True

    # PEP 668: the marker lives in the interpreter's stdlib sysconfig dir.
    marker = Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED"
    if marker.exists():
        print("⚠️  Externally-managed Python environment detected (PEP 668)")
        print()
        print("This Python (e.g. Homebrew on macOS, Debian/Ubuntu system Python)")
        print("blocks bare `pip install` to protect its own packages.")
        print()
        print("Recommended - install as an isolated tool:")
        print("  uv tool install agentwire-dev")
        print("  # or: pipx install agentwire-dev")
        print()
        print("Alternative - a dedicated venv:")
        print("  python3 -m venv ~/.agentwire-venv")
        print("  source ~/.agentwire-venv/bin/activate")
        print("  pip install agentwire-dev")
        print()
        return False

    return True


def generate_certs() -> int:
    """Generate self-signed SSL certificates."""
    cert_dir = CONFIG_DIR
    cert_dir.mkdir(parents=True, exist_ok=True)

    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"

    if cert_path.exists() and key_path.exists():
        print(f"Certificates already exist at {cert_dir}")
        response = input("Overwrite? [y/N] ").strip().lower()
        if response != "y":
            print("Aborted.")
            return 1

    print(f"Generating self-signed certificates in {cert_dir}...")

    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:4096",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "365",
                "-nodes",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Failed to generate certificates: {e.stderr}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("openssl not found. Please install OpenSSL.", file=sys.stderr)
        return 1

    print(f"Created: {cert_path}")
    print(f"Created: {key_path}")
    return 0


def tmux_session_exists(name: str) -> bool:
    """Check if a tmux session exists (exact match)."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"],  # = prefix for exact match
        capture_output=True,
    )
    return result.returncode == 0


def tmux_session_cwd(name: str) -> str | None:
    """Where a live session's agent pane is ACTUALLY running, per tmux.

    The counterpart to the recorded ``cwd_at_launch``: asked of the running
    process rather than read back from our own record, so the two can be
    compared. They diverge exactly when someone relocates a session's
    directory — which is what strands its Claude history under the old key
    (#871 item 5). Returns None when the session isn't live.

    **The agent pane is the FIRST pane, resolved by asking, never by index.**
    Three measured facts force that (all on real tmux, and the first is what
    this function originally got wrong):

    - ``display-message -t "=<session>"`` returns an EMPTY string. A session
      target does not resolve a pane-level format, so the helper answered None
      for every session alive and the check built on it was inert.
    - ``display-message`` does not fail on an unresolvable target either — it
      silently returns the ACTIVE pane. ``-t "=<session>:9.9"`` came back rc=0
      with a plausible path. So a hardcoded index that is wrong doesn't error,
      it lies. ``list-panes`` exits 1 with "can't find window" instead.
    - Indices are not knowable in advance. ``base-index`` and
      ``pane-base-index`` are independent options a ``.tmux.conf`` can set
      either way, and existing windows keep their old index when it changes —
      this machine currently has live sessions at window 1 *and* window 0.

    ``-s`` is load-bearing: without it ``list-panes`` scopes to the session's
    ACTIVE WINDOW, so a session with a second window open (a worker split, an
    artifact window, an operator poking around) answers with the wrong pane.
    With it, panes come back in index order, so line one is the lowest
    window/pane under any base — which is also why an active worker pane can't
    make its orchestrator look moved.
    """
    result = subprocess.run(
        ["tmux", "list-panes", "-s", "-t", f"={name}", "-F", "#{pane_current_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    first = result.stdout.splitlines()[:1]
    return first[0].strip() if first and first[0].strip() else None


def wait_for_shell_prompt(target: str, timeout: float = 2.0) -> None:
    """Poll tmux capture-pane until the shell has drawn a prompt.

    Prevents a race where send-keys fires before the shell is ready, causing
    the command to appear in the pre-prompt buffer and again after the prompt
    renders (looks like it ran twice).
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and any(
            c in result.stdout for c in ("$", "%", "#", "❯", "➜", ">")
        ):
            return
        time.sleep(0.05)


def _live_session_cwd(session: str) -> Path | None:
    """The session's current pane cwd, or None if it isn't a live tmux session.

    Unlike ``_get_session_project_path``, this never falls back to guessing a
    path from the session name — a guessed path is unsafe for an identity
    comparison (#715's same-project check needs the real cwd or nothing).
    """
    if not tmux_session_exists(session):
        return None
    result = subprocess.run(
        ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return None


def _get_session_project_path(session: str) -> Path | None:
    """Get a session's project path from tmux cwd, falling back to session name parsing."""
    live = _live_session_cwd(session)
    if live is not None:
        return live

    # Fallback: derive from session name
    config = load_config()
    projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser()
    project, _, _ = parse_session_name(session)
    return projects_dir / project


def _same_project(path_a: Path, path_b: Path) -> bool:
    """True when two paths belong to the same git repo (shared .git dir
    across linked worktrees) or, outside a repo, are the same resolved path."""
    common_a = git_common_dir(path_a)
    if common_a is None:
        return path_a.resolve() == path_b.resolve()
    common_b = git_common_dir(path_b)
    if common_b is None:
        return path_a.resolve() == path_b.resolve()
    return common_a == common_b


def resolve_default_created_by(caller: str | None, target_path: Path) -> str | None:
    """The default ``created_by`` when none was explicitly given.

    Inherit the caller only when the new session's project is the one the
    caller is already running in — a cross-project spawn gets its own root
    instead of flattening into the caller's subtree (#715). An explicit
    --created-by always wins and never reaches this function.

    Uses ``_live_session_cwd`` rather than ``_get_session_project_path`` —
    the latter's session-name-guessing fallback isn't a safe basis for an
    identity comparison (it doesn't understand the worktree naming scheme,
    `{project}-{name}`, and would misjudge same/cross-project); if the
    caller's real cwd can't be confirmed, treat it as unknown (no inheritance)
    rather than risk a wrong guess.

    A service session (agentwire-portal, -tts, -stt, -kokoro, ...) never
    qualifies as a parent: it's infrastructure, not an agent that will ever
    drain its msg inbox, so anything parented to it dead-letters forever. A
    subprocess the portal shells out to inherits its TMUX_PANE and resolves
    ``caller`` right back to "agentwire-portal" — this was the root cause of
    a 147-message dead-letter storm (each mailing the owner individually,
    2026-07-19).
    """
    if not caller:
        return None
    from .services import is_service_session

    if is_service_session(caller):
        return None
    caller_path = _live_session_cwd(caller)
    if caller_path is None:
        return None
    return caller if _same_project(caller_path, target_path) else None


def tmux_session_has_agent(name: str) -> bool:
    """Check if a tmux session has an agent running (not just a bare shell).

    Returns True if any pane is running claude or similar agent.
    Returns False if all panes are just zsh/bash (agent died or never started).
    """
    result = subprocess.run(
        ["tmux", "list-panes", "-t", f"={name}", "-F", "#{pane_current_command}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    bare_shells = {"zsh", "bash", "sh", "fish", "tcsh", "csh"}
    for line in result.stdout.strip().split("\n"):
        if line.strip().lower() not in bare_shells:
            return True

    return False


def load_config() -> dict:
    """Load configuration from ~/.agentwire/config.yaml."""
    config_path = CONFIG_DIR / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def get_source_dir() -> Path:
    """Get the agentwire source directory from config.

    Precedence: AGENTWIRE_SOURCE_DIR env var, then dev.source_dir from
    config.yaml, then ~/projects/agentwire-dev. The path is not validated —
    use find_source_checkout() when the caller needs a real checkout.
    """
    env_dir = os.environ.get("AGENTWIRE_SOURCE_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    config = load_config()
    source_dir = config.get("dev", {}).get("source_dir", "~/projects/agentwire-dev")
    return Path(source_dir).expanduser()


# Where a git clone of agentwire-dev conventionally lives. Checked in order
# after the explicit env/config location; a pip/uv-tool-only install has none
# of these, and callers must degrade clearly rather than crash.
_SOURCE_SEARCH_DIRS = (
    "~/projects/agentwire-dev",
    "~/agentwire-dev",
    "~/src/agentwire-dev",
    "~/code/agentwire-dev",
)


def find_source_checkout() -> Path | None:
    """Locate an agentwire-dev source checkout, or None on a package-only install.

    A directory counts as a checkout when it holds a pyproject.toml. The
    explicitly configured location (env var / config) wins; otherwise the
    conventional clone locations are searched.
    """
    candidates = [get_source_dir()]
    candidates += [Path(p).expanduser() for p in _SOURCE_SEARCH_DIRS]
    for candidate in candidates:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return None


def get_portal_session_name() -> str:
    """Get portal tmux session name from config."""
    config = load_config()
    return config.get("services", {}).get("portal", {}).get("session_name", "agentwire-portal")


def get_tts_session_name() -> str:
    """Get TTS tmux session name from config."""
    config = load_config()
    return config.get("services", {}).get("tts", {}).get("session_name", "agentwire-tts")


def get_stt_session_name() -> str:
    """Get STT tmux session name from config."""
    config = load_config()
    return config.get("services", {}).get("stt", {}).get("session_name", "agentwire-stt")


def get_kokoro_session_name() -> str:
    """Get default-tier Kokoro TTS shim tmux session name from config."""
    config = load_config()
    return config.get("services", {}).get("kokoro", {}).get("session_name", "agentwire-kokoro")


def _get_machine_config(machine_id: str) -> dict | None:
    """Load machine config from machines.json.

    Returns:
        Machine dict with id, host, user, projects_dir, etc.
        None if machine not found.
    """
    machines_file = CONFIG_DIR / "machines.json"
    if not machines_file.exists():
        return None

    try:
        with open(machines_file) as f:
            machines_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return None

    machines = machines_data.get("machines", [])
    for m in machines:
        if m.get("id") == machine_id:
            return m

    return None


def _parse_session_target(name: str) -> tuple[str, str | None]:
    """Parse 'session@machine' into (session, machine_id).

    Examples:
        "myapp" -> ("myapp", None)
        "myapp@gpu-server" -> ("myapp", "gpu-server")
        "myapp/feature@gpu-server" -> ("myapp/feature", "gpu-server")
    """
    if "@" in name:
        session, machine = name.rsplit("@", 1)
        return session, machine
    return name, None


def _get_all_machines() -> list[dict]:
    """Get list of all registered machines from machines.json."""
    machines_file = CONFIG_DIR / "machines.json"
    if not machines_file.exists():
        return []

    try:
        with open(machines_file) as f:
            machines_data = json.load(f)
            return machines_data.get("machines", [])
    except (json.JSONDecodeError, IOError):
        return []


def _output_json(data: dict) -> None:
    """Output JSON to stdout."""
    print(json.dumps(data, indent=2))


def _output_result(success: bool, json_mode: bool, message: str = "", exit_code: int | None = None, **kwargs) -> int:
    """Output result in text or JSON mode.

    Args:
        success: Whether the operation succeeded
        json_mode: Output JSON if True
        message: Message to display
        exit_code: Custom exit code (default: 0 if success, 1 otherwise)
        **kwargs: Additional JSON fields

    Returns:
        exit_code if provided, else 0 if success, 1 otherwise
    """
    if json_mode:
        result = {"success": success, **kwargs}
        if not success and "error" not in result:
            result["error"] = message
        if exit_code is not None:
            result["exit_code"] = exit_code
        _output_json(result)
    else:
        if message:
            if success:
                print(message)
            else:
                print(message, file=sys.stderr)
    if exit_code is not None:
        return exit_code
    return 0 if success else 1


def load_session_metadata(session_name: str) -> dict:
    """Load session metadata from storage.

    Args:
        session_name: The session name (without @machine suffix if present)

    Returns:
        Dictionary of metadata (empty dict if not found)
    """
    # Parse session name to extract just the name part (remove @machine)
    clean_name = session_name.split("@")[0]

    metadata_file = CONFIG_DIR / "sessions" / clean_name / "metadata.json"

    if not metadata_file.exists():
        return {}

    try:
        with open(metadata_file) as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, IOError):
        return {}


def session_metadata_path(session_name: str) -> Path:
    """Where a session's record lives. One implementation, both directions."""
    return CONFIG_DIR / "sessions" / session_name.split("@")[0] / "metadata.json"


def recorded_sessions() -> list[str]:
    """Every session name with a launch record on disk, sorted.

    The enumeration counterpart to :func:`load_session_metadata` — one place
    knows the store's layout, so a sweep (``doctor``) can't drift from the
    reader. Names only; a caller loads what it needs. These are RECORDS, not
    live sessions: a name here may long since have been killed.

    The glob is ``**/metadata.json`` for the reason #884 had to fix in
    :func:`role_prompts.reachable_conversation_ids`: session names contain
    slashes by design (``project/branch`` is what every ``agentwire worktree``
    and every scheduler dispatch is called), so :func:`session_metadata_path`
    nests those records one level deeper. A flat scan found 469 of 1106
    records on this machine — a sweep built on it would silently skip 58% of
    the fleet while reporting itself clean.
    """
    sessions_dir = CONFIG_DIR / "sessions"
    try:
        return sorted(
            str(f.parent.relative_to(sessions_dir))
            for f in sessions_dir.glob("**/metadata.json")
        )
    except OSError:
        return []


def store_session_metadata(session_name: str, metadata: dict) -> None:
    """Store session metadata to disk — RAISING if it doesn't land (#885).

    This used to end in ``except (IOError, TypeError): pass``, which made a
    failed write indistinguishable from a successful one. That was survivable
    while the record only held ``created_by``/``role`` (losing it degraded
    prompt routing, visibly). Since #871 it holds the conversation id — the
    one piece of session identity that is NOT otherwise recoverable — so a
    dropped write means a session that can never be resumed, reported at the
    time as success. The whole epic assumes this file is on disk; the write is
    the wrong place to be forgiving.

    Two failure modes, deliberately distinguished:

    - ``TypeError`` — the metadata isn't JSON-serializable, i.e. a code bug.
      Serialized BEFORE anything is opened, so the bug surfaces as a crash and
      cannot truncate a good record on its way out.
    - ``OSError`` — the store isn't writable. Callers that can survive it (see
      :func:`record_session_launch`, whose session is already running) catch
      it and warn; nothing swallows it silently.

    Written via :func:`_atomic_write`, so a crash mid-write leaves the previous
    record intact rather than a truncated file that
    :func:`load_session_metadata` would then read back as ``{}``.

    Args:
        session_name: The session name (without @machine suffix if present)
        metadata: Dictionary of metadata to store
    """
    text = json.dumps(metadata, indent=2)
    _atomic_write(session_metadata_path(session_name), text)


def git_identity(cwd) -> dict:
    """The ``repo`` / ``branch`` / ``worktree_path`` triple for a launch cwd.

    Asked of GIT, never string-built from a naming convention — the same rule
    that #837 had to retrofit onto worktree paths and #868 onto session names.
    ``worktree_path`` is None when *cwd* is the repo's MAIN checkout, so its
    presence alone answers "is this session running in a linked worktree".

    Every value is None off-repo (and for a remote session's path, which
    doesn't exist locally); a caller must read a missing key as "unknown",
    never as "the conventional value".
    """
    blank = {"repo": None, "branch": None, "worktree_path": None}
    path = Path(cwd)
    if not path.exists():
        return blank

    top = git_root(path)
    if top is None:
        return blank

    main = main_worktree(top)
    branch = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    branch_name = branch.stdout.strip() if branch.returncode == 0 else ""
    # Detached HEAD reports the literal "HEAD" — not a branch, so record none.
    if branch_name == "HEAD":
        branch_name = ""

    return {
        "repo": str(main),
        "branch": branch_name or None,
        "worktree_path": str(top) if top.resolve() != Path(main).resolve() else None,
    }


def record_session_launch(
    session_name: str,
    agent: "AgentCommand",
    cwd,
    *,
    created_by: str | None = None,
    created_via: str | None = None,
    role: str | None = None,
    remote: bool = False,
) -> dict:
    """Record a session's launch identity — the ONE writer of metadata.json (#871).

    Every path that starts an agent in a tmux SESSION calls this exactly once,
    right after the launch: ``new`` (and therefore ``worktree`` /
    ``orchestrator`` / scheduler+ensure dispatch, which all delegate to it),
    ``recreate``, ``fork``, ``history resume``, and ``dev``. Routing them all
    through one function is the point: a creation path that hand-rolls its own
    record is exactly how the worktree-path (#837) and session-name (#868)
    conventions each drifted into a bug that reported success while doing
    nothing.

    Deliberately NOT called by ``spawn`` — a worker pane is not a session, and
    this store is keyed by session name, so a pane recording here would
    overwrite its OWNING session's record. Panes still get a minted
    conversation id and a durable role prompt from ``build_agent_command``;
    they just have nowhere session-scoped to write it.

    What comes from where:

    - ``agent`` supplies conversation id, role-prompt path, posture and role
      names — the flag builder is the only thing that knows them, and taking
      the whole object means a caller can't pair them wrong.
    - ``cwd`` supplies ``cwd_at_launch`` verbatim plus the git-derived
      repo/branch/worktree triple. ``cwd_at_launch`` is what a later check
      compares against Claude's own history key
      (``~/.claude/projects/<encoded-cwd>/``) to detect history orphaned by a
      moved worktree. ``remote=True`` records the path but skips the git
      derivation — the path lives on another machine, and a same-named local
      directory would otherwise answer with some other repo's branch.
    - ``roles`` + ``posture`` are recorded to REGENERATE the system prompt,
      not merely to reference it — a role-prompt file that has gone missing
      is recoverable from them.

    Merge-preserving, and ``conversation_ids`` APPENDS: ``--fork-session``
    mints a new id on every resume, so identity is a chain, not a scalar.

    A write that fails is WARNED about, never raised (#885): by the time this
    runs the session is already live in tmux, so a traceback here would turn a
    successful creation into a failed command while leaving the session
    running. Silence was the actual bug — the warning names the session, the
    conversation id that is now unrecoverable, and what breaks because of it.
    """
    clean_name = session_name.split("@")[0]
    metadata = load_session_metadata(session_name)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # ``created_by`` of ``''`` means "explicitly rootless" and must still be
    # written — otherwise a re-`new`/`recreate` that forces standalone (e.g.
    # `--created-by ''`) leaves a stale parent from a prior creation in place,
    # since `not created_by` is also true for `''`. ``None`` means the caller
    # has no opinion, and must not clobber what's already recorded.
    if created_by is not None and created_by != clean_name:
        metadata["created_by"] = created_by
        if created_via:
            metadata["created_via"] = created_via

    # The ROLE axis (orchestrator/worker/reviewer) — distinct from the
    # etiquette/persona ``roles`` list below. Read back by
    # list_local_sessions() and the session_created notify lookup.
    if role:
        metadata["role"] = role

    if agent.conversation_id:
        chain = list(metadata.get("conversation_ids") or [])
        if agent.conversation_id not in chain:
            chain.append(agent.conversation_id)
        metadata["conversation_ids"] = chain

    metadata.update({
        "cwd_at_launch": str(cwd),
        "posture": agent.posture,
        "roles": list(agent.roles),
        # The model override belongs with roles/posture for the same reason:
        # these three are what REGENERATE the launch flags. Recording two of
        # them means `agentwire restart` silently drops the third and hands the
        # conversation back on a different model than it was running on.
        "model": agent.model,
        "role_prompt_path": agent.role_prompt_path,
        "launched_at": now,
        **({"repo": None, "branch": None, "worktree_path": None}
           if remote else git_identity(cwd)),
    })
    metadata.setdefault("created_at", now)

    try:
        store_session_metadata(session_name, metadata)
    except (OSError, TypeError) as e:
        # TypeError is a CODE bug (unserializable record) and OSError an
        # environment one; both are reported the same way here because the
        # response is the same — the session is up, its identity is not on
        # disk, and the operator has to know now rather than at the next
        # `history resume`.
        print(
            f"Warning: session '{clean_name}' launched but its identity was "
            f"NOT recorded: {type(e).__name__}: {e}\n"
            f"         {session_metadata_path(session_name)}\n"
            f"         Conversation {agent.conversation_id or '(none)'} is not "
            "recoverable from disk — `agentwire history resume`, prompt "
            "routing and the topology view will not find this session.",
            file=sys.stderr,
        )
    return metadata


def notify_portal_session_created(session_name: str, parent: str | None, role: str | None) -> None:
    """Tell the portal a session was just created, with topology context (#747).

    Fires immediately from the creating process instead of relying solely on
    the global tmux ``session-created`` hook, which only knows the bare
    session name (no parent/role) and can race a slow first-message delivery
    before metadata is written. Fire-and-forget — failures are silently
    ignored since the portal may not be running.
    """
    import ssl

    payload: dict = {"event": "session_created", "session": session_name}
    if parent:
        payload["parent"] = parent
    if role:
        payload["role"] = role

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            f"{_default_portal_url()}/api/notify",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **_portal_auth_headers()},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3, context=ctx)
    except Exception:
        pass


def _display_parent(session_name: str, path: str = "") -> "str | None":
    """The session that should visually own this one in the sidebar.

    Display-only relationship (powers sidebar nesting, issue #448) — NOT a
    lifecycle coupling. Mirrors prompt_router.resolve_parent's precedence for
    pane-0 sessions, minus the liveness check (the sidebar decides whether to
    nest based on whether the parent is actually in the list):
      1. Creator recorded at `agentwire new` time (session metadata).
      2. `.agentwire.yml` `parent:` field (from the session's path).
    Returns None for top-level sessions (no recorded parent).
    """
    bare = session_name.split("@")[0]
    creator = load_session_metadata(bare).get("created_by")
    if isinstance(creator, str) and creator and creator != bare:
        return creator
    try:
        parent = get_parent_from_config(Path(path) if path else None)
    except Exception:
        parent = None
    if parent and parent != bare:
        return parent
    return None


def format_relative_time(timestamp_ms: int) -> str:
    """Format timestamp as relative time (e.g., '2 hours ago')."""
    from datetime import datetime

    dt = datetime.fromtimestamp(timestamp_ms / 1000)
    delta = datetime.now() - dt

    seconds = delta.total_seconds()

    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif seconds < 604800:
        days = int(seconds / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    else:
        weeks = int(seconds / 604800)
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"


# === Cross-group shared helpers (Phase 0.5 sweep, #495) ===

def _default_portal_url() -> str:
    """Default portal URL — scheme mirrors the typed config's logic: https
    only when server.ssl cert/key are configured AND exist on disk."""
    ssl_cfg = load_config().get("server", {}).get("ssl", {})
    cert, key = ssl_cfg.get("cert"), ssl_cfg.get("key")
    enabled = bool(
        cert and key
        and Path(os.path.expanduser(cert)).exists()
        and Path(os.path.expanduser(key)).exists()
    )
    return f"{'https' if enabled else 'http'}://localhost:8765"


def _run_remote(machine_id: str, command: str) -> subprocess.CompletedProcess:
    """Run command on remote machine via SSH.

    Args:
        machine_id: Machine ID from machines.json
        command: Shell command to run

    Returns:
        subprocess.CompletedProcess with stdout, stderr, returncode
    """
    machine = _get_machine_config(machine_id)
    if machine is None:
        # Return a failed result
        result = subprocess.CompletedProcess(
            args=["ssh", machine_id, command],
            returncode=1,
            stdout="",
            stderr=f"Machine '{machine_id}' not found in machines.json",
        )
        return result

    host = machine.get("host", machine_id)
    user = machine.get("user")
    port = machine.get("port")

    # Build SSH target
    if user:
        ssh_target = f"{user}@{host}"
    else:
        ssh_target = host

    # Build SSH command with optional port and connection timeout
    ssh_cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([ssh_target, command])

    try:
        return subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=10,  # Hard timeout for command execution
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=ssh_cmd,
            returncode=1,
            stdout="",
            stderr=f"SSH connection to {machine_id} timed out",
        )


def _guarded_launch_command(path_str: str, agent_cmd: str | None) -> str:
    """Build the pane's ``cd <path> && <agent_cmd>`` line with a missing-dir guard (#739).

    A crashed worktree create can leave ``path_str`` absent by the time the
    pane actually runs its ``cd`` (race between ``agentwire new`` reporting
    success and the async pane launch, or an external actor removing the
    dir). Without a guard, ``cd`` fails, the shell prints an error and
    carries on, and ``agent_cmd`` (e.g. ``claude``) then launches from the
    WRONG cwd and crashes — dropping the pane to a bare shell that the
    idle-reaper never touches (it only reaps a *running* agent going idle),
    so it lingers forever.

    On a missing dir this instead alerts and exits the shell, which tears the
    tmux session down instead of leaving a zombie. ``agent_cmd`` never runs.

    Two alert routes, both shell-runtime-gated on env vars stamped at launch
    (``_set_session_name_env``), so this function stays a pure string builder
    with no Python-level knowledge of the session's parent (#743):
    - A real recorded parent (``$AGENTWIRE_CREATED_BY`` set) gets the crash
      escalated to its msg inbox — in-band, not just an email the human has
      to forward.
    - No parent (root/orchestrator sessions, the genuine scheduler-dispatch
      case) falls back to the original owner email, still gated on
      ``$AGENTWIRE_UNATTENDED=1`` (see ``_with_unattended_env``).
    """
    quoted_path = shlex.quote(path_str)
    session_ref = "${AGENTWIRE_SESSION_NAME:-unknown session}"
    missing_body = f"cd failed at launch: {path_str}"
    notify_parent = (
        '[ -n "$AGENTWIRE_CREATED_BY" ] && agentwire msg send --to "$AGENTWIRE_CREATED_BY" '
        f'--kind escalation --subject "agentwire: worktree missing at launch — {session_ref}" '
        f'--body "{missing_body}" >/dev/null 2>&1'
    )
    notify_owner = (
        '[ -z "$AGENTWIRE_CREATED_BY" ] && [ "$AGENTWIRE_UNATTENDED" = "1" ] && agentwire email '
        f'--subject "agentwire: worktree missing — {session_ref}" '
        f'--body "{missing_body}" >/dev/null 2>&1'
    )
    alert = (
        f"echo \"agentwire: worktree missing at launch, aborting: {path_str}\" >&2; "
        f"{notify_parent}; {notify_owner}"
    )
    guard = f"cd {quoted_path} || {{ {alert}; exit 1; }}"
    # Braces, not a bare `&& {agent_cmd}`: the agent command is now several
    # statements (the #901 conversation-flag prelude, then `claude`), and an
    # unbraced `;` would break the guard's chain — `claude` would run even
    # after a failed `cd`, from the wrong directory, which is the exact
    # zombie this function exists to prevent.
    return f"{guard} && {{ {agent_cmd}; }}" if agent_cmd else guard


# The pane's launch command travels as an ENV VAR, not as keystrokes (#856).
#
# `send-keys` fires 0.1s after `tmux new-session`, before the shell has put
# its tty into raw mode, so the keystrokes land in the tty's CANONICAL-mode
# input buffer — which is capped at 1024 bytes per line on macOS
# (MAX_CANON/`N_TTY_BUF_SIZE`; Linux is 4096). Everything past the cap is
# discarded SILENTLY, and since the launch line ends in
# `--append-system-prompt "$(</tmp/…)"`, a truncated one is syntactically
# incomplete: zsh sits at a continuation prompt forever, `claude` never runs,
# and the session is a bare shell that `wait_for_session_ready` can only
# report as "Agent not running". #742/#743 grew `_guarded_launch_command` by
# ~700 chars, which pushed long-named worktree sessions (the scheduler's
# `scheduler-<task>-<timestamp>`, whose path is interpolated FOUR times) over
# the cap — deterministically, since the length is a pure function of the
# session name.
#
# tmux `-e` is not keyboard input, so it has no such cap. Sending a fixed
# ~70-char `eval` line instead makes the launch length-independent.
LAUNCH_CMD_ENV = "AGENTWIRE_LAUNCH_CMD"
_LAUNCH_EVAL = f'eval "${{{LAUNCH_CMD_ENV}:?agentwire: launch command not injected}}"'


def _launch_tmux_session(
    session_name: str,
    session_path,
    env: dict[str, str],
    agent_cmd: str | None,
    machine_id: str | None = None,
) -> subprocess.CompletedProcess | None:
    """Create a detached tmux session at *session_path* and start the agent.

    The one launch sequence shared by ``new`` / ``recreate`` / ``fork`` (#630):
    `tmux new-session -e K=V` injects *env* into the session environment
    BEFORE the initial shell starts (post-hoc `set-environment` never reaches
    it), then `send-keys` runs the guarded launch line (see
    ``_guarded_launch_command``) which cd's into place and, if *agent_cmd* is
    non-empty, starts the agent after a short settle.

    That launch line rides in as ``AGENTWIRE_LAUNCH_CMD`` and is `eval`'d by
    the pane, rather than being typed out in full — see
    :data:`LAUNCH_CMD_ENV` for why typing it is length-limited and fails
    silently.

    Local (machine_id None): runs subprocess calls with check=True (raises on
    tmux failure) and returns None. Remote: runs one composite shell command
    over SSH and returns the CompletedProcess for the caller to check.
    """
    import time

    path_str = str(session_path)
    launch_cmd = _guarded_launch_command(path_str, agent_cmd)
    # Copy, never mutate: callers reuse `agent.env` after the launch.
    launch_env = {**env, LAUNCH_CMD_ENV: launch_cmd}
    if machine_id:
        env_flags = _build_tmux_env_flags_shell(launch_env)
        create_cmd = (
            f"tmux new-session -d -s {shlex.quote(session_name)} -c {shlex.quote(path_str)} {env_flags}&& "
            f"sleep 0.1 && "
            f"tmux send-keys -t {shlex.quote(session_name)} {shlex.quote(_LAUNCH_EVAL)} Enter"
        )
        return _run_remote(machine_id, create_cmd)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", path_str,
         *_build_tmux_env_flags(launch_env)],
        check=True,
    )
    time.sleep(0.1)
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, _LAUNCH_EVAL, "Enter"],
        check=True,
    )
    return None


def _graceful_kill(session_name: str, machine_id: str | None = None) -> None:
    """Ask the agent to /exit, wait, then kill the tmux session.

    The graceful-kill dance shared by ``new -f`` / ``recreate`` (#630).
    Tolerant on every step — a missing session or dead agent never fails the
    caller (kill-session errors are suppressed / captured).
    """
    import time

    if machine_id:
        kill_cmd = (
            f"tmux send-keys -t {shlex.quote(session_name)} /exit Enter 2>/dev/null; "
            f"sleep 2; "
            f"tmux kill-session -t {shlex.quote(session_name)} 2>/dev/null"
        )
        _run_remote(machine_id, kill_cmd)
        return
    subprocess.run(["tmux", "send-keys", "-t", session_name, "/exit", "Enter"])
    time.sleep(2)
    subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)


def _notify_portal_sessions_changed():
    """Notify portal that sessions have changed so it can broadcast to clients.

    This is fire-and-forget - failures are silently ignored since the portal
    may not be running.
    """
    import ssl

    try:
        # Create SSL context that doesn't verify (localhost self-signed cert)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            f"{_default_portal_url()}/api/sessions/refresh",
            method="POST",
            data=b"",
            headers=_portal_auth_headers(),
        )
        urllib.request.urlopen(req, timeout=2, context=ctx)
    except Exception:
        # Portal may not be running - that's fine
        pass


def _portal_auth_headers() -> dict:
    """Headers carrying the portal auth token, if one is configured."""
    from .security import get_local_portal_token

    token = get_local_portal_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def portal_request(
    method: str,
    url: str,
    *,
    json: dict | None = None,
    files: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10,
):
    """The one canonical portal HTTP call (#632).

    Attaches the portal auth token and talks to the localhost self-signed
    cert (verify=False, warnings suppressed). Returns the `requests`
    Response; raises `requests` exceptions — callers own error handling.
    """
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return requests.request(
        method,
        url,
        json=json,
        files=files,
        headers={**_portal_auth_headers(), **(headers or {})},
        verify=False,
        timeout=timeout,
    )


def _atomic_write(path: Path, text: str, validate=None) -> None:
    """Write `text` to `path` atomically: temp file -> fsync -> validate -> rename.

    The file is never left half-written: a crash mid-write leaves the original
    intact and only a discardable .tmp behind. `validate(tmp_path)` (if given)
    must raise on bad content — the rename is skipped and the temp removed,
    so corrupt content can never replace a good file (#449).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if validate is not None:
            validate(tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_portal_health(url: str, timeout: int = 2) -> bool:
    """Check if portal is responding at URL."""
    import ssl

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.urlopen(f"{url}/health", context=ctx, timeout=timeout)
        return req.status == 200
    except Exception:
        return False


def _get_portal_url() -> str:
    """Get portal URL from config, with smart fallbacks.

    Uses NetworkContext to determine the best URL:
    - If portal is local: use localhost
    - If portal is remote with tunnel: use localhost (tunnel port)
    - If portal is remote without tunnel: use direct URL
    """
    from .network import NetworkContext

    ctx = NetworkContext.from_config()

    if ctx.is_local("portal"):
        # Portal runs locally — scheme comes from services.portal.scheme
        # (http unless SSL certs exist or explicitly configured)
        return ctx.get_service_url("portal")

    # Portal is remote - check if tunnel exists by testing localhost first
    tunnel_url = ctx.get_service_url("portal", use_tunnel=True)
    direct_url = ctx.get_service_url("portal", use_tunnel=False)

    # Try tunnel first (more common setup)
    if _check_portal_health(tunnel_url):
        return tunnel_url

    # Fall back to direct connection
    return direct_url


def _get_agentwire_path() -> str:
    """Get the full path to the agentwire executable.

    Checks config first, then falls back to shutil.which() to find it in PATH.
    This ensures tmux hooks work even when run-shell has a minimal PATH.
    """
    import shutil

    config = load_config()
    configured_path = config.get("executables", {}).get("agentwire")

    if configured_path:
        return os.path.expanduser(configured_path)

    # Find agentwire in PATH
    found = shutil.which("agentwire")
    if found:
        return found

    # Fallback to common location
    return os.path.expanduser("~/.local/bin/agentwire")


def _post_desktop_notification(text: str, session: str | None = None, priority: str = "normal",
                               timeout: float | None = None) -> bool:
    """POST a toast to the portal's desktop-notification endpoint. Best-effort.

    Shared by `agentwire notify-user` and the `say --display` path. Returns True
    on a 2xx, False on any failure (no portal, network error) — never raises.
    `timeout` (seconds) overrides the frontend's auto-fade default; 0 = sticky.
    """
    import ssl

    body: dict = {"text": text, "priority": priority}
    if session:
        body["session"] = session
    if timeout is not None:
        body["timeout"] = timeout
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(
            f"{_get_portal_url()}/api/desktop/notification",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **_portal_auth_headers()},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=5):
            return True
    except Exception:
        return False


def _resolve_posture_from_args(args) -> tuple[str | None, str | None]:
    """Resolve the session's posture from the shared spawn-flag core.

    Posture is the ONLY session axis (#729), and every spawn defaults to the
    same one — bypass — regardless of kind/topology: workers run bypass +
    damage-control just like orchestrators, no tool-locking. Precedence:
    explicit --posture first; else the internal --bare/--prompted booleans
    (set by cmd_worktree / cmd_recreate callers); else the default posture.

    Returns ``(posture, error)`` — error is a message string when an invalid
    posture was given, posture is None in that case.
    """
    posture = getattr(args, 'posture', None)
    if posture:
        try:
            return resolve_posture(posture), None
        except ValueError as e:
            return None, str(e)
    if getattr(args, 'bare', False):
        return BARE, None
    if getattr(args, 'prompted', False):
        return "prompted", None
    return DEFAULT_POSTURE, None


def _resolve_posture_or_config(args, project_config, default: str = DEFAULT_POSTURE) -> tuple[str | None, str | None]:
    """Posture for recreate/fork: explicit --posture wins, else the source
    config's posture, else *default*. Returns ``(posture, error)``."""
    posture = getattr(args, 'posture', None)
    if posture:
        try:
            return resolve_posture(posture), None
        except ValueError as e:
            return None, str(e)
    cfg_posture = getattr(project_config, 'posture', None) if project_config else None
    if cfg_posture:
        try:
            return resolve_posture(cfg_posture), None
        except ValueError:
            return default, None
    return default, None


def _add_posture_flag(parser) -> None:
    """Register the canonical posture axis on a spawn-verb parser (#729).

    Accepts ``bare`` too (the no-agent sentinel) so a bare session can be
    re-specified on recreate/fork through the one axis flag.
    """
    parser.add_argument("--posture", choices=[*POSTURES, BARE],
                        help="Permission mode the agent runs under: bypass/prompted/auto "
                             "(or bare for no agent). Default: bypass.")


def _git_behind_origin(repo: Path, base: str = "main", do_fetch: bool = True):
    """How many commits ``origin/<base>`` is ahead of the checkout's HEAD.

    Returns ``(behind, error)``: ``behind`` is the commit count (0 = up to date),
    or ``None`` with a human-readable ``error`` string when the comparison can't
    be made (not a git repo, no remote, offline fetch failure, etc.).
    """
    if not (repo / ".git").exists():
        return None, "not a git checkout"
    if do_fetch:
        fetch = subprocess.run(
            ["git", "fetch", "origin", base],
            cwd=repo, capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            return None, (fetch.stderr or fetch.stdout or "git fetch failed").strip()
    count = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..origin/{base}"],
        cwd=repo, capture_output=True, text=True,
    )
    if count.returncode != 0:
        return None, (count.stderr or count.stdout or "git rev-list failed").strip()
    try:
        return int(count.stdout.strip()), None
    except ValueError:
        return None, f"unexpected rev-list output: {count.stdout.strip()!r}"


def _start_portal_local(args, attach: bool = True) -> int:
    """Start portal locally in tmux.

    When attach is False (used by `agentwire up`), the portal is started
    detached and we return without attaching.
    """
    session_name = get_portal_session_name()

    if tmux_session_exists(session_name):
        print(f"Portal already running in tmux session '{session_name}'")
        if attach:
            print("Attaching... (Ctrl+B D to detach)")
            subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    # No tunnel auto-spawn (#420): agentwire owns only the local portal
    # boundary. Reaching the portal from elsewhere is bring-your-own
    # (cloudflared/tailscale/ssh -L), and `agentwire tunnels *` remains as an
    # opt-in manual helper for the vestigial remote-service-split case.

    # Build the server command
    # --dev runs from source with uv run (picks up code changes immediately)
    if getattr(args, 'dev', False):
        cmd_parts = ["uv", "run", "python", "-m", "agentwire", "portal", "serve"]
    else:
        cmd_parts = ["agentwire", "portal", "serve"]

    if args.port:
        cmd_parts.extend(["--port", str(args.port)])
    if args.host:
        cmd_parts.extend(["--host", args.host])
    if args.no_tts:
        cmd_parts.append("--no-tts")
    if args.no_stt:
        cmd_parts.append("--no-stt")
    if args.config:
        cmd_parts.extend(["--config", str(args.config)])

    server_cmd = " ".join(cmd_parts)

    # Create tmux session and start server
    mode = "dev mode (from source)" if getattr(args, 'dev', False) else "installed"
    print(f"Starting AgentWire portal ({mode}) in tmux session '{session_name}'...")
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name,
    ])
    subprocess.run([
        "tmux", "send-keys", "-t", session_name, server_cmd, "Enter",
    ])

    # Install global tmux hooks for portal sync
    _install_global_tmux_hooks()

    # Custom services (incl. the notifications bridge) are autostarted by the
    # portal server itself on launch — see run_server() in server.py.

    if attach:
        print("Portal started. Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
    else:
        print("Portal started.")
    return 0


def _install_global_tmux_hooks() -> None:
    """Install global tmux hooks for portal sync.

    Installs hooks globally so the portal is notified of:
    - session-created: New session created
    - session-closed: Session destroyed
    - client-attached: Client attached to session (presence tracking)
    - client-detached: Client detached from session
    - after-split-window: New pane created
    - session-renamed: Session name changed
    - alert-activity: Activity in monitored window (requires monitor-activity on)
    """
    agentwire_path = _get_agentwire_path()

    # Check existing hooks
    result = subprocess.run(
        ["tmux", "show-hooks", "-g"],
        capture_output=True,
        text=True,
    )
    existing = result.stdout

    # Reinstall whenever the EXACT command isn't already set, so changes to the
    # hook string (e.g. a subcommand rename) propagate on portal restart instead
    # of leaving a stale hook that silently fails.
    def install_hook(hook_name: str, hook_cmd: str) -> None:
        if hook_cmd not in existing:
            subprocess.run(
                ["tmux", "set-hook", "-g", hook_name, hook_cmd],
                capture_output=True,
            )

    # Session lifecycle hooks
    # All hooks suppress output and exit 0 (|| true) to avoid tmux showing error messages
    install_hook(
        "session-created",
        f'run-shell -b "{agentwire_path} notify-event session_created -s #{{session_name}} >/dev/null 2>&1 || true"'
    )
    install_hook(
        "session-closed",
        f'run-shell -b "{agentwire_path} notify-event session_closed -s #{{hook_session_name}} >/dev/null 2>&1 || true"'
    )

    # Presence tracking hooks
    install_hook(
        "client-attached",
        f'run-shell -b "{agentwire_path} notify-event client_attached -s #{{session_name}} >/dev/null 2>&1 || true"'
    )
    install_hook(
        "client-detached",
        f'run-shell -b "{agentwire_path} notify-event client_detached -s #{{session_name}} >/dev/null 2>&1 || true"'
    )

    # Pane creation hook (global - catches all pane creations)
    install_hook(
        "after-split-window",
        f'run-shell -b "{agentwire_path} notify-event pane_created -s #{{session_name}} --pane-id #{{pane_id}} >/dev/null 2>&1 || true"'
    )

    # Session rename hook
    # Note: #{hook_session_name} has new name, we pass old name via #{@_old_session_name} if set
    install_hook(
        "session-renamed",
        f'run-shell -b "{agentwire_path} notify-event session_renamed -s #{{session_name}} >/dev/null 2>&1 || true"'
    )

    # Activity notification hook (fires when monitor-activity is enabled on a window)
    install_hook(
        "alert-activity",
        f'run-shell -b "{agentwire_path} notify-event window_activity -s #{{session_name}} >/dev/null 2>&1 || true"'
    )
