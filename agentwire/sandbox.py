"""Opt-in Claude Code sandbox for a session (#1279).

``agentwire new --sandbox`` launches the agent with ``claude --settings <json>``
turning on Claude Code's native Bash sandbox: writes limited to the session's
working directory plus an allowance list, network limited to an allowlist,
and the unsandboxed-retry escape hatch disabled. This only bounds what an *approved* command can touch.

The settings ride ``--settings`` (per-process, above user/project files, below
managed), so no settings file on disk is ever written. The allowance lists come
from ``sandbox:`` in ``config.yaml`` (:class:`agentwire.config.SandboxConfig`);
the spike report is ``docs/wiki/security/sandbox-spike.md``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path


def tmux_socket_path() -> str:
    """Path of the default tmux server socket for this user.

    Taken from ``$TMUX`` when inside tmux (its first field IS the socket), else
    the conventional ``<tmpdir>/tmux-<uid>/default``.
    """
    inside = os.environ.get("TMUX", "").split(",")[0]
    if inside:
        return inside
    return f"/private/tmp/tmux-{os.getuid()}/default" if sys.platform == "darwin" \
        else f"/tmp/tmux-{os.getuid()}/default"


def build_settings(
    *,
    allow_write: list[str],
    allowed_domains: list[str],
    allow_unix_sockets: list[str],
    excluded_commands: list[str],
    allow_local_binding: bool,
) -> dict:
    """The ``--settings`` document for a sandboxed session.

    Fixed (not configurable, because loosening them defeats the feature):
    ``enabled``, ``failIfUnavailable`` (a sandbox that can't start must refuse,
    not silently run unsandboxed), ``allowUnsandboxedCommands: false`` and
    ``network.strictAllowlist`` (a host off the list is refused, not prompted
    or — under bypass — silently allowed). The tmux socket is always allowed
    because ``agentwire`` itself cannot function without it.
    """
    sockets = list(dict.fromkeys([tmux_socket_path(), *allow_unix_sockets]))
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {"allowWrite": list(allow_write)},
            "network": {
                "allowedDomains": list(allowed_domains),
                "strictAllowlist": True,
                "allowUnixSockets": sockets,
                "allowLocalBinding": allow_local_binding,
            },
            "excludedCommands": list(excluded_commands),
        }
    }


def settings_for(config) -> dict:
    """Settings document from a loaded :class:`~agentwire.config.Config`."""
    cfg = config.sandbox
    return build_settings(
        allow_write=cfg.allow_write,
        allowed_domains=cfg.allowed_domains,
        allow_unix_sockets=cfg.allow_unix_sockets,
        excluded_commands=cfg.excluded_commands,
        allow_local_binding=cfg.allow_local_binding,
    )


def settings_flag(settings: dict) -> str:
    """The shell-quoted ``--settings '<json>'`` flag for a launch line."""
    return f"--settings {shlex.quote(json.dumps(settings, separators=(',', ':')))}"


def platform_problem() -> str | None:
    """Why the sandbox cannot start on this machine, or None if it can.

    macOS uses Seatbelt (``sandbox-exec``); Linux needs bubblewrap + socat.
    ``failIfUnavailable`` makes a missing dependency a hard launch failure, so
    doctor says so before a session is wasted on it.
    """
    if sys.platform == "darwin":
        return None if shutil.which("sandbox-exec") else "sandbox-exec not found (Seatbelt)"
    if sys.platform.startswith("linux"):
        missing = [t for t in ("bwrap", "socat") if not shutil.which(t)]
        return f"missing {', '.join(missing)} (apt/dnf install bubblewrap socat)" if missing else None
    return f"unsupported platform {sys.platform}"


def sandboxed_sessions() -> list[str]:
    """Names of recorded sessions launched with ``--sandbox``."""
    from .core import load_session_metadata, sessions_dir

    root = sessions_dir()
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in Path(root).iterdir()
        if p.is_dir() and load_session_metadata(p.name).get("sandbox")
    )
