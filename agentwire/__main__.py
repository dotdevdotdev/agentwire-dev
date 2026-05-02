"""CLI entry point for AgentWire."""

import argparse
import base64
from dataclasses import dataclass, field
import datetime
import importlib.resources
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# Load .env files (project first, then global config)
load_dotenv()  # .env in current directory
load_dotenv(Path.home() / ".agentwire" / ".env")  # Global config

from . import __version__, cli_safety, pane_manager
from .project_config import (
    ProjectConfig,
    SessionType,
    detect_default_agent_type,
    get_parent_from_config,
    get_voice_from_config,
    load_project_config,
    normalize_session_type,
    save_project_config,
)
from .roles import RoleConfig, load_roles, merge_roles
from .worktree import ensure_worktree, parse_session_name, remove_worktree

# Default config directory
CONFIG_DIR = Path.home() / ".agentwire"


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


def _check_config_exists() -> bool:
    """Check ~/.agentwire/config.yaml exists; print init hint if not."""
    config_path = CONFIG_DIR / "config.yaml"
    if not config_path.exists():
        print("Error: AgentWire is not configured.", file=sys.stderr)
        print(file=sys.stderr)
        print("Run 'agentwire init' to set up your configuration.", file=sys.stderr)
        return False
    return True


@dataclass
class AgentCommand:
    """Result of building an agent command."""
    command: str  # The shell command to execute
    temp_file: str | None = None  # Temp file to clean up after agent starts
    env: dict[str, str] = field(default_factory=dict)  # Secrets to inject via tmux set-environment (keeps keys out of `ps`)


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
    for key, value in env.items():
        flags.extend(["-e", f"{key}={value}"])
    return flags


def _build_tmux_env_flags_shell(env: dict[str, str]) -> str:
    """Shell-quoted `-e 'K=V' …` fragment for inlining via SSH. Trailing space when non-empty."""
    if not env:
        return ""
    parts = [f"-e {shlex.quote(f'{k}={v}')}" for k, v in env.items()]
    return " ".join(parts) + " "


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


def build_agent_command(session_type: str, roles: list[RoleConfig] | None = None, model: str | None = None) -> AgentCommand:
    """Build the shell command + injected env for the given session type."""
    if session_type == "bare":
        return AgentCommand(command="")

    merged = merge_roles(roles) if roles else None

    # === Pi coding agent (any provider) ===
    # Session type: pi-<provider>[-restricted|-readonly], e.g. pi-zai, pi-deepseek
    if session_type.startswith("pi-"):
        remainder = session_type[3:]
        if remainder.endswith("-restricted"):
            provider = remainder[:-11]
            variant = "restricted"
        elif remainder.endswith("-readonly"):
            provider = remainder[:-9]
            variant = "readonly"
        else:
            provider = remainder
            variant = None

        config = load_config()
        pi_config = config.get("pi", {})
        pi_binary = pi_config.get("binary", "pi")

        provider_cfg = pi_config.get("providers", {}).get(provider)
        if not provider_cfg:
            raise ValueError(
                f"No config for pi provider '{provider}'. "
                f"Add pi.providers.{provider} to ~/.agentwire/config.yaml "
                f"with at least env_var, api_key, and default_model."
            )
        env_var = provider_cfg.get("env_var", f"{provider.upper().replace('-', '_')}_API_KEY")
        api_key = provider_cfg.get("api_key", "")
        default_model = provider_cfg.get("default_model", "")

        # Merge provider key + any global pi extra_env (e.g. BRAVE_SEARCH_API_KEY)
        env: dict[str, str] = {}
        if api_key:
            env[env_var] = api_key
        env.update(pi_config.get("extra_env", {}))

        parts = [pi_binary, "--provider", provider]
        resolved_model = model or default_model
        if resolved_model:
            parts.extend(["--model", resolved_model])

        # Permission variants (pi has no permission system to bypass; variants
        # translate to pi's --tools whitelist instead)
        temp_file = None
        if variant == "restricted":
            parts.extend(["--tools", "read,grep,find,bash"])
        elif variant == "readonly":
            parts.extend(["--tools", "read,grep,find"])
        elif merged and merged.tools:
            # Translate Claude tool names to pi's lowercase tool names.
            # Pi only supports: read, bash, edit, write, grep, find, ls
            pi_valid = {"read", "bash", "edit", "write", "grep", "find", "ls"}
            pi_tools = [t.lower() for t in merged.tools if t.lower() in pi_valid]
            if pi_tools:
                parts.extend(["--tools", ",".join(pi_tools)])

        # System prompt: combine global pi.system_prompt + role instructions.
        # Skipped for restricted/readonly — those are curated contexts.
        if variant not in ("restricted", "readonly"):
            global_prompt = pi_config.get("system_prompt", "")
            role_prompt = merged.instructions if merged and merged.instructions else ""
            combined = "\n\n".join(filter(None, [global_prompt, role_prompt]))
            if combined:
                f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
                f.write(combined)
                f.close()
                temp_file = f.name
                parts.append(f'--append-system-prompt "$(<{temp_file})"')

        return AgentCommand(
            command=" ".join(parts),
            temp_file=temp_file,
            env=env,
        )

    # === Agentwire REPL (claude-agent-sdk) ===
    # sdk-bypass / sdk-prompted / sdk-restricted — Python REPL running in a
    # tmux pane, auth via Claude subscription. See docs/missions/agentwire-repl.md.
    if session_type.startswith("sdk-"):
        # Permission mode maps to SDK's permission_mode parameter
        mode_map = {
            "sdk-bypass": "bypass",
            "sdk-prompted": "prompted",
            "sdk-restricted": "restricted",
        }
        mode = mode_map.get(session_type, "bypass")

        parts = ["agentwire", "repl", "--mode", mode]
        if model:
            parts.extend(["--model", model])

        # Role-based system prompt (same temp-file pattern as claude-* / pi-*
        # to avoid shell escaping on multiline content; --append-system-prompt
        # must be last).
        temp_file = None
        if merged and merged.instructions:
            f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
            f.write(merged.instructions)
            f.close()
            temp_file = f.name
            parts.append(f'--append-system-prompt "$(<{temp_file})"')

        return AgentCommand(
            command=" ".join(parts),
            temp_file=temp_file,
        )

    # === Claude Code ===
    if session_type.startswith("claude"):
        parts = ["claude"]

        # Permission flags
        if session_type == "claude-bypass":
            parts.append("--dangerously-skip-permissions")
        elif session_type == "claude-auto":
            parts.extend(["--enable-auto-mode", "--permission-mode", "auto"])
            # Inject core allows that bypass the classifier entirely (zero token cost)
            core_allows = [
                "Bash(agentwire *)", "Bash(tmux *)", "Bash(git *)",
                "Bash(gh pr create*)", "Bash(gh pr view*)",
                "Read(*)", "Edit(*)", "Write(*)", "Glob(*)", "Grep(*)",
            ]
            parts.extend(["--allowedTools", shlex.quote(",".join(core_allows))])
        elif session_type == "claude-restricted":
            parts.append("--tools Bash")
        # claude-prompted has no special flags

        # Model override
        if model:
            parts.append(f"--model {model}")

        # Role-based flags (not for restricted mode)
        temp_file = None
        if merged and session_type != "claude-restricted":
            if merged.tools:
                parts.append(f"--tools {','.join(merged.tools)}")

            if merged.disallowed_tools:
                parts.append(f"--disallowedTools {','.join(merged.disallowed_tools)}")

            if merged.instructions:
                # Write to temp file to avoid shell escaping issues
                # See docs/wiki/internals/shell-escaping.md for details
                # MUST be last flag — multiline content can break subsequent args
                f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
                f.write(merged.instructions)
                f.close()
                temp_file = f.name
                parts.append(f'--append-system-prompt "$(<{temp_file})"')

        return AgentCommand(
            command=" ".join(parts),
            temp_file=temp_file,
        )

    # Unknown session type - return empty
    return AgentCommand(command="")


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
    """Detect Ubuntu 24.04+ EXTERNALLY-MANAGED marker; return False if user must act."""
    if not sys.platform.startswith('linux'):
        return True

    # Check for EXTERNALLY-MANAGED marker
    marker = Path(sys.prefix) / "EXTERNALLY-MANAGED"
    if marker.exists():
        print("⚠️  Externally-managed Python environment detected (Ubuntu 24.04+)")
        print()
        print("Ubuntu prevents pip from installing packages system-wide to avoid conflicts.")
        print()
        print("Recommended approach - Use venv:")
        print("  python3 -m venv ~/.agentwire-venv")
        print("  source ~/.agentwire-venv/bin/activate")
        print("  pip install agentwire-dev")
        print()
        print("  Add to ~/.bashrc for persistence:")
        print("  echo 'source ~/.agentwire-venv/bin/activate' >> ~/.bashrc")
        print()
        print("Alternative (not recommended):")
        print("  pip3 install --break-system-packages agentwire-dev")
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


def _get_session_project_path(session: str) -> Path | None:
    """Get a session's project path from tmux cwd, falling back to session name parsing."""
    if tmux_session_exists(session):
        result = subprocess.run(
            ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())

    # Fallback: derive from session name
    config = load_config()
    projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser()
    project, _, _ = parse_session_name(session)
    return projects_dir / project


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

    Reads dev.source_dir from config.yaml, defaults to ~/projects/agentwire-dev.
    """
    config = load_config()
    source_dir = config.get("dev", {}).get("source_dir", "~/projects/agentwire-dev")
    return Path(source_dir).expanduser()


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


# === Wave 2: Remote Infrastructure Helpers ===


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


def _portal_api(method: str, path: str, data: dict | None = None, timeout: int = 10) -> dict | None:
    """Make an API request to the portal using curl (reliable with self-signed certs).

    Returns parsed JSON dict on success, None on failure.
    """
    config = load_config()
    portal_url = config.get("portal", {}).get("url", "https://localhost:8765")
    url = f"{portal_url}{path}"

    cmd = ["curl", "-sk", "--max-time", str(timeout), "-o", "-", "-w", "\n%{http_code}"]
    if method == "POST":
        cmd.extend(["-X", "POST", "-H", "Content-Type: application/json"])
        if data is not None:
            cmd.extend(["-d", json.dumps(data)])
    elif method == "DELETE":
        cmd.extend(["-X", "DELETE"])
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        if result.returncode != 0:
            return None
        lines = result.stdout.rsplit("\n", 1)
        if len(lines) < 2:
            return None
        body, status_code = lines[0], lines[1].strip()
        if not status_code.startswith("2"):
            return None
        return json.loads(body) if body.strip() else {}
    except Exception:
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
            "https://localhost:8765/api/sessions/refresh",
            method="POST",
            data=b"",
        )
        urllib.request.urlopen(req, timeout=2, context=ctx)
    except Exception:
        # Portal may not be running - that's fine
        pass


# === Portal Commands ===


def _start_portal_local(args) -> int:
    """Start portal locally in tmux."""
    from .network import NetworkContext
    from .tunnels import TunnelManager

    session_name = get_portal_session_name()

    if tmux_session_exists(session_name):
        print(f"Portal already running in tmux session '{session_name}'")
        print("Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    # Ensure required tunnels are up before starting portal
    ctx = NetworkContext.from_config()
    required_tunnels = ctx.get_required_tunnels()

    if required_tunnels:
        print("Ensuring tunnels to remote services...")
        tm = TunnelManager()

        for spec in required_tunnels:
            status = tm.check_tunnel(spec)

            if status.status == "up":
                print(f"  [ok] {spec.remote_machine}:{spec.remote_port} (already up)")
            else:
                print(f"  [..] Creating tunnel to {spec.remote_machine}:{spec.remote_port}...", end=" ", flush=True)
                result = tm.create_tunnel(spec, ctx)

                if result.status == "up":
                    print("[ok]")
                else:
                    print("[!!]")
                    print(f"      Warning: Could not create tunnel: {result.error}")
                    print(f"      The portal may not be able to reach {spec.remote_machine}.")

        print()

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

    print("Portal started. Attaching... (Ctrl+B D to detach)")
    subprocess.run(["tmux", "attach-session", "-t", session_name])
    return 0


def _start_portal_remote(ssh_target: str, machine_id: str, args) -> int:
    """Start portal on remote machine via SSH."""
    session_name = get_portal_session_name()

    # Check if portal already running remotely
    check_cmd = f"tmux has-session -t ={session_name} 2>/dev/null && echo running || echo stopped"
    result = subprocess.run(
        ["ssh", ssh_target, check_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Cannot reach portal machine. Check: ssh {ssh_target} echo ok", file=sys.stderr)
        return 1

    if "running" in result.stdout:
        print(f"Portal already running on {machine_id} in tmux session '{session_name}'")
        return 0

    # Build remote command
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

    server_cmd = " ".join(cmd_parts)

    # Start remotely in tmux
    remote_cmd = f"tmux new-session -d -s {session_name} && tmux send-keys -t {session_name} {shlex.quote(server_cmd)} Enter"
    mode = "dev mode" if getattr(args, 'dev', False) else "installed"
    print(f"Starting AgentWire portal ({mode}) on {machine_id}...")

    result = subprocess.run(
        ["ssh", ssh_target, remote_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Failed to start portal on {machine_id}: {result.stderr}", file=sys.stderr)
        return 1

    print(f"Portal started on {machine_id}.")
    return 0


def _stop_portal_remote(ssh_target: str, machine_id: str) -> int:
    """Stop portal on remote machine via SSH."""
    session_name = get_portal_session_name()

    # Check if running
    check_cmd = f"tmux has-session -t ={session_name} 2>/dev/null && echo running || echo stopped"
    result = subprocess.run(
        ["ssh", ssh_target, check_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Cannot reach portal machine. Check: ssh {ssh_target} echo ok", file=sys.stderr)
        return 1

    if "stopped" in result.stdout:
        print(f"Portal is not running on {machine_id}.")
        return 1

    # Kill session
    kill_cmd = f"tmux kill-session -t {session_name}"
    result = subprocess.run(
        ["ssh", ssh_target, kill_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Failed to stop portal on {machine_id}: {result.stderr}", file=sys.stderr)
        return 1

    print(f"Portal stopped on {machine_id}.")
    return 0


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


def cmd_portal_start(args) -> int:
    """Start the AgentWire portal web server in tmux."""
    if not _check_config_exists():
        return 1
    if not _check_tmux_installed():
        return 1

    from .network import NetworkContext

    ctx = NetworkContext.from_config()

    if ctx.is_local("portal"):
        return _start_portal_local(args)

    # Portal runs on another machine
    ssh_target = ctx.get_ssh_target("portal")
    machine_id = ctx.get_machine_for_service("portal")

    if not ssh_target or not machine_id:
        print("Portal configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    print(f"Portal runs on {machine_id}, starting remotely...")
    return _start_portal_remote(ssh_target, machine_id, args)


def cmd_portal_serve(args) -> int:
    """Run the web server directly (foreground)."""
    from .server import main as server_main

    server_main(
        config_path=str(args.config) if args.config else None,
        port=args.port,
        host=args.host,
        no_tts=args.no_tts,
        no_stt=args.no_stt,
    )
    return 0


def cmd_portal_stop(args) -> int:
    """Stop the AgentWire portal."""
    from .network import NetworkContext

    ctx = NetworkContext.from_config()
    session_name = get_portal_session_name()

    if ctx.is_local("portal"):
        if not tmux_session_exists(session_name):
            print("Portal is not running.")
            return 1

        subprocess.run(["tmux", "kill-session", "-t", session_name])
        print("Portal stopped.")
        return 0

    # Portal runs on another machine
    ssh_target = ctx.get_ssh_target("portal")
    machine_id = ctx.get_machine_for_service("portal")

    if not ssh_target or not machine_id:
        print("Portal configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    print(f"Portal runs on {machine_id}, stopping remotely...")
    return _stop_portal_remote(ssh_target, machine_id)


def cmd_portal_status(args) -> int:
    """Check portal status."""
    from .network import NetworkContext

    json_mode = getattr(args, 'json', False)
    ctx = NetworkContext.from_config()
    session_name = get_portal_session_name()

    if ctx.is_local("portal"):
        url = ctx.get_service_url("portal", use_tunnel=False)
        if tmux_session_exists(session_name):
            healthy = _check_portal_health(url)
            if json_mode:
                _output_json({
                    "success": True,
                    "running": True,
                    "url": url,
                    "session": session_name,
                    "healthy": healthy,
                    "machine": None,
                })
            else:
                print(f"Portal is running in tmux session '{session_name}'")
                print(f"  Attach: tmux attach -t {session_name}")
                if healthy:
                    print(f"  Health: OK ({url})")
                else:
                    print("  Health: starting or not responding yet")
            return 0
        else:
            if json_mode:
                _output_json({
                    "success": True,
                    "running": False,
                    "url": url,
                    "session": session_name,
                    "healthy": False,
                    "machine": None,
                })
            else:
                print("Portal is not running.")
                print("  Start:  agentwire portal start")
            return 1

    # Portal runs on another machine - check via health endpoint
    machine_id = ctx.get_machine_for_service("portal")
    url = ctx.get_service_url("portal", use_tunnel=True)

    healthy = _check_portal_health(url)
    if healthy:
        if json_mode:
            _output_json({
                "success": True,
                "running": True,
                "url": url,
                "healthy": True,
                "machine": machine_id,
            })
        else:
            print(f"Portal runs on {machine_id}")
            print("  Status: running")
            print(f"  Health: OK ({url})")
        return 0
    else:
        # Try direct connection if tunnel might not exist
        direct_url = ctx.get_service_url("portal", use_tunnel=False)
        if direct_url != url and _check_portal_health(direct_url):
            if json_mode:
                _output_json({
                    "success": True,
                    "running": True,
                    "url": direct_url,
                    "healthy": True,
                    "machine": machine_id,
                    "tunnel_issue": True,
                })
            else:
                print(f"Portal runs on {machine_id}")
                print("  Status: running (tunnel not working, direct OK)")
                print(f"  Health: OK ({direct_url})")
                print("  Hint: Run 'agentwire tunnels check' to verify tunnels")
            return 0

        if json_mode:
            _output_json({
                "success": True,
                "running": False,
                "url": url,
                "healthy": False,
                "machine": machine_id,
            })
        else:
            print(f"Portal runs on {machine_id}")
            print("  Status: not reachable")
            print(f"  Checked: {url}")
            if direct_url != url:
                print(f"  Also checked: {direct_url}")
        return 1


def cmd_portal_restart(args) -> int:
    """Restart the AgentWire portal (stop + start)."""
    import time

    print("Stopping portal...")
    stop_result = cmd_portal_stop(args)

    if stop_result != 0:
        # Portal wasn't running, just start it
        print("Portal was not running, starting fresh...")

    # Brief pause to ensure clean shutdown
    time.sleep(0.5)

    print("Starting portal...")
    return cmd_portal_start(args)


# === TTS Commands ===


def _get_venv_for_backend(backend: str) -> str:
    """Get the venv family required for a backend."""
    if backend.startswith("chatterbox"):
        return "chatterbox"
    if backend.startswith("zonos"):
        return "zonos"
    if backend == "kokoro":
        return "kokoro"
    return "qwen"


def _start_tts_local(args, venv_override: str | None = None) -> int:
    """Start TTS server locally in tmux.

    Args:
        args: Parsed CLI arguments
        venv_override: Force specific venv (used for restart after venv_mismatch)
    """
    session_name = get_tts_session_name()

    if tmux_session_exists(session_name):
        print(f"TTS server already running in tmux session '{session_name}'")
        print("Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    # Get TTS config
    config = load_config()
    tts_config = config.get("tts", {})
    port = args.port or tts_config.get("port", 8100)
    host = args.host or tts_config.get("host", "0.0.0.0")
    backend = getattr(args, "backend", None) or tts_config.get("backend", "chatterbox")

    # Determine venv family
    venv = venv_override or _get_venv_for_backend(backend)

    # Find the source directory and appropriate venv
    source_dir = get_source_dir()
    if not source_dir:
        print("Error: Cannot find agentwire source directory.", file=sys.stderr)
        return 1

    # Map venv family to venv directory name
    venv_name = f".venv-{venv}" if venv != "default" else ".venv"
    venv_path = source_dir / venv_name / "bin" / "activate"

    if not venv_path.exists():
        print(f"Error: Venv not found: {venv_path}", file=sys.stderr)
        print(f"Create it with: cd {source_dir} && uv venv {venv_name}", file=sys.stderr)
        return 1

    # Build command using venv python directly (avoids broken activate scripts and conda interference)
    venv_python = source_dir / venv_name / "bin" / "python"
    tts_cmd = (
        f"cd {source_dir} && "
        f"{venv_python} -m agentwire tts serve --host {host} --port {port} --backend {backend} --venv {venv}"
    )

    print(f"Starting TTS server on {host}:{port} (backend: {backend}, venv: {venv})...")
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name,
    ])
    subprocess.run([
        "tmux", "send-keys", "-t", session_name, tts_cmd, "Enter",
    ])

    print("TTS server started. Attaching... (Ctrl+B D to detach)")
    subprocess.run(["tmux", "attach-session", "-t", session_name])
    return 0


def _start_tts_remote(ssh_target: str, machine_id: str, args) -> int:
    """Start TTS server on remote machine via SSH."""
    session_name = get_tts_session_name()

    # Check if TTS already running remotely
    check_cmd = f"tmux has-session -t ={session_name} 2>/dev/null && echo running || echo stopped"
    result = subprocess.run(
        ["ssh", ssh_target, check_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Cannot reach TTS machine. Check: ssh {ssh_target} echo ok", file=sys.stderr)
        return 1

    if "running" in result.stdout:
        print(f"TTS server already running on {machine_id} in tmux session '{session_name}'")
        return 0

    # Get port config
    config = load_config()
    tts_config = config.get("tts", {})
    port = args.port or tts_config.get("port", 8100)
    host = args.host or tts_config.get("host", "0.0.0.0")
    backend = getattr(args, "backend", None) or tts_config.get("backend", "chatterbox")

    # Build backend flag
    backend_flag = f" --backend {backend}" if backend != "chatterbox" else ""

    # Build remote command - on remote machine, use agentwire tts serve
    server_cmd = f"agentwire tts serve --host {host} --port {port}{backend_flag}"

    # Start remotely in tmux
    remote_cmd = f"tmux new-session -d -s {session_name} && tmux send-keys -t {session_name} {shlex.quote(server_cmd)} Enter"
    print(f"Starting TTS server on {machine_id}...")

    result = subprocess.run(
        ["ssh", ssh_target, remote_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Failed to start TTS on {machine_id}: {result.stderr}", file=sys.stderr)
        return 1

    print(f"TTS server started on {machine_id}.")
    return 0


def _stop_tts_remote(ssh_target: str, machine_id: str) -> int:
    """Stop TTS server on remote machine via SSH."""
    session_name = get_tts_session_name()

    # Check if running
    check_cmd = f"tmux has-session -t ={session_name} 2>/dev/null && echo running || echo stopped"
    result = subprocess.run(
        ["ssh", ssh_target, check_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Cannot reach TTS machine. Check: ssh {ssh_target} echo ok", file=sys.stderr)
        return 1

    if "stopped" in result.stdout:
        print(f"TTS server is not running on {machine_id}.")
        return 1

    # Kill session
    kill_cmd = f"tmux kill-session -t {session_name}"
    result = subprocess.run(
        ["ssh", ssh_target, kill_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Failed to stop TTS on {machine_id}: {result.stderr}", file=sys.stderr)
        return 1

    print(f"TTS server stopped on {machine_id}.")
    return 0


def _check_tts_health(url: str, timeout: int = 2) -> tuple[bool, list[str] | None]:
    """Check if TTS server is responding at URL.

    Returns:
        (is_healthy, voices_list or None)
    """

    try:
        req = urllib.request.urlopen(f"{url}/voices", timeout=timeout)
        voices = json.loads(req.read().decode())
        if isinstance(voices, list):
            return True, voices
        return True, None
    except Exception:
        return False, None


def cmd_tts_start(args) -> int:
    """Start the Chatterbox TTS server in tmux."""
    from .network import NetworkContext

    ctx = NetworkContext.from_config()

    if ctx.is_local("tts"):
        return _start_tts_local(args)

    # TTS runs on another machine
    ssh_target = ctx.get_ssh_target("tts")
    machine_id = ctx.get_machine_for_service("tts")

    if not ssh_target or not machine_id:
        print("TTS configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    print(f"TTS runs on {machine_id}, starting remotely...")
    return _start_tts_remote(ssh_target, machine_id, args)


def cmd_tts_serve(args) -> int:
    """Run the TTS server directly (foreground)."""
    import uvicorn

    config = load_config()
    tts_config = config.get("tts", {})
    port = args.port or tts_config.get("port", 8100)
    host = args.host or tts_config.get("host", "0.0.0.0")
    backend = getattr(args, "backend", None) or tts_config.get("backend", "chatterbox")

    # Determine venv family (explicit or auto-detect from backend)
    venv = getattr(args, "venv", None)
    if not venv:
        venv = _get_venv_for_backend(backend)

    # Set env vars for the TTS server module
    os.environ["DEFAULT_BACKEND"] = backend
    os.environ["CURRENT_VENV"] = venv

    print(f"Starting TTS server on {host}:{port} (backend: {backend}, venv: {venv})...")
    uvicorn.run(
        "agentwire.tts_server:app",
        host=host,
        port=port,
        log_level="info",
    )
    return 0


def cmd_tts_stop(args) -> int:
    """Stop the TTS server."""
    from .network import NetworkContext

    ctx = NetworkContext.from_config()
    session_name = get_tts_session_name()

    if ctx.is_local("tts"):
        if not tmux_session_exists(session_name):
            print("TTS server is not running.")
            return 1

        subprocess.run(["tmux", "kill-session", "-t", session_name])
        print("TTS server stopped.")
        return 0

    # TTS runs on another machine
    ssh_target = ctx.get_ssh_target("tts")
    machine_id = ctx.get_machine_for_service("tts")

    if not ssh_target or not machine_id:
        print("TTS configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    print(f"TTS runs on {machine_id}, stopping remotely...")
    return _stop_tts_remote(ssh_target, machine_id)


def cmd_tts_restart(args) -> int:
    """Restart TTS server with optional venv override.

    Used by CLI when venv_mismatch occurs during TTS request.
    Supports both local and remote TTS servers.
    """
    from .network import NetworkContext
    import time

    ctx = NetworkContext.from_config()
    session_name = get_tts_session_name()

    # Get overrides from args
    venv_override = getattr(args, "venv", None)
    backend = getattr(args, "backend", None)

    if ctx.is_local("tts"):
        # Stop if running
        if tmux_session_exists(session_name):
            print("Stopping TTS server...")
            subprocess.run(["tmux", "kill-session", "-t", session_name])
            time.sleep(1)

        # Start with new venv
        return _start_tts_local(args, venv_override=venv_override)

    # Remote TTS
    ssh_target = ctx.get_ssh_target("tts")
    machine_id = ctx.get_machine_for_service("tts")

    if not ssh_target or not machine_id:
        print("TTS configured for remote machine but machine not found.", file=sys.stderr)
        return 1

    # Determine backend and venv
    if not backend:
        config = load_config()
        backend = config.get("tts", {}).get("backend", "chatterbox")

    venv = venv_override or _get_venv_for_backend(backend)

    print(f"Restarting TTS on {machine_id} with backend '{backend}'...")
    success = _restart_tts_remote_for_venv(ssh_target, machine_id, venv, backend)
    return 0 if success else 1


def cmd_tts_status(args) -> int:
    """Check TTS server status."""
    from .network import NetworkContext

    json_mode = getattr(args, 'json', False)
    ctx = NetworkContext.from_config()
    session_name = get_tts_session_name()
    config = load_config()
    backend = config.get("tts", {}).get("backend", "unknown")

    if ctx.is_local("tts"):
        url = ctx.get_service_url("tts", use_tunnel=False)
        if tmux_session_exists(session_name):
            healthy, voices = _check_tts_health(url)
            if json_mode:
                _output_json({
                    "success": True,
                    "running": True,
                    "url": url,
                    "session": session_name,
                    "healthy": healthy,
                    "voices": voices or [],
                    "backend": backend,
                    "machine": None,
                })
            else:
                print(f"TTS server is running in tmux session '{session_name}'")
                print(f"  Attach: tmux attach -t {session_name}")
                if healthy:
                    if voices:
                        print(f"  Voices: {', '.join(voices)}")
                    else:
                        print(f"  Health: OK ({url})")
                else:
                    print("  Status: starting or not responding yet")
            return 0
        else:
            # No local tmux session, but check if TTS is reachable anyway
            healthy, voices = _check_tts_health(url)
            if healthy:
                if json_mode:
                    _output_json({
                        "success": True,
                        "running": True,
                        "url": url,
                        "healthy": True,
                        "voices": voices or [],
                        "backend": backend,
                        "machine": None,
                        "external": True,
                    })
                else:
                    print("TTS server is running (external/tunnel)")
                    if voices:
                        print(f"  Voices: {', '.join(voices)}")
                    print(f"  URL: {url}")
                return 0

            if json_mode:
                _output_json({
                    "success": True,
                    "running": False,
                    "url": url,
                    "healthy": False,
                    "backend": backend,
                    "machine": None,
                })
            else:
                print("TTS server is not running.")
                print("  Start:  agentwire tts start")
            return 1

    # TTS runs on another machine - check via health endpoint
    machine_id = ctx.get_machine_for_service("tts")
    url = ctx.get_service_url("tts", use_tunnel=True)

    healthy, voices = _check_tts_health(url)
    if healthy:
        if json_mode:
            _output_json({
                "success": True,
                "running": True,
                "url": url,
                "healthy": True,
                "voices": voices or [],
                "backend": backend,
                "machine": machine_id,
            })
        else:
            print(f"TTS server runs on {machine_id}")
            print("  Status: running")
            if voices:
                print(f"  Voices: {', '.join(voices)}")
            print(f"  URL: {url}")
        return 0
    else:
        # Try direct connection if tunnel might not exist
        direct_url = ctx.get_service_url("tts", use_tunnel=False)
        if direct_url != url:
            healthy, voices = _check_tts_health(direct_url)
            if healthy:
                if json_mode:
                    _output_json({
                        "success": True,
                        "running": True,
                        "url": direct_url,
                        "healthy": True,
                        "voices": voices or [],
                        "backend": backend,
                        "machine": machine_id,
                        "tunnel_issue": True,
                    })
                else:
                    print(f"TTS server runs on {machine_id}")
                    print("  Status: running (tunnel not working, direct OK)")
                    if voices:
                        print(f"  Voices: {', '.join(voices)}")
                    print(f"  URL: {direct_url}")
                    print("  Hint: Run 'agentwire tunnels check' to verify tunnels")
                return 0

        if json_mode:
            _output_json({
                "success": True,
                "running": False,
                "url": url,
                "healthy": False,
                "backend": backend,
                "machine": machine_id,
            })
        else:
            print(f"TTS server runs on {machine_id}")
            print("  Status: not reachable")
            print(f"  Checked: {url}")
            if direct_url != url:
                print(f"  Also checked: {direct_url}")
        return 1


# === STT Commands ===

def cmd_stt_start(args) -> int:
    """Start the STT server in tmux."""
    session_name = get_stt_session_name()

    if tmux_session_exists(session_name):
        print(f"STT server already running in tmux session '{session_name}'")
        print(f"  Attach: tmux attach -t {session_name}")
        return 0

    port = args.port or 8101
    host = args.host or "0.0.0.0"
    config = load_config()
    stt_config = config.get("stt", {})
    model = args.model or stt_config.get("model", "base")
    backend = getattr(args, 'backend', None) or stt_config.get("backend", "auto")
    moonshine_model = stt_config.get("moonshine_model", "moonshine/base")

    # Find agentwire source directory (for running from source venv)
    source_dir = get_source_dir()
    if not (source_dir / ".venv" / "bin" / "python").exists():
        print("Error: Cannot find agentwire source directory with .venv", file=sys.stderr)
        print(f"Configure dev.source_dir in ~/.agentwire/config.yaml (current: {source_dir})", file=sys.stderr)
        return 1
    agentwire_dir = source_dir

    # Build command using source venv
    python_path = agentwire_dir / ".venv" / "bin" / "python"
    cmd = f"cd {agentwire_dir} && WHISPER_MODEL={model} WHISPER_DEVICE=cpu STT_PORT={port} STT_HOST={host} STT_BACKEND={backend} MOONSHINE_MODEL={moonshine_model} {python_path} -m agentwire.stt.stt_server"

    # Create tmux session
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name, "-c", str(agentwire_dir)
    ], check=True)

    subprocess.run([
        "tmux", "send-keys", "-t", session_name, cmd, "Enter"
    ], check=True)

    print(f"STT server starting in tmux session '{session_name}'")
    print(f"  Model: {model}")
    print(f"  Port: {port}")
    print(f"  Attach: tmux attach -t {session_name}")
    return 0


def cmd_stt_serve(args) -> int:
    """Run the STT server directly (foreground)."""
    import uvicorn

    port = args.port or 8101
    host = args.host or "0.0.0.0"
    config = load_config()
    stt_config = config.get("stt", {})
    model = args.model or stt_config.get("model", "base")
    backend = getattr(args, 'backend', None) or stt_config.get("backend", "auto")
    moonshine_model = stt_config.get("moonshine_model", "moonshine/base")

    os.environ["WHISPER_MODEL"] = model
    os.environ["WHISPER_DEVICE"] = "cpu"
    os.environ["STT_BACKEND"] = backend
    os.environ["MOONSHINE_MODEL"] = moonshine_model

    print(f"Starting STT server on {host}:{port} with model {model}...")
    uvicorn.run(
        "agentwire.stt.stt_server:app",
        host=host,
        port=port,
        log_level="info",
    )
    return 0


def cmd_stt_stop(args) -> int:
    """Stop the STT server."""
    session_name = get_stt_session_name()

    if not tmux_session_exists(session_name):
        print("STT server is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", session_name])
    print("STT server stopped.")
    return 0


def cmd_stt_status(args) -> int:
    """Check STT server status."""
    json_mode = getattr(args, 'json', False)
    session_name = get_stt_session_name()
    config = load_config()
    stt_url = config.get("stt", {}).get("url", "http://localhost:8101")

    # Check health endpoint
    try:
        req = urllib.request.Request(f"{stt_url}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            if json_mode:
                _output_json({
                    "success": True,
                    "running": True,
                    "url": stt_url,
                    "healthy": True,
                    "model": data.get('model', 'unknown'),
                    "device": data.get('device', 'unknown'),
                    "session": session_name if tmux_session_exists(session_name) else None,
                })
            else:
                print("STT server is running")
                print(f"  Model: {data.get('model', 'unknown')}")
                print(f"  Device: {data.get('device', 'unknown')}")
                print(f"  URL: {stt_url}")
                if tmux_session_exists(session_name):
                    print(f"  Attach: tmux attach -t {session_name}")
            return 0
    except Exception:
        pass

    if tmux_session_exists(session_name):
        if json_mode:
            _output_json({
                "success": True,
                "running": True,
                "url": stt_url,
                "healthy": False,
                "session": session_name,
                "starting": True,
            })
        else:
            print(f"STT server is starting in tmux session '{session_name}'")
            print(f"  Attach: tmux attach -t {session_name}")
        return 0

    if json_mode:
        _output_json({
            "success": True,
            "running": False,
            "url": stt_url,
            "healthy": False,
        })
    else:
        print("STT server is not running.")
        print("  Start: agentwire stt start")
    return 1


# === Telegram Commands ===


def cmd_channels_list(args) -> int:
    """List all registered communication channels."""
    from agentwire.channels import ChannelRegistry
    from agentwire.config import get_config

    config = get_config()
    channels = []
    for name, cls in sorted(ChannelRegistry._channels.items()):
        ch_config = config.channels.get(name)
        # Check if channel has meaningful config (api key, token, or url)
        configured = False
        if ch_config is not None:
            for attr in ("api_key", "bot_token", "account_sid", "url"):
                if getattr(ch_config, attr, ""):
                    configured = True
                    break
        # Built-in channels live under agentwire.channels.*; anything else is external.
        builtin = cls.__module__.startswith("agentwire.channels.")
        channels.append({
            "name": name,
            "type": cls.channel_type,
            "configured": configured,
            "builtin": builtin,
        })

    json_mode = getattr(args, "json", False)
    if json_mode:
        _output_json({"success": True, "channels": channels})
    else:
        if not channels:
            print("No channels registered.")
        else:
            for ch in channels:
                status = "configured" if ch["configured"] else "not configured"
                builtin = "" if ch["builtin"] else " (custom)"
                print(f"  {ch['name']:12s} {ch['type']:10s} {status}{builtin}")
    return 0


def get_telegram_session_name() -> str:
    """Get Telegram bridge tmux session name from config."""
    config = load_config()
    return config.get("services", {}).get("telegram", {}).get("session_name", "agentwire-telegram")


def _bridge_tmux_cmd(source_dir: Path, python_invocation: str) -> str:
    """Build a tmux command that sources ~/.agentwire/.env (if present) under bash,
    then execs the bridge. Wrapped in bash -c so it works regardless of the user's
    default shell (zsh can choke on .env values containing special chars)."""
    env_file = Path.home() / ".agentwire" / ".env"
    parts = [f"cd {source_dir}"]
    if env_file.exists():
        parts.append(f"set -a; source {env_file}; set +a")
    parts.append(f"exec {python_invocation}")
    return "bash -c '" + "; ".join(parts) + "'"


def cmd_telegram_start(args) -> int:
    """Start the Telegram bridge in tmux."""
    session_name = get_telegram_session_name()

    if tmux_session_exists(session_name):
        print(f"Telegram bridge already running in tmux session '{session_name}'")
        print("Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    # Find agentwire source directory
    source_dir = get_source_dir()
    venv_python = source_dir / ".venv" / "bin" / "python"
    if not venv_python.exists():
        print(f"Error: Cannot find venv at {venv_python}", file=sys.stderr)
        return 1

    cmd = _bridge_tmux_cmd(source_dir, f"{venv_python} -m agentwire.bridges.telegram")

    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-c", str(source_dir)])
    subprocess.run(["tmux", "send-keys", "-t", session_name, cmd, "Enter"])

    print(f"Telegram bridge starting in tmux session '{session_name}'")
    print(f"  Attach: tmux attach -t {session_name}")
    return 0


def cmd_telegram_serve(args) -> int:
    """Run the Telegram bridge directly (foreground)."""
    from .bridges.telegram import run_bridge
    run_bridge()
    return 0


def cmd_telegram_stop(args) -> int:
    """Stop the Telegram bridge."""
    session_name = get_telegram_session_name()

    if not tmux_session_exists(session_name):
        print("Telegram bridge is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", session_name])
    print("Telegram bridge stopped.")
    return 0


def cmd_telegram_status(args) -> int:
    """Check Telegram bridge status."""
    json_mode = getattr(args, 'json', False)
    session_name = get_telegram_session_name()

    running = tmux_session_exists(session_name)

    if json_mode:
        _output_json({
            "success": True,
            "running": running,
            "session": session_name if running else None,
        })
    else:
        if running:
            print(f"Telegram bridge is running in tmux session '{session_name}'")
            print(f"  Attach: tmux attach -t {session_name}")
        else:
            print("Telegram bridge is not running.")
            print("  Start: agentwire telegram start")

    return 0 if running else 1


def cmd_telegram_notify(args) -> int:
    """Send a Telegram notification."""
    from .channels.telegram import TelegramConfigError, send_telegram

    body = args.body
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read()

    if not body:
        print("Error: No message body. Use --body or pipe content.", file=sys.stderr)
        return 1

    chat_id = getattr(args, "chat_id", None)

    try:
        result = send_telegram(text=body, chat_id=chat_id)
        if result.success:
            if not getattr(args, "quiet", False):
                print(f"Telegram message sent (id: {result.message_id})")
            return 0
        else:
            print(f"Error: {result.error}", file=sys.stderr)
            return 1
    except TelegramConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1


# === Discord Commands ===

def get_discord_session_name() -> str:
    """Get Discord bridge tmux session name from config."""
    config = load_config()
    return config.get("channels", {}).get("discord", {}).get("session_name", "agentwire-discord")


def cmd_discord_start(args) -> int:
    """Start the Discord bridge in tmux."""
    session_name = get_discord_session_name()

    if tmux_session_exists(session_name):
        print(f"Discord bridge already running in tmux session '{session_name}'")
        print("Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    source_dir = get_source_dir()
    venv_python = source_dir / ".venv" / "bin" / "python"
    if not venv_python.exists():
        print(f"Error: Cannot find venv at {venv_python}", file=sys.stderr)
        return 1

    cmd = _bridge_tmux_cmd(source_dir, f'{venv_python} -c "from agentwire.channels.discord import run_bridge; run_bridge()"')

    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-c", str(source_dir)])
    subprocess.run(["tmux", "send-keys", "-t", session_name, cmd, "Enter"])

    print(f"Discord bridge starting in tmux session '{session_name}'")
    print(f"  Attach: tmux attach -t {session_name}")
    return 0


def cmd_discord_serve(args) -> int:
    """Run the Discord bridge directly (foreground)."""
    from .channels.discord import run_bridge
    run_bridge()
    return 0


def cmd_discord_stop(args) -> int:
    """Stop the Discord bridge."""
    session_name = get_discord_session_name()

    if not tmux_session_exists(session_name):
        print("Discord bridge is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", session_name])
    print("Discord bridge stopped.")
    return 0


def cmd_discord_status(args) -> int:
    """Check Discord bridge status."""
    json_mode = getattr(args, 'json', False)
    session_name = get_discord_session_name()

    running = tmux_session_exists(session_name)

    if json_mode:
        _output_json({
            "success": True,
            "running": running,
            "session": session_name if running else None,
        })
    else:
        if running:
            print(f"Discord bridge is running in tmux session '{session_name}'")
            print(f"  Attach: tmux attach -t {session_name}")
        else:
            print("Discord bridge is not running.")
            print("  Start: agentwire discord start")

    return 0 if running else 1


# === Slack Commands ===

def get_slack_session_name() -> str:
    """Get Slack bridge tmux session name from config."""
    config = load_config()
    return config.get("channels", {}).get("slack", {}).get("session_name", "agentwire-slack")


def cmd_slack_start(args) -> int:
    """Start the Slack bridge in tmux."""
    session_name = get_slack_session_name()

    if tmux_session_exists(session_name):
        print(f"Slack bridge already running in tmux session '{session_name}'")
        print("Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    source_dir = get_source_dir()
    venv_python = source_dir / ".venv" / "bin" / "python"
    if not venv_python.exists():
        print(f"Error: Cannot find venv at {venv_python}", file=sys.stderr)
        return 1

    cmd = _bridge_tmux_cmd(source_dir, f'{venv_python} -c "from agentwire.channels.slack import run_bridge; run_bridge()"')

    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-c", str(source_dir)])
    subprocess.run(["tmux", "send-keys", "-t", session_name, cmd, "Enter"])

    print(f"Slack bridge starting in tmux session '{session_name}'")
    print(f"  Attach: tmux attach -t {session_name}")
    return 0


def cmd_slack_serve(args) -> int:
    """Run the Slack bridge directly (foreground)."""
    from .channels.slack import run_bridge
    run_bridge()
    return 0


def cmd_slack_stop(args) -> int:
    """Stop the Slack bridge."""
    session_name = get_slack_session_name()

    if not tmux_session_exists(session_name):
        print("Slack bridge is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", session_name])
    print("Slack bridge stopped.")
    return 0


def cmd_slack_status(args) -> int:
    """Check Slack bridge status."""
    json_mode = getattr(args, 'json', False)
    session_name = get_slack_session_name()

    running = tmux_session_exists(session_name)

    if json_mode:
        _output_json({
            "success": True,
            "running": running,
            "session": session_name if running else None,
        })
    else:
        if running:
            print(f"Slack bridge is running in tmux session '{session_name}'")
            print(f"  Attach: tmux attach -t {session_name}")
        else:
            print("Slack bridge is not running.")
            print("  Start: agentwire slack start")

    return 0 if running else 1


# === Say Command ===

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
        # Portal runs locally
        return f"https://localhost:{ctx.config.services.portal.port}"

    # Portal is remote - check if tunnel exists by testing localhost first
    tunnel_url = ctx.get_service_url("portal", use_tunnel=True)
    direct_url = ctx.get_service_url("portal", use_tunnel=False)

    # Try tunnel first (more common setup)
    if _check_portal_health(tunnel_url.replace("http://", "https://")):
        return tunnel_url.replace("http://", "https://")

    # Fall back to direct connection
    return direct_url.replace("http://", "https://")


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

    # Helper to install hook if not present
    def install_hook(hook_name: str, hook_cmd: str) -> None:
        if hook_name not in existing or agentwire_path not in existing:
            subprocess.run(
                ["tmux", "set-hook", "-g", hook_name, hook_cmd],
                capture_output=True,
            )

    # Session lifecycle hooks
    # All hooks suppress output and exit 0 (|| true) to avoid tmux showing error messages
    install_hook(
        "session-created",
        f'run-shell -b "{agentwire_path} notify session_created -s #{{session_name}} >/dev/null 2>&1 || true"'
    )
    install_hook(
        "session-closed",
        f'run-shell -b "{agentwire_path} notify session_closed -s #{{hook_session_name}} >/dev/null 2>&1 || true"'
    )

    # Presence tracking hooks
    install_hook(
        "client-attached",
        f'run-shell -b "{agentwire_path} notify client_attached -s #{{session_name}} >/dev/null 2>&1 || true"'
    )
    install_hook(
        "client-detached",
        f'run-shell -b "{agentwire_path} notify client_detached -s #{{session_name}} >/dev/null 2>&1 || true"'
    )

    # Pane creation hook (global - catches all pane creations)
    install_hook(
        "after-split-window",
        f'run-shell -b "{agentwire_path} notify pane_created -s #{{session_name}} --pane-id #{{pane_id}} >/dev/null 2>&1 || true"'
    )

    # Session rename hook
    # Note: #{hook_session_name} has new name, we pass old name via #{@_old_session_name} if set
    install_hook(
        "session-renamed",
        f'run-shell -b "{agentwire_path} notify session_renamed -s #{{session_name}} >/dev/null 2>&1 || true"'
    )

    # Activity notification hook (fires when monitor-activity is enabled on a window)
    install_hook(
        "alert-activity",
        f'run-shell -b "{agentwire_path} notify window_activity -s #{{session_name}} >/dev/null 2>&1 || true"'
    )


def _install_pane_hooks(session_name: str, pane_index: int) -> None:
    """Install tmux hooks to notify portal of pane state changes.

    Installs:
    - after-kill-pane: Fires when a pane is killed
    - pane-focus-in: Fires when pane focus changes (for multi-pane sessions)

    Uses run-shell -b for background execution to not block tmux.
    """
    agentwire_path = _get_agentwire_path()

    # Check existing hooks
    result = subprocess.run(
        ["tmux", "show-hooks", "-t", session_name],
        capture_output=True,
        text=True,
    )
    existing = result.stdout

    # Install after-kill-pane hook on the session
    # Note: #{hook_pane} may be empty when pane is already dead, so we just notify
    # without pane-id and let the portal refresh its pane list
    # Use || true to suppress error display in tmux
    if "after-kill-pane" not in existing:
        hook_cmd = f'run-shell -b "{agentwire_path} notify pane_died -s {session_name} >/dev/null 2>&1 || true"'
        subprocess.run(
            ["tmux", "set-hook", "-t", session_name, "after-kill-pane", hook_cmd],
            capture_output=True,
        )

    # Install pane-focus-in hook for active pane tracking
    # This fires when a pane gains focus within the session
    # Use || true to suppress error display in tmux
    if "pane-focus-in" not in existing:
        hook_cmd = f'run-shell -b "{agentwire_path} notify pane_focused -s {session_name} --pane-id #{{pane_id}} >/dev/null 2>&1 || true"'
        subprocess.run(
            ["tmux", "set-hook", "-t", session_name, "pane-focus-in", hook_cmd],
            capture_output=True,
        )


def _get_current_tmux_session() -> str | None:
    """Get the current tmux session name, if running inside tmux."""
    # Check if we're in tmux
    if not os.environ.get("TMUX"):
        return None

    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass

    return None


def _get_session_config_from_path(path: str) -> dict:
    """Read session config from .agentwire.yml in the given path.

    Returns:
        Dict with 'type' and 'roles' keys (values may be None/empty).
    """
    import yaml

    if not path:
        return {"type": None, "roles": []}

    yml_path = Path(path) / ".agentwire.yml"
    if yml_path.exists():
        try:
            with open(yml_path) as f:
                config = yaml.safe_load(f) or {}
                return {
                    "type": config.get("type"),
                    "roles": config.get("roles", []) or [],
                }
        except Exception:
            pass
    return {"type": None, "roles": []}


def _get_session_type_from_path(path: str) -> str | None:
    """Read session type from .agentwire.yml in the given path."""
    return _get_session_config_from_path(path).get("type")


def _get_remote_session_type(machine_id: str, path: str) -> str | None:
    """Read session type from .agentwire.yml on a remote machine.

    Returns:
        Session type (e.g., 'bare', 'claude-bypass') or None
    """
    import yaml

    if not path or not machine_id:
        return None

    cmd = f"cat {path}/.agentwire.yml 2>/dev/null || echo ''"
    result = _run_remote(machine_id, cmd)

    if result.returncode == 0 and result.stdout.strip():
        try:
            config = yaml.safe_load(result.stdout) or {}
            return config.get("type")
        except Exception:
            pass
    return None


def _infer_session_from_path() -> str | None:
    """Infer session name from current working directory.

    ~/projects/myapp -> myapp
    ~/projects/myapp-worktrees/feature -> myapp/feature
    """
    cwd = Path.cwd()
    projects_dir = Path.home() / "projects"

    try:
        rel = cwd.relative_to(projects_dir)
        parts = rel.parts

        if len(parts) == 1:
            return parts[0]
        elif len(parts) >= 2 and "-worktrees" in parts[0]:
            # myapp-worktrees/feature -> myapp/feature
            base = parts[0].replace("-worktrees", "")
            return f"{base}/{parts[1]}"
        elif len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    except ValueError:
        pass

    return None


def _check_portal_connections(session: str, portal_url: str) -> tuple[bool, str]:
    """Check if portal has active browser connections for a session.

    Tries session name variants: as-is, with hostname.

    Returns:
        Tuple of (has_connections, actual_session_name)
        - has_connections: True if there are connections (audio should go to portal)
        - actual_session_name: The session name that has connections (may include @machine)
    """
    import ssl

    # Try session variants: as-is, with hostname, with @local
    session_variants = [session]
    if "@" not in session:
        hostname = socket.gethostname().split('.')[0]
        session_variants.append(f"{session}@{hostname}")
        session_variants.append(f"{session}@local")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for session_name in session_variants:
        try:
            req = urllib.request.Request(
                f"{portal_url}/api/sessions/{session_name}/connections",
                headers={"Accept": "application/json"},
            )

            with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
                result = json.loads(response.read().decode())
                if result.get("has_connections", False):
                    return True, session_name

        except Exception:
            continue

    # No connections found in any variant
    return False, session


def _local_say_runpod(
    text: str,
    voice: str,
    exaggeration: float,
    cfg_weight: float,
    tts_config: dict,
    backend: str | None = None,
    instruct: str | None = None,
    language: str = "English",
    stream: bool = False,
) -> int:
    """Generate TTS via RunPod API or local TTS server.

    Works with runpod backend - calls the API directly.
    Falls back to HTTP TTS server for other backends.
    """
    config_backend = tts_config.get("backend", "none")

    if config_backend == "runpod":
        return _local_say_runpod_api(text, voice, exaggeration, cfg_weight, tts_config)
    else:
        # All HTTP-server-backed backends (kokoro, chatterbox, qwen-*, zonos-*, etc.)
        from .network import NetworkContext
        ctx = NetworkContext.from_config()
        tts_url = ctx.get_service_url("tts", use_tunnel=True)
        return _local_say(
            text, voice, exaggeration, cfg_weight, tts_url,
            backend=backend, instruct=instruct, language=language, stream=stream
        )


def _local_say_runpod_api(
    text: str,
    voice: str,
    exaggeration: float,
    cfg_weight: float,
    tts_config: dict,
) -> int:
    """Generate TTS via RunPod serverless API and play locally."""

    endpoint_id = tts_config.get("runpod_endpoint_id", "")
    api_key = tts_config.get("runpod_api_key", "")
    timeout = tts_config.get("runpod_timeout", 120)

    if not endpoint_id or not api_key:
        print("RunPod backend requires runpod_endpoint_id and runpod_api_key in config", file=sys.stderr)
        return 1

    endpoint_url = f"https://api.runpod.ai/v2/{endpoint_id}/runsync"

    payload = {
        "input": {
            "action": "generate",
            "text": text,
            "voice": voice,
            "exaggeration": exaggeration,
            "cfg_weight": cfg_weight,
        }
    }

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            endpoint_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode())

            # Check RunPod status
            if result.get("status") == "error":
                print(f"RunPod error: {result.get('error', 'Unknown error')}", file=sys.stderr)
                return 1

            # Extract output
            output = result.get("output", {})
            if "error" in output:
                print(f"TTS error: {output['error']}", file=sys.stderr)
                return 1

            # Decode base64 audio
            audio_b64 = output.get("audio", "")
            if not audio_b64:
                print("No audio returned from TTS", file=sys.stderr)
                return 1

            audio_data = base64.b64decode(audio_b64)

        # Save to temp file and play
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name

        # Play audio (cross-platform)
        if sys.platform == "darwin":
            subprocess.run(["afplay", temp_path], check=True)
        elif sys.platform == "linux":
            # Try various players
            for player in ["aplay", "paplay", "play"]:
                try:
                    subprocess.run([player, temp_path], check=True)
                    break
                except FileNotFoundError:
                    continue
        else:
            print(f"Audio saved to: {temp_path}")
            return 0

        # Clean up
        Path(temp_path).unlink(missing_ok=True)
        return 0

    except urllib.error.URLError as e:
        print(f"RunPod API not reachable: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"TTS failed: {e}", file=sys.stderr)
        return 1


def cmd_say(args) -> int:
    """Generate TTS audio and play it.

    Smart routing:
    1. Determine session (--session flag, .agentwire.yml, path inference, or tmux)
    2. Check if portal has browser connections for that session
    3. If connections exist → send to portal (plays on browser/tablet)
    4. If no connections → generate locally and play via system audio

    Voice notification:
    - If in a worker pane (pane > 0), auto-notifies pane 0 (orchestrator)
    - Use --notify SESSION to also notify a parent session
    - Use --no-auto-notify to disable worker->orchestrator notification
    """
    text = " ".join(args.text) if args.text else ""

    if not text:
        print("Usage: agentwire say <text>", file=sys.stderr)
        return 1

    config = load_config()
    tts_config = config.get("tts", {})
    # Voice priority: CLI flag > .agentwire.yml > global config default
    voice = args.voice or get_voice_from_config() or tts_config.get("default_voice", "default")
    exaggeration = args.exaggeration if args.exaggeration is not None else tts_config.get("exaggeration", 0.5)
    cfg_weight = args.cfg if args.cfg is not None else tts_config.get("cfg_weight", 0.5)

    # New parameters for modular TTS
    backend = getattr(args, 'backend', None)
    instruct = getattr(args, 'instruct', None)
    language = getattr(args, 'language', "English")
    stream = getattr(args, 'stream', False)

    # Determine session name (priority: flag > tmux session > path inference)
    # Tmux session is more accurate than path for forked/named sessions like "anna-fork-1"
    session = args.session or _get_current_tmux_session() or _infer_session_from_path()

    # Handle voice notifications
    _handle_voice_notifications(text, voice, args, session)

    # Try portal first if we have a session
    # Portal handles chunking internally (sequential generation + broadcast)
    if session:
        portal_url = _get_portal_url()
        has_connections, actual_session = _check_portal_connections(session, portal_url)

        if has_connections:
            return _remote_say(text, actual_session, portal_url)

    # No portal connections — chunk locally for better TTS quality
    from .utils.chunker import chunk_text
    chunks = chunk_text(text)

    for chunk in chunks:
        result = _local_say_runpod(
            chunk, voice, exaggeration, cfg_weight, tts_config,
            backend=backend, instruct=instruct, language=language, stream=stream
        )
        if result != 0:
            return result

    return 0


def _portal_broadcast(text: str, session: str, source: str) -> bool:
    """Broadcast a message via portal WebSocket. Returns True if sent."""
    try:
        import ssl
        import urllib.request
        portal_url = _get_portal_url()
        data = json.dumps({
            "type": "alert",
            "text": text,
            "session": session,
            "source": source,
        }).encode()
        req = urllib.request.Request(
            f"{portal_url}/api/session/{session}/broadcast",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(req, timeout=3, context=ssl_ctx)
        return True
    except Exception:
        return False


def cmd_reply(args) -> int:
    """Reply to the channel user (Discord, Slack, Telegram) who messaged this session.

    Broadcasts via portal WebSocket — channel bridges pick it up and deliver
    the message back to the originating platform (Discord channel, Slack DM, etc.).

    This is for channel sessions only. For worker→parent communication, use notify.

    Examples:
        agentwire reply "Here's what I found..."
        agentwire reply "The weather tomorrow is sunny and 22°C"
    """
    text = " ".join(args.text) if args.text else ""

    if not text:
        print("Usage: agentwire reply <message>", file=sys.stderr)
        return 1

    current_session = pane_manager.get_current_session()
    if not current_session:
        print("Cannot determine current session", file=sys.stderr)
        return 1

    if _portal_broadcast(text, current_session, current_session):
        if not getattr(args, 'quiet', False):
            print(f"Reply sent.")
        return 0
    else:
        print("Failed to send reply (portal not reachable?)", file=sys.stderr)
        return 1


def cmd_notify_parent(args) -> int:
    """Notify parent session (worker→orchestrator communication).

    Sends a prefixed text message to the parent session via tmux.
    The parent is determined from .agentwire.yml or --to flag.

    This is for session hierarchy communication. For replying to channel
    users (Discord, Slack, etc.), use reply.

    Notification targets (in priority order):
    1. --to SESSION if specified
    2. parent from .agentwire.yml if exists
    3. pane 0 of current session (if in worker pane)

    Examples:
        agentwire notify "Worker 1 completed task"
        agentwire notify --to agentwire "Build finished"
    """
    text = " ".join(args.text) if args.text else ""

    if not text:
        print("Usage: agentwire notify <message>", file=sys.stderr)
        return 1

    target_session = getattr(args, 'to', None)
    current_session = pane_manager.get_current_session()
    current_pane = pane_manager.get_current_pane_index()

    # If no explicit target, try parent from config
    if not target_session:
        parent = get_parent_from_config()
        if parent:
            target_session = parent

    # Build notification message
    source = current_session or "unknown"
    if current_pane is not None and current_pane > 0:
        notification = f"[NOTIFY from {source} pane {current_pane}] {text}"
    else:
        notification = f"[NOTIFY from {source}] {text}"

    try:
        if target_session:
            if target_session == current_session and current_pane == 0:
                print("Cannot notify own pane", file=sys.stderr)
                return 1
            pane_manager.send_to_pane(target_session, 0, notification)
            if not getattr(args, 'quiet', False):
                print(f"Notified {target_session}")
        elif current_pane is not None and current_pane > 0 and current_session:
            pane_manager.send_to_pane(current_session, 0, notification)
            if not getattr(args, 'quiet', False):
                print(f"Notified {current_session} pane 0")
        else:
            print("No target session (set 'parent' in .agentwire.yml or use --to)", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"Failed to send notification: {e}", file=sys.stderr)
        return 1

    return 0




def cmd_open(args) -> int:
    """Open a URL or local file as an artifact window in the portal.

    Examples:
        agentwire open dashboard.html --title "Dashboard"
        agentwire open https://example.com --title "External"
        agentwire open test.html --artifact-id my-test --json
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    url = args.url
    title = args.title
    artifact_id = getattr(args, 'artifact_id', None)
    json_output = getattr(args, 'json', False)

    portal_url = _get_portal_url()

    body = {
        "type": "artifact",
        "url": url,
        "title": title,
    }
    if artifact_id:
        body["artifact_id"] = artifact_id

    try:
        resp = requests.post(
            f"{portal_url}/api/desktop/window/open",
            json=body,
            verify=False,
            timeout=10,
        )
        data = resp.json()

        if json_output:
            print(json.dumps(data))
        elif data.get("success"):
            print(f"Opened artifact window: {title} (id: {data.get('window_id', 'unknown')})")
        else:
            print(f"Failed: {data.get('error', 'Unknown error')}", file=sys.stderr)
            return 1

    except requests.exceptions.ConnectionError:
        msg = "Portal not reachable. Is it running? (agentwire portal status)"
        if json_output:
            print(json.dumps({"success": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1
    except Exception as e:
        if json_output:
            print(json.dumps({"success": False, "error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _handle_voice_notifications(text: str, voice: str, args, session: str | None) -> None:
    """Handle voice notification to parent orchestrators.

    Auto-notify rules:
    - If in worker pane (pane > 0), notify pane 0 (local orchestrator)
    - If --notify SESSION specified, notify that session

    Args:
        text: The spoken text
        voice: Voice being used
        args: Command args (for --notify and --no-auto-notify flags)
        session: Current session name
    """
    notify_session = getattr(args, 'notify', None)
    no_auto_notify = getattr(args, 'no_auto_notify', False)

    # Get current pane index
    current_pane = pane_manager.get_current_pane_index()
    current_session = pane_manager.get_current_session()

    # Auto-notify pane 0 if we're in a worker pane (pane > 0)
    if not no_auto_notify and current_pane is not None and current_pane > 0 and current_session:
        notification = f"[VOICE] {voice} (pane {current_pane}): \"{text}\""
        try:
            pane_manager.send_to_pane(current_session, 0, notification)
        except Exception:
            pass  # Don't fail the say command if notification fails

    # Explicit --notify to another session
    if notify_session and notify_session != current_session:
        # Format: [VOICE from session] voice: "text"
        source = current_session or "unknown"
        notification = f"[VOICE from {source}] {voice}: \"{text}\""
        try:
            pane_manager.send_to_pane(notify_session, 0, notification)
        except Exception:
            pass  # Don't fail the say command if notification fails


def _restart_tts_local_for_venv(venv: str, backend: str) -> bool:
    """Restart local TTS server with specific venv (non-interactive).

    Returns True if restart succeeded, False otherwise.
    """
    import time
    session_name = get_tts_session_name()
    config = load_config()
    tts_config = config.get("tts", {})
    port = tts_config.get("port", 8100)
    host = tts_config.get("host", "0.0.0.0")

    # Stop existing server
    if tmux_session_exists(session_name):
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
        time.sleep(1)

    # Find source directory and venv
    source_dir = get_source_dir()
    if not source_dir:
        print("Error: Cannot find agentwire source directory.", file=sys.stderr)
        return False

    venv_name = f".venv-{venv}"
    venv_path = source_dir / venv_name / "bin" / "activate"

    if not venv_path.exists():
        print(f"Error: Venv not found: {venv_path}", file=sys.stderr)
        return False

    # Start server in tmux (non-interactive)
    tts_cmd = (
        f"cd {source_dir} && "
        f"source {venv_name}/bin/activate && "
        f"python -m agentwire tts serve --host {host} --port {port} --backend {backend} --venv {venv}"
    )

    subprocess.run(["tmux", "new-session", "-d", "-s", session_name], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", session_name, tts_cmd, "Enter"], capture_output=True)

    # Wait for server to be ready
    url = f"http://{host}:{port}/health"
    for _ in range(30):  # Wait up to 30 seconds
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass

    print("Warning: Server may not be fully ready yet.", file=sys.stderr)
    return True  # Continue anyway, it might just be slow to load models


def _restart_tts_remote_for_venv(ssh_target: str, machine_id: str, venv: str, backend: str) -> bool:
    """Restart remote TTS server with specific venv/backend.

    Returns True if restart succeeded, False otherwise.
    """
    import time
    session_name = get_tts_session_name()
    config = load_config()
    tts_config = config.get("tts", {})
    port = tts_config.get("port", 8100)
    host = tts_config.get("host", "0.0.0.0")

    # Stop existing server
    kill_cmd = f"tmux kill-session -t {session_name} 2>/dev/null || true"
    subprocess.run(["ssh", ssh_target, kill_cmd], capture_output=True)
    time.sleep(1)

    # Start with new backend (venv is determined by backend on remote)
    # Use agentwire tts start which handles venv selection
    start_cmd = f"~/.local/bin/agentwire tts start --backend {backend}"
    result = subprocess.run(
        ["ssh", ssh_target, start_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0 and "already running" not in result.stdout:
        # Check if it failed for a real reason (not just "already running")
        if "not a terminal" not in result.stdout and "not a terminal" not in result.stderr:
            print(f"Failed to start TTS on {machine_id}: {result.stderr}", file=sys.stderr)
            return False

    # Wait for server to be ready (check via tunnel)
    url = f"http://localhost:{port}/health"
    for _ in range(60):  # Wait up to 60 seconds for remote (models take time)
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass

    print("Warning: Remote server may not be fully ready yet.", file=sys.stderr)
    return True  # Continue anyway


def _restart_tts_for_venv(venv: str, backend: str) -> bool:
    """Restart TTS server with specific venv (non-interactive).

    Handles both local and remote TTS servers.
    Returns True if restart succeeded, False otherwise.
    """
    from .network import NetworkContext

    ctx = NetworkContext.from_config()

    if ctx.is_local("tts"):
        return _restart_tts_local_for_venv(venv, backend)

    # Remote TTS
    ssh_target = ctx.get_ssh_target("tts")
    machine_id = ctx.get_machine_for_service("tts")

    if not ssh_target or not machine_id:
        print("TTS configured for remote machine but machine not found.", file=sys.stderr)
        return False

    print(f"Restarting TTS on {machine_id} with backend '{backend}'...")
    return _restart_tts_remote_for_venv(ssh_target, machine_id, venv, backend)


def _local_say(
    text: str,
    voice: str,
    exaggeration: float,
    cfg_weight: float,
    tts_url: str,
    backend: str | None = None,
    instruct: str | None = None,
    language: str = "English",
    stream: bool = False,
    _retry: bool = False,
) -> int:
    """Generate TTS locally and play via system audio."""

    try:
        # Build request payload
        payload = {
            "text": text,
            "voice": voice,
            "exaggeration": exaggeration,
            "cfg_weight": cfg_weight,
            "language": language,
            "stream": stream,
        }
        if backend:
            payload["backend"] = backend
        if instruct:
            payload["instruct"] = instruct

        data = json.dumps(payload).encode()

        req = urllib.request.Request(
            f"{tts_url}/tts",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=60) as response:
            audio_data = response.read()

        # Save to temp file and play
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name

        # Play audio (cross-platform)
        if sys.platform == "darwin":
            subprocess.run(["afplay", temp_path], check=True)
        elif sys.platform == "linux":
            # Try various players
            for player in ["aplay", "paplay", "play"]:
                try:
                    subprocess.run([player, temp_path], check=True)
                    break
                except FileNotFoundError:
                    continue
        else:
            print(f"Audio saved to: {temp_path}")

        # Clean up
        Path(temp_path).unlink(missing_ok=True)
        return 0

    except urllib.error.HTTPError as e:
        # Try to read the actual error message from the response body
        try:
            error_body = json.loads(e.read().decode())
        except Exception:
            error_body = None

        # Check for venv_mismatch error (422) - auto-restart TTS with correct venv
        if e.code == 422 and not _retry and error_body:
            if error_body.get("error") == "venv_mismatch":
                required_venv = error_body.get("required_venv")
                target_backend = error_body.get("backend", backend)
                print(f"Backend '{target_backend}' requires venv '{required_venv}'. Restarting TTS server...")

                if _restart_tts_for_venv(required_venv, target_backend):
                    print("TTS server restarted. Retrying...")
                    return _local_say(
                        text, voice, exaggeration, cfg_weight, tts_url,
                        backend=target_backend, instruct=instruct, language=language,
                        stream=stream, _retry=True
                    )
                else:
                    print("Failed to restart TTS server.", file=sys.stderr)
                    return 1

        # Show the actual error message from the TTS server if available
        if error_body:
            detail = error_body.get("detail") or error_body.get("error") or error_body
            print(f"TTS error: {detail}", file=sys.stderr)
        else:
            print(f"TTS request failed: {e}", file=sys.stderr)
        return 1

    except urllib.error.URLError as e:
        print(f"TTS server not reachable: {e}", file=sys.stderr)
        print("Start it with: agentwire tts start", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"TTS failed: {e}", file=sys.stderr)
        return 1


def _remote_say(text: str, session: str, portal_url: str) -> int:
    """Send TTS to a session via the portal (for remote sessions)."""
    import ssl

    try:
        # Create SSL context that doesn't verify (self-signed certs)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        data = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            f"{portal_url}/api/say/{session}",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        # 90 second timeout to handle RunPod cold starts
        with urllib.request.urlopen(req, context=ctx, timeout=90) as response:
            result = json.loads(response.read().decode())
            if result.get("error"):
                print(f"Error: {result['error']}", file=sys.stderr)
                return 1

        return 0

    except Exception as e:
        print(f"Failed to send to portal: {e}", file=sys.stderr)
        return 1


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


def store_session_metadata(session_name: str, metadata: dict) -> None:
    """Store session metadata to disk.

    Args:
        session_name: The session name (without @machine suffix if present)
        metadata: Dictionary of metadata to store
    """
    # Parse session name to extract just the name part (remove @machine)
    clean_name = session_name.split("@")[0]

    metadata_dir = CONFIG_DIR / "sessions" / clean_name
    metadata_dir.mkdir(parents=True, exist_ok=True)

    metadata_file = metadata_dir / "metadata.json"

    try:
        with open(metadata_file, "w") as f:
            json.dump(metadata, f, indent=2)
    except (IOError, TypeError):
        pass


def cmd_notify(args) -> int:
    """Send a notification to the portal about session/pane state changes.

    Called by tmux hooks to notify the portal when sessions are created/closed,
    panes are created/killed, clients attach/detach, sessions are renamed, etc.
    The portal broadcasts these events to connected dashboard clients for real-time
    UI updates.
    """
    event = args.event
    session = getattr(args, 'session', None)
    pane = getattr(args, 'pane', None)
    pane_id = getattr(args, 'pane_id', None)
    old_name = getattr(args, 'old_name', None)
    new_name = getattr(args, 'new_name', None)
    json_mode = getattr(args, 'json', False)

    if not event:
        return _output_result(False, json_mode, "Event is required")

    portal_url = _get_portal_url()
    if not portal_url:
        return _output_result(False, json_mode, "Portal URL not configured")

    # Build payload
    payload = {"event": event}
    if session:
        payload["session"] = session
    if pane is not None:
        payload["pane"] = pane
    if pane_id is not None:
        payload["pane_id"] = pane_id
    if old_name is not None:
        payload["old_name"] = old_name
    if new_name is not None:
        payload["new_name"] = new_name

    try:
        # Use urllib to avoid requests dependency in core CLI

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{portal_url}/api/notify",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        # Disable SSL verification for self-signed certs
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
            result = json.loads(response.read().decode())

        if result.get("success"):
            if json_mode:
                _output_json({"success": True, "event": event, "session": session})
            return 0
        else:
            return _output_result(False, json_mode, result.get("error", "Unknown error"))

    except Exception as e:
        # Don't fail loudly - hooks run in background and shouldn't block tmux
        if json_mode:
            _output_json({"success": False, "error": str(e)})
        return 1


# === Session Commands ===

def cmd_send(args) -> int:
    """Send a prompt to a tmux session or pane (adds Enter automatically).

    Supports remote sessions with session@machine format.
    Use --pane N to send to a specific pane in the current session.
    """
    session_full = getattr(args, 'session', None)
    pane_index = getattr(args, 'pane', None)
    prompt = " ".join(args.prompt) if args.prompt else ""
    json_mode = getattr(args, 'json', False)

    # Handle pane mode (auto-detect session from environment)
    if pane_index is not None:
        if not prompt:
            return _output_result(False, json_mode, "Usage: agentwire send --pane N <prompt>")

        try:
            target_session = session_full or pane_manager.get_current_session()
            pane_manager.send_to_pane(session_full, pane_index, prompt)
            if json_mode:
                _output_json({
                    "success": True,
                    "pane": pane_index,
                    "session": target_session,
                    "message": "Prompt sent"
                })
            else:
                print(f"Sent to pane {pane_index}")
            return 0
        except RuntimeError as e:
            return _output_result(False, json_mode, str(e))

    # Session mode (original behavior)
    if not session_full:
        if json_mode:
            print(json.dumps({"success": False, "error": "Session name required (-s) or pane number (--pane)"}))
        else:
            print("Usage: agentwire send -s <session> <prompt>", file=sys.stderr)
            print("   or: agentwire send --pane N <prompt>", file=sys.stderr)
        return 1

    if not prompt:
        if json_mode:
            print(json.dumps({"success": False, "error": "Prompt required"}))
        else:
            print("Usage: agentwire send -s <session> <prompt>", file=sys.stderr)
        return 1

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)
    if machine_id:
        # Remote: SSH and run tmux commands
        machine = _get_machine_config(machine_id)
        if machine is None:
            if json_mode:
                print(json.dumps({"success": False, "error": f"Machine '{machine_id}' not found"}))
            else:
                print(f"Machine '{machine_id}' not found in machines.json", file=sys.stderr)
            return 1

        # Build remote command — use load-buffer for anything non-trivial
        quoted_session = shlex.quote(session)
        use_buffer = len(prompt) > 10 or "\n" in prompt

        if use_buffer:
            encoded = base64.b64encode(prompt.encode()).decode()
            cmd = (
                f"echo {shlex.quote(encoded)} | base64 -d > /tmp/aw-send-$$.txt && "
                f"tmux load-buffer /tmp/aw-send-$$.txt && "
                f"tmux paste-buffer -t {quoted_session} && "
                f"rm -f /tmp/aw-send-$$.txt && "
                f"sleep 0.5 && "
                f"tmux send-keys -t {quoted_session} Enter"
            )
        else:
            quoted_prompt = shlex.quote(prompt)
            cmd = f"tmux send-keys -t {quoted_session} -l {quoted_prompt} && sleep 0.5 && tmux send-keys -t {quoted_session} Enter"

        # For multi-line text, Claude Code shows "[Pasted text...]" and waits for Enter
        if "\n" in prompt or len(prompt) > 200:
            cmd += f" && sleep 0.5 && tmux send-keys -t {quoted_session} Enter"

        result = _run_remote(machine_id, cmd)
        if result.returncode != 0:
            if json_mode:
                print(json.dumps({"success": False, "error": f"Failed to send to {session_full}"}))
            else:
                print(f"Failed to send to {session_full}: {result.stderr}", file=sys.stderr)
            return 1

        if json_mode:
            print(json.dumps({"success": True, "session": session_full, "machine": machine_id, "message": "Prompt sent"}))
        else:
            print(f"Sent to {session_full}")
        return 0

    # Local: existing logic
    # Check if session exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if result.returncode != 0:
        if json_mode:
            print(json.dumps({"success": False, "error": f"Session '{session}' not found"}))
        else:
            print(f"Session '{session}' not found", file=sys.stderr)
        return 1

    # Send the prompt — use load-buffer for anything non-trivial to avoid PTY flooding
    use_buffer = len(prompt) > 10 or "\n" in prompt

    if use_buffer:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(prompt)
            temp_path = f.name
        try:
            subprocess.run(["tmux", "load-buffer", temp_path], check=True)
            subprocess.run(["tmux", "paste-buffer", "-t", session], check=True)
        finally:
            os.unlink(temp_path)
    else:
        subprocess.run(
            ["tmux", "send-keys", "-t", session, "-l", prompt],
            check=True
        )

    # Wait for text to be displayed before pressing Enter
    time.sleep(0.5)

    subprocess.run(
        ["tmux", "send-keys", "-t", session, "Enter"],
        check=True
    )

    # For multi-line text, Claude Code shows "[Pasted text...]" and waits for Enter
    # Send another Enter after a short delay to confirm the paste
    if "\n" in prompt or len(prompt) > 200:
        time.sleep(0.5)
        subprocess.run(
            ["tmux", "send-keys", "-t", session, "Enter"],
            check=True
        )

    if json_mode:
        print(json.dumps({"success": True, "session": session_full, "machine": None, "message": "Prompt sent"}))
    else:
        print(f"Sent to {session}")
    return 0


def cmd_list(args) -> int:
    """List tmux sessions or panes.

    When inside a tmux session, shows panes by default.
    Use --sessions to show sessions instead.
    """
    json_mode = getattr(args, 'json', False)

    if not _check_tmux_installed():
        return 1 if not json_mode else _output_result(False, json_mode, "tmux is required but not installed")
    local_only = getattr(args, 'local', False)
    remote_only = getattr(args, 'remote', False)
    machine_filter = getattr(args, 'machine', None)
    show_sessions = getattr(args, 'sessions', False)

    # Check if we're inside a tmux session
    current_session = pane_manager.get_current_session()

    # If inside tmux and not explicitly asking for sessions, show panes
    if current_session and not show_sessions:
        panes = pane_manager.list_panes(current_session)

        if json_mode:
            pane_data = [
                {
                    "index": p.index,
                    "pane_id": p.pane_id,
                    "pid": p.pid,
                    "command": p.command,
                    "active": p.active,
                }
                for p in panes
            ]
            _output_json({"success": True, "session": current_session, "panes": pane_data})
            return 0

        if not panes:
            print(f"No panes in session '{current_session}'")
            return 0

        print(f"Panes in {current_session}:")
        for p in panes:
            active_marker = " *" if p.active else ""
            role = "orchestrator" if p.index == 0 else "worker"
            print(f"  {p.index}: [{role}] {p.command}{active_marker}")
        return 0

    # Show sessions (original behavior)
    all_sessions = []

    # Get local sessions (skip if remote_only)
    local_sessions = []
    if not remote_only:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}:#{session_windows}:#{pane_current_path}"],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split(":", 2)
                    if len(parts) >= 2:
                        path = parts[2] if len(parts) > 2 else ""
                        cfg = _get_session_config_from_path(path)
                        session_info = {
                            "name": parts[0],
                            "windows": int(parts[1]) if parts[1].isdigit() else 1,
                            "path": path,
                            "machine": None,
                            "type": cfg.get("type"),
                            "roles": cfg.get("roles", []),
                        }
                        local_sessions.append(session_info)
                        all_sessions.append(session_info)

    # Get remote sessions from all registered machines (skip if local_only)
    remote_by_machine = {}
    if not local_only:
        machines = _get_all_machines()
        for machine in machines:
            machine_id = machine.get("id")
            if not machine_id:
                continue

            # Skip "local" machine (reserved for future use)
            if machine_id == "local":
                continue

            # Filter by specific machine if requested
            if machine_filter and machine_id != machine_filter:
                continue

            cmd = "tmux list-sessions -F '#{session_name}:#{session_windows}:#{pane_current_path}' 2>/dev/null || echo ''"
            result = _run_remote(machine_id, cmd)

            machine_sessions = []
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split("\n"):
                    if line:
                        parts = line.split(":", 2)
                        if len(parts) >= 2:
                            remote_path = parts[2] if len(parts) > 2 else ""
                            session_info = {
                                "name": f"{parts[0]}@{machine_id}",
                                "windows": int(parts[1]) if parts[1].isdigit() else 1,
                                "path": remote_path,
                                "machine": machine_id,
                                "type": _get_remote_session_type(machine_id, remote_path),
                            }
                            machine_sessions.append(session_info)
                            all_sessions.append(session_info)

            remote_by_machine[machine_id] = machine_sessions

    # Output
    if json_mode:
        _output_json({"success": True, "sessions": all_sessions})
        return 0

    # Text output - grouped by machine
    # Combine local and remote sessions into single machine-based view
    all_machines = {}

    # Add local sessions to machine view
    for s in local_sessions:
        machine = s['machine']
        if machine not in all_machines:
            all_machines[machine] = []
        all_machines[machine].append(s)

    # Add remote sessions to machine view
    for machine_id, sessions in remote_by_machine.items():
        if machine_id not in all_machines:
            all_machines[machine_id] = []
        all_machines[machine_id].extend(sessions)

    if not all_machines:
        print("No sessions running")
        return 0

    # Display all sessions grouped by machine
    for machine_id, sessions in sorted(all_machines.items(), key=lambda x: (x[0] is not None, x[0])):
        label = machine_id if machine_id else "local"
        print(f"{label}:")
        if sessions:
            for s in sessions:
                # Remove @machine suffix for display within machine group
                display_name = s['name'].rsplit('@', 1)[0] if '@' in s['name'] else s['name']
                print(f"  {display_name}: {s['windows']} window(s) ({s['path']})")
        else:
            print("  (no sessions)")
        print()

    return 0


def cmd_repl(args) -> int:
    """Agentwire REPL — interactive harness built on claude-agent-sdk.

    Invoked by build_agent_command when a session is spawned with --type sdk-*.
    Interactive mode auto-persists every turn under
    `~/.agentwire/sessions/repl/<session-name>/`; `--resume NAME` continues a
    prior session by reusing its sdk_session_id.
    """
    from agentwire.repl.app import run_repl
    return run_repl(
        mode=args.mode,
        model=args.model,
        print_prompt=args.print,
        system_prompt=args.append_system_prompt,
        session_name=getattr(args, "session_name", None),
        resume=getattr(args, "resume", None),
        roles=getattr(args, "roles", None),
        view=getattr(args, "view", "chat"),
        cols=getattr(args, "cols", 3),
        col_models=getattr(args, "col_models", None),
        col_efforts=getattr(args, "col_efforts", None),
        col_roles=getattr(args, "col_roles", None),
    )


def cmd_new(args) -> int:
    """Create a new agent session.

    Supports:
    - "project" -> simple session in ~/projects/project/
    - "project/branch" -> worktree session in ~/projects/project-worktrees/branch/
    - "project@machine" -> remote session
    - "project/branch@machine" -> remote worktree session
    """
    json_mode = getattr(args, 'json', False)

    if not _check_config_exists():
        return 1 if not json_mode else _output_result(False, json_mode, "AgentWire not configured. Run 'agentwire init'")

    if not _check_tmux_installed():
        return 1 if not json_mode else _output_result(False, json_mode, "tmux is required but not installed")

    name = args.session
    path = args.path

    if not name:
        return _output_result(False, json_mode, "Usage: agentwire new -s <name> [-p path] [-f]")

    # Parse roles from CLI or existing .agentwire.yml
    roles_arg = getattr(args, 'roles', None)
    role_names: list[str] = []
    if roles_arg:
        role_names = [r.strip() for r in roles_arg.split(",") if r.strip()]
    else:
        # Check existing .agentwire.yml in the project
        project_path_for_config = Path(path).expanduser().resolve() if path else None
        if project_path_for_config:
            existing = load_project_config(project_path_for_config)
            if existing and existing.roles:
                role_names = existing.roles
            else:
                # Use configured default role for new projects
                config = load_config()
                default_role = config.get("session", {}).get("default_role", "agentwire")
                role_names = [default_role] if default_role else []
        else:
            # Use configured default role when no path specified
            config = load_config()
            default_role = config.get("session", {}).get("default_role", "agentwire")
            role_names = [default_role] if default_role else []

    # Load and validate roles
    roles: list[RoleConfig] = []
    if role_names:
        # Determine project path for role discovery
        project_path_for_roles = Path(path).expanduser().resolve() if path else None
        roles, missing = load_roles(role_names, project_path_for_roles)
        if missing:
            return _output_result(False, json_mode, f"Roles not found: {', '.join(missing)}")

    # Parse session name: project, branch, machine
    project, branch, machine_id = parse_session_name(name)

    # Build the tmux session name (convert dots to underscores, preserve slashes)
    if branch:
        session_name = f"{project}/{branch}".replace(".", "_")
    else:
        session_name = project.replace(".", "_")

    # Load config
    config = load_config()
    projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser()
    worktrees_config = config.get("projects", {}).get("worktrees", {})
    worktrees_enabled = worktrees_config.get("enabled", True)
    worktree_suffix = worktrees_config.get("suffix", "-worktrees")
    auto_create_branch = worktrees_config.get("auto_create_branch", True)

    # Handle remote session
    if machine_id:
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        remote_projects_dir = machine.get("projects_dir", "~/projects")

        # Build remote path
        if path:
            remote_path = path
        elif branch:
            remote_path = f"{remote_projects_dir}/{project}{worktree_suffix}/{branch}"
        else:
            remote_path = f"{remote_projects_dir}/{project}"

        # If branch specified, create worktree on remote
        if branch:
            # Create worktree on remote
            project_path = f"{remote_projects_dir}/{project}"
            worktree_path = remote_path

            # Check if worktree already exists
            check_cmd = f"test -d {shlex.quote(worktree_path)}"
            result = _run_remote(machine_id, check_cmd)

            if result.returncode != 0:
                # Create worktree
                # First check if branch exists
                branch_check = f"cd {shlex.quote(project_path)} && git rev-parse --verify refs/heads/{shlex.quote(branch)} 2>/dev/null"
                result = _run_remote(machine_id, branch_check)

                if result.returncode == 0:
                    # Branch exists, create worktree
                    create_cmd = f"cd {shlex.quote(project_path)} && mkdir -p $(dirname {shlex.quote(worktree_path)}) && git worktree add {shlex.quote(worktree_path)} {shlex.quote(branch)}"
                elif auto_create_branch:
                    # Create branch with worktree
                    create_cmd = f"cd {shlex.quote(project_path)} && mkdir -p $(dirname {shlex.quote(worktree_path)}) && git worktree add -b {shlex.quote(branch)} {shlex.quote(worktree_path)}"
                else:
                    return _output_result(False, json_mode, f"Branch '{branch}' does not exist and auto_create_branch is disabled")

                result = _run_remote(machine_id, create_cmd)
                if result.returncode != 0:
                    return _output_result(False, json_mode, f"Failed to create remote worktree: {result.stderr}")

        # Check if remote session already exists
        check_cmd = f"tmux has-session -t ={shlex.quote(session_name)} 2>/dev/null"
        result = _run_remote(machine_id, check_cmd)
        if result.returncode == 0:
            if args.force:
                # Kill existing session
                kill_cmd = f"tmux send-keys -t {shlex.quote(session_name)} /exit Enter && sleep 2 && tmux kill-session -t {shlex.quote(session_name)} 2>/dev/null"
                _run_remote(machine_id, kill_cmd)
            else:
                return _output_result(False, json_mode, f"Session '{session_name}' already exists on {machine_id}. Use -f to replace.")

        # Create remote tmux session
        # Determine agent type and session type from CLI --type flag or boolean flags
        agent_type = detect_default_agent_type()

        # Check explicit --type flag first (from portal UI or CLI)
        if getattr(args, 'type', None):
            session_type = args.type
        elif getattr(args, 'bare', False):
            session_type = "bare"
        elif getattr(args, 'restricted', False):
            session_type = f"{agent_type}-restricted"
        elif getattr(args, 'prompted', False):
            session_type = f"{agent_type}-prompted"
        else:
            session_type = f"{agent_type}-bypass"

        # Build agent command
        agent = build_agent_command(session_type, roles if roles else None)
        agent.env.update(parse_env_args(getattr(args, 'env', None)))

        agent_cmd = agent.command

        # If agent command uses a local temp file, write content to remote
        if agent.temp_file and agent_cmd:
            try:
                with open(agent.temp_file, 'r') as f:
                    content = f.read()
                # Create remote temp file with same content
                remote_temp = f"/tmp/agentwire-prompt-{session_name.replace('/', '-')}.txt"
                # Escape content for shell
                escaped_content = content.replace("'", "'\"'\"'")
                write_cmd = f"cat > {shlex.quote(remote_temp)} << 'AGENTWIRE_EOF'\n{content}\nAGENTWIRE_EOF"
                result = _run_remote(machine_id, write_cmd)
                if result.returncode == 0:
                    # Replace local path with remote path in command
                    agent_cmd = agent_cmd.replace(agent.temp_file, remote_temp)
            except Exception as e:
                print(f"Warning: Failed to write system prompt to remote: {e}", file=sys.stderr)

        env_flags = _build_tmux_env_flags_shell(agent.env)

        # Create session - Agent starts immediately if not bare.
        # `-e K=V` on new-session places env in the session environment BEFORE
        # the initial shell starts, so send-keys'd agent command sees it.
        if agent_cmd:
            create_cmd = (
                f"tmux new-session -d -s {shlex.quote(session_name)} -c {shlex.quote(remote_path)} {env_flags}&& "
                f"tmux send-keys -t {shlex.quote(session_name)} 'cd {shlex.quote(remote_path)}' Enter && "
                f"sleep 0.1 && "
                f"tmux send-keys -t {shlex.quote(session_name)} {shlex.quote(agent_cmd)} Enter"
            )
        else:
            # Bare session - just create tmux
            create_cmd = (
                f"tmux new-session -d -s {shlex.quote(session_name)} -c {shlex.quote(remote_path)} {env_flags}&& "
                f"tmux send-keys -t {shlex.quote(session_name)} 'cd {shlex.quote(remote_path)}' Enter"
            )

        result = _run_remote(machine_id, create_cmd)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create remote session: {result.stderr}")

        if json_mode:
            _output_json({
                "success": True,
                "session": f"{session_name}@{machine_id}",
                "path": remote_path,
                "branch": branch,
                "machine": machine_id,
            })
        else:
            print(f"Created session '{session_name}' on {machine_id} in {remote_path}")
            print(f"Attach via portal or: ssh {machine.get('host', machine_id)} -t tmux attach -t {session_name}")

        _notify_portal_sessions_changed()
        return 0

    # Local session
    # Resolve path
    if path and branch and worktrees_enabled:
        # Path + branch: use provided path as main repo, create worktree from it
        project_path = Path(path).expanduser().resolve()
        session_path = project_path.parent / f"{project_path.name}{worktree_suffix}" / branch

        # Ensure worktree exists
        if not session_path.exists():
            if not project_path.exists():
                return _output_result(False, json_mode, f"Project path does not exist: {project_path}")

            success = ensure_worktree(
                project_path,
                branch,
                session_path,
                auto_create_branch=auto_create_branch,
            )
            if not success:
                return _output_result(False, json_mode, f"Failed to create worktree for branch '{branch}' in {project_path}")
    elif path:
        session_path = Path(path).expanduser().resolve()
    elif branch and worktrees_enabled:
        # Worktree session: ~/projects/project-worktrees/branch/
        project_path = projects_dir / project
        session_path = projects_dir / f"{project}{worktree_suffix}" / branch

        # Ensure worktree exists
        if not session_path.exists():
            if not project_path.exists():
                return _output_result(False, json_mode, f"Project path does not exist: {project_path}")

            success = ensure_worktree(
                project_path,
                branch,
                session_path,
                auto_create_branch=auto_create_branch,
            )
            if not success:
                return _output_result(False, json_mode, f"Failed to create worktree for branch '{branch}' in {project_path}")
    else:
        # Simple session: ~/projects/project/
        session_path = projects_dir / project

    if not session_path.exists():
        if args.force or path:
            # Auto-create directory with -f flag or when custom path explicitly provided
            session_path.mkdir(parents=True, exist_ok=True)
        else:
            return _output_result(False, json_mode, f"Path does not exist: {session_path}")

    # Check if session already exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={session_name}"],
        capture_output=True
    )
    if result.returncode == 0:
        if args.force:
            # Kill existing session
            subprocess.run(["tmux", "send-keys", "-t", session_name, "/exit", "Enter"])
            time.sleep(2)
            subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
        else:
            return _output_result(False, json_mode, f"Session '{session_name}' already exists. Use -f to replace.")

    # Determine agent type and normalize session type BEFORE creating the
    # tmux session, so we can inject secrets via `tmux new-session -e K=V`
    # (the only way to make the initial pane's shell see them — post-hoc
    # `tmux set-environment` only affects shells spawned AFTER the call).
    agent_type = detect_default_agent_type()

    # Determine session type from CLI --type flag or existing config
    persist = getattr(args, 'persist', False)
    type_arg = getattr(args, 'type', None)
    if type_arg:
        # CLI flag specified - use it directly and normalize
        session_type = normalize_session_type(type_arg, agent_type)
        # Only save to .agentwire.yml if --persist is given
        if session_path and persist:
            existing_config = load_project_config(session_path)
            project_config = ProjectConfig(
                type=SessionType.from_str(session_type),
                roles=role_names if role_names else (existing_config.roles if existing_config else []),
                voice=existing_config.voice if existing_config else None,
                parent=existing_config.parent if existing_config else None,
                shell=existing_config.shell if existing_config else None,
                tasks=existing_config.tasks if existing_config else {},
            )
            save_project_config(project_config, session_path)
    else:
        # Check existing .agentwire.yml for type
        existing_config = load_project_config(session_path)
        if existing_config and existing_config.type:
            # Normalize in case it's a universal type
            session_type = normalize_session_type(existing_config.type.value, agent_type)
        else:
            # Default to standard
            session_type = f"{agent_type}-bypass"

    # Build agent command
    model_override = getattr(args, 'model', None)
    agent = build_agent_command(session_type, roles if roles else None, model=model_override)
    agent.env.update(parse_env_args(getattr(args, 'env', None)))

    agent_cmd = agent.command

    # Create new tmux session with env vars injected at creation time.
    # `-e K=V` places vars in the session environment before the initial
    # shell starts, so the agent command (run via send-keys below) sees them.
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(session_path),
         *_build_tmux_env_flags(agent.env)],
        check=True
    )

    # Ensure Claude starts in correct directory
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, f"cd {shlex.quote(str(session_path))}", "Enter"],
        check=True
    )
    time.sleep(0.1)

    # Start agent command if not bare
    if agent_cmd:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, agent_cmd, "Enter"],
            check=True
        )

    # Update project config (.agentwire.yml) - only if --persist is given
    if persist:
        existing_config = load_project_config(session_path)
        if existing_config:
            # Preserve existing settings if not overridden by CLI
            project_config = ProjectConfig(
                type=SessionType.from_str(session_type),
                roles=role_names if type_arg else existing_config.roles,
                voice=existing_config.voice,
                parent=existing_config.parent,
                shell=existing_config.shell,
                tasks=existing_config.tasks,
            )
        else:
            # Create new config
            project_config = ProjectConfig(
                type=SessionType.from_str(session_type),
                roles=role_names if role_names else [],
                voice=None,
            )
        save_project_config(project_config, session_path)

    if json_mode:
        _output_json({
            "success": True,
            "session": session_name,
            "path": str(session_path),
            "branch": branch,
            "machine": None,
        })
    else:
        print(f"Created session '{session_name}' in {session_path}")
        print(f"Attach with: tmux attach -t {session_name}")

    _notify_portal_sessions_changed()
    return 0


def cmd_output(args) -> int:
    """Read output from a tmux session or pane.

    Supports remote sessions with session@machine format.
    Use --pane N to read from a specific pane in the current session.
    """
    session_full = getattr(args, 'session', None)
    pane_index = getattr(args, 'pane', None)
    lines = args.lines or 50
    json_mode = getattr(args, 'json', False)

    # Handle pane mode (auto-detect session from environment)
    if pane_index is not None:
        try:
            output = pane_manager.capture_pane(session_full, pane_index, lines)
            if json_mode:
                _output_json({
                    "success": True,
                    "pane": pane_index,
                    "session": session_full or pane_manager.get_current_session(),
                    "lines": lines,
                    "output": output
                })
            else:
                print(output)
            return 0
        except RuntimeError as e:
            return _output_result(False, json_mode, str(e))

    # Session mode (original behavior)
    if not session_full:
        if json_mode:
            print(json.dumps({"success": False, "error": "Session name required (-s) or pane number (--pane)"}))
        else:
            print("Usage: agentwire output -s <session> [-n lines]", file=sys.stderr)
            print("   or: agentwire output --pane N [-n lines]", file=sys.stderr)
        return 1

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)

    if machine_id:
        # Remote: SSH and run tmux capture-pane
        machine = _get_machine_config(machine_id)
        if machine is None:
            if json_mode:
                print(json.dumps({"success": False, "error": f"Machine '{machine_id}' not found"}))
            else:
                print(f"Machine '{machine_id}' not found in machines.json", file=sys.stderr)
            return 1

        cmd = f"tmux capture-pane -t {shlex.quote(session)} -p -S -{lines}"
        result = _run_remote(machine_id, cmd)

        if result.returncode != 0:
            if json_mode:
                print(json.dumps({"success": False, "error": f"Session '{session}' not found on {machine_id}"}))
            else:
                print(f"Session '{session}' not found on {machine_id}", file=sys.stderr)
            return 1

        if json_mode:
            print(json.dumps({
                "success": True,
                "session": session_full,
                "lines": lines,
                "machine": machine_id,
                "output": result.stdout
            }))
        else:
            print(result.stdout)
        return 0

    # Local: existing logic
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if result.returncode != 0:
        if json_mode:
            print(json.dumps({"success": False, "error": f"Session '{session}' not found"}))
        else:
            print(f"Session '{session}' not found", file=sys.stderr)
        return 1

    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
        capture_output=True,
        text=True
    )

    if json_mode:
        print(json.dumps({
            "success": True,
            "session": session_full,
            "lines": lines,
            "machine": None,
            "output": result.stdout
        }))
    else:
        print(result.stdout)
    return 0


def cmd_info(args) -> int:
    """Get session information as JSON.

    Returns working directory, pane count, and other metadata.
    """
    session_full = args.session
    json_mode = getattr(args, 'json', True)  # Default to JSON

    if not session_full:
        return _output_result(False, json_mode, "Session name required (-s)")

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)

    if machine_id:
        # Remote session
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found")

        # Get session info via SSH
        cmd = f"tmux display-message -t {shlex.quote(session)} -p '#{{pane_current_path}}:#{{window_panes}}' 2>/dev/null"
        result = _run_remote(machine_id, cmd)

        if result.returncode != 0:
            return _output_result(False, json_mode, f"Session '{session}' not found on {machine_id}")

        parts = result.stdout.strip().split(":")
        cwd = parts[0] if parts else ""
        pane_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

        info = {
            "success": True,
            "session": session_full,
            "name": session,
            "machine": machine_id,
            "cwd": cwd,
            "pane_count": pane_count,
            "is_remote": True,
        }
    else:
        # Local session
        if not tmux_session_exists(session):
            return _output_result(False, json_mode, f"Session '{session}' not found")

        # Get working directory
        result = subprocess.run(
            ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}:#{window_panes}"],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return _output_result(False, json_mode, f"Could not get info for '{session}'")

        parts = result.stdout.strip().split(":")
        cwd = parts[0] if parts else ""
        pane_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

        # Get pane details
        panes_result = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_index}:#{pane_current_command}:#{pane_active}"],
            capture_output=True,
            text=True,
        )
        panes = []
        if panes_result.returncode == 0:
            for line in panes_result.stdout.strip().split("\n"):
                if line:
                    pane_parts = line.split(":")
                    if len(pane_parts) >= 3:
                        panes.append({
                            "index": int(pane_parts[0]),
                            "command": pane_parts[1],
                            "active": pane_parts[2] == "1",
                        })

        info = {
            "success": True,
            "session": session,
            "name": session,
            "machine": None,
            "cwd": cwd,
            "pane_count": pane_count,
            "panes": panes,
            "is_remote": False,
        }

    if json_mode:
        print(json.dumps(info))
    else:
        print(f"Session: {info['name']}")
        if info['machine']:
            print(f"Machine: {info['machine']}")
        print(f"CWD: {info['cwd']}")
        print(f"Panes: {info['pane_count']}")

    return 0


def cmd_kill(args) -> int:
    """Kill a tmux session or pane (with clean Claude exit).

    Supports remote sessions with session@machine format.
    Use --pane N to kill a specific pane in the current session.
    """
    session_full = getattr(args, 'session', None)
    pane_index = getattr(args, 'pane', None)
    json_mode = getattr(args, 'json', False)

    # Handle pane mode (auto-detect session from environment)
    if pane_index is not None:
        if pane_index == 0:
            return _output_result(False, json_mode, "Cannot kill pane 0 (orchestrator)")

        try:
            session = session_full or pane_manager.get_current_session()
            if not session:
                return _output_result(False, json_mode, "Not in tmux session and no session specified")

            # Send /exit for clean shutdown (use send_to_pane for proper timing)
            pane_manager.send_to_pane(session, pane_index, "/exit")
            if not json_mode:
                print(f"Sent /exit to pane {pane_index}, waiting 3s...")
            time.sleep(3)

            # Kill the pane
            pane_manager.kill_pane(session, pane_index)

            if json_mode:
                _output_json({
                    "success": True,
                    "pane": pane_index,
                    "session": session,
                })
            else:
                print(f"Killed pane {pane_index}")
            return 0
        except RuntimeError as e:
            return _output_result(False, json_mode, str(e))

    # Session mode (original behavior)
    if not session_full:
        return _output_result(False, json_mode, "Usage: agentwire kill -s <session> or --pane N")

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)

    if machine_id:
        # Remote: SSH and run tmux commands
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        # Check if session exists
        check_cmd = f"tmux has-session -t {shlex.quote(session)} 2>/dev/null"
        result = _run_remote(machine_id, check_cmd)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Session '{session}' not found on {machine_id}")

        # Send /exit to Claude first for clean shutdown (target pane 0 specifically)
        exit_cmd = f"tmux send-keys -t {shlex.quote(session)}.0 /exit Enter"
        _run_remote(machine_id, exit_cmd)
        if not json_mode:
            print(f"Sent /exit to {session_full}, waiting 3s...")
        time.sleep(3)

        # Kill the session
        kill_cmd = f"tmux kill-session -t {shlex.quote(session)}"
        _run_remote(machine_id, kill_cmd)
        if not json_mode:
            print(f"Killed session '{session_full}'")

        _notify_portal_sessions_changed()

        if json_mode:
            _output_json({"success": True, "session": session_full})
        return 0

    # Local: existing logic
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if result.returncode != 0:
        return _output_result(False, json_mode, f"Session '{session}' not found")

    # Send /exit to Claude first for clean shutdown
    # Target pane 0 specifically and capture output to avoid terminal noise
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{session}.0", "/exit", "Enter"],
        capture_output=True
    )
    if not json_mode:
        print(f"Sent /exit to {session}, waiting 3s...")
    time.sleep(3)

    # Kill the session
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    if not json_mode:
        print(f"Killed session '{session}'")

    _notify_portal_sessions_changed()

    if json_mode:
        _output_json({"success": True, "session": session_full})
    return 0


def _wait_for_agent_ready(session: str, timeout: int = 30, pane_index: int = 0) -> bool:
    """Wait for an agent to be ready to accept input.

    Handles the first-time folder trust prompt by auto-accepting it.
    Returns True if agent became ready, False if timeout.
    """
    start = time.time()
    trust_accepted = False

    while (time.time() - start) < timeout:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", f"{session}.{pane_index}", "-p", "-S", "-20"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                output = result.stdout

                # Handle first-time bypass trust prompt
                if not trust_accepted and ("trust this folder" in output.lower() or "enter to confirm" in output.lower()):
                    subprocess.run(
                        ["tmux", "send-keys", "-t", f"{session}.{pane_index}", "Enter"],
                        capture_output=True,
                    )
                    trust_accepted = True
                    time.sleep(2)
                    continue

                # Check for ready prompt
                if "❯" in output and "bypass" in output.lower():
                    time.sleep(0.3)
                    return True
        except Exception:
            pass
        time.sleep(0.5)

    return False


def _wait_for_worker_ready(session: str, pane_index: int, timeout: int = 30, agent_type: str = "claude") -> bool:
    """Wait for a worker pane to be ready to receive input.

    Polls the pane output looking for Claude Code's '❯' prompt.

    Returns True if worker became ready, False if timeout.
    """
    import time

    start = time.time()
    poll_interval = 0.5  # Check every 500ms

    ready_indicators = ['❯', '>', 'Claude Code']

    while (time.time() - start) < timeout:
        try:
            # Use session.N to target pane N (omit window index, let tmux resolve it)
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", f"{session}.{pane_index}", "-p", "-S", "-20"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                output = result.stdout
                # Check for any ready indicator
                for indicator in ready_indicators:
                    if indicator in output:
                        # Extra wait to ensure fully ready
                        time.sleep(0.3)
                        return True
        except Exception:
            pass

        time.sleep(poll_interval)

    return False


def cmd_spawn(args) -> int:
    """Spawn a worker pane in the current session.

    Creates a new tmux pane in the orchestrator's session and starts
    Claude Code with the specified roles (default: worker).

    With --branch, creates an isolated worktree for the worker to enable
    parallel commits without conflicts.

    By default, waits for the worker to be ready before returning.
    Use --no-wait to return immediately after spawning.
    """
    json_mode = getattr(args, 'json', False)
    cwd = getattr(args, 'cwd', None)
    roles_arg = getattr(args, 'roles', 'worker')
    session = getattr(args, 'session', None)
    branch = getattr(args, 'branch', None)
    no_wait = getattr(args, 'no_wait', False)
    timeout = getattr(args, 'timeout', 30)

    # If cwd not specified, use the target session's pane 0 directory
    if not cwd:
        target_session = session or pane_manager.get_current_session()
        if target_session:
            result = subprocess.run(
                ["tmux", "display", "-t", f"{target_session}.0", "-p", "#{pane_current_path}"],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                cwd = result.stdout.strip()
        if not cwd:
            cwd = os.getcwd()

    worktree_path = None

    # Handle --branch: create worktree for isolated work
    if branch:
        try:
            worktree_path = pane_manager.create_worker_worktree(branch, cwd)
            cwd = worktree_path
            if not json_mode:
                print(f"Created worktree at {worktree_path}")
        except RuntimeError as e:
            return _output_result(False, json_mode, f"Failed to create worktree: {e}")

    # Parse roles
    role_names = [r.strip() for r in roles_arg.split(",") if r.strip()]

    # Load and validate roles
    roles, missing = load_roles(role_names, Path(cwd))
    if missing:
        return _output_result(False, json_mode, f"Roles not found: {', '.join(missing)}")

    # Use provided type or default to {agent_type}-restricted
    session_type_arg = getattr(args, 'type', None)
    if session_type_arg:
        session_type_str = session_type_arg
    else:
        agent_type = detect_default_agent_type()
        session_type_str = f"{agent_type}-restricted"

    # Build agent command
    agent = build_agent_command(session_type_str, roles if roles else None)
    agent.env.update(parse_env_args(getattr(args, 'env', None)))

    agent_cmd = agent.command

    try:
        # Inject secrets onto the parent session before spawning the pane so the
        # new pane inherits them from tmux (avoids putting keys in `ps`).
        parent_session = session or pane_manager.get_current_session()
        if parent_session:
            inject_session_env(parent_session, agent.env)

        # Spawn pane first to get the pane index
        pane_index = pane_manager.spawn_worker_pane(
            session=session,
            cwd=cwd,
            cmd=agent_cmd
        )

        # Install pane hook to notify portal when pane exits
        actual_session = session or pane_manager.get_current_session()
        _install_pane_hooks(actual_session, pane_index)

        # Wait for worker to be ready (unless --no-wait)
        worker_ready = True
        if not no_wait:
            worker_ready = _wait_for_worker_ready(actual_session, pane_index, timeout)

        if json_mode:
            result = {
                "success": True,
                "pane": pane_index,
                "session": actual_session,
                "roles": role_names,
                "ready": worker_ready,
            }
            if branch:
                result["branch"] = branch
                result["worktree"] = worktree_path
            _output_json(result)
        else:
            if worker_ready:
                print(f"Spawned pane {pane_index}")
            else:
                print(f"Spawned pane {pane_index} (timeout waiting for ready)")

        return 0

    except RuntimeError as e:
        return _output_result(False, json_mode, str(e))


def cmd_split(args) -> int:
    """Add terminal pane(s) to current session with even vertical layout."""
    count = getattr(args, 'count', 1)
    cwd = getattr(args, 'cwd', None) or os.getcwd()
    session = getattr(args, 'session', None)

    # Get current session if not specified
    if not session:
        session = os.environ.get("TMUX_PANE")
        if session:
            # We're in tmux, get session name
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#{session_name}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                session = result.stdout.strip()
            else:
                session = None

    if not session:
        print("Error: Not in a tmux session and no --session specified")
        return 1

    # Verify session exists
    check = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if check.returncode != 0:
        print(f"Error: Session '{session}' not found")
        return 1

    # Add panes
    for i in range(count):
        result = subprocess.run([
            "tmux", "split-window", "-v", "-t", session, "-c", cwd
        ], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error: Failed to split pane: {result.stderr.strip()}")
            return 1

    # Apply main-top layout: orchestrator (pane 0) at top with 60%, workers below
    pane_manager._apply_main_top_layout(session)
    subprocess.run(["tmux", "select-pane", "-t", f"{session}.0"], capture_output=True)

    pane_count = 1 + count  # original + new
    print(f"Added {count} pane(s) - now {pane_count} panes")
    return 0


def cmd_detach(args) -> int:
    """Move a pane to its own session and re-align remaining panes."""
    pane_index = getattr(args, 'pane', None)
    new_session = getattr(args, 'session', None)
    source_session = getattr(args, 'source', None)

    if pane_index is None or new_session is None:
        print("Error: --pane and -s/--session are required")
        return 1

    # Get source session if not specified
    if not source_session:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            source_session = result.stdout.strip()
        else:
            print("Error: Could not detect current session")
            return 1

    # Check if target session already exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", new_session],
        capture_output=True
    )
    session_exists = result.returncode == 0

    # Verify source session exists
    check = subprocess.run(
        ["tmux", "has-session", "-t", source_session],
        capture_output=True
    )
    if check.returncode != 0:
        print(f"Error: Session '{source_session}' not found")
        return 1

    # Move pane to new session
    if session_exists:
        # Move to existing session
        result = subprocess.run([
            "tmux", "move-pane", "-s", f"{source_session}:{pane_index}", "-t", f"{new_session}:"
        ], capture_output=True, text=True)
    else:
        # Break pane into new session
        result = subprocess.run([
            "tmux", "break-pane", "-d", "-s", f"{source_session}:{pane_index}", "-t", f"{new_session}:"
        ], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error: Failed to detach pane {pane_index}: {result.stderr.strip()}")
        return 1

    # Re-align remaining panes with main-top layout
    pane_manager._apply_main_top_layout(source_session)
    subprocess.run(["tmux", "select-pane", "-t", f"{source_session}.0"], capture_output=True)

    print(f"Moved pane {pane_index} to session '{new_session}'")
    return 0


def cmd_jump(args) -> int:
    """Jump to (focus) a specific pane."""
    json_mode = getattr(args, 'json', False)
    pane_index = getattr(args, 'pane', None)
    session = getattr(args, 'session', None)

    if pane_index is None:
        return _output_result(False, json_mode, "Usage: agentwire jump --pane N")

    try:
        pane_manager.focus_pane(session, pane_index)

        if json_mode:
            _output_json({
                "success": True,
                "pane": pane_index,
                "session": session or pane_manager.get_current_session(),
            })
        else:
            print(f"Jumped to pane {pane_index}")

        return 0

    except RuntimeError as e:
        return _output_result(False, json_mode, str(e))


def cmd_resize(args) -> int:
    """Resize tmux window to fit the largest attached client."""
    json_mode = getattr(args, 'json', False)
    session = getattr(args, 'session', None)

    # Get session name
    if not session:
        session = pane_manager.get_current_session()
        if not session:
            return _output_result(False, json_mode, "Not in a tmux session. Use -s to specify session.")

    try:
        result = subprocess.run(
            ["tmux", "resize-window", "-A", "-t", session],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to resize: {result.stderr.strip()}")

        if json_mode:
            _output_json({"success": True, "session": session})
        else:
            print(f"Resized {session} to fit largest client")

        return 0

    except Exception as e:
        return _output_result(False, json_mode, str(e))


def cmd_send_keys(args) -> int:
    """Send raw keys to a tmux session (no automatic Enter).

    Each argument is sent as a separate key group with a brief pause between.
    Useful for sending special keys like Enter, Escape, C-c, etc.

    Supports remote sessions with session@machine format.
    """
    session_full = args.session
    keys = args.keys if args.keys else []

    if not session_full:
        print("Usage: agentwire send-keys -s <session> <keys>...", file=sys.stderr)
        return 1

    if not keys:
        print("Usage: agentwire send-keys -s <session> <keys>...", file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print("  agentwire send-keys -s mysession Enter", file=sys.stderr)
        print("  agentwire send-keys -s mysession C-c", file=sys.stderr)
        print("  agentwire send-keys -s mysession Escape", file=sys.stderr)
        print("  agentwire send-keys -s mysession 'hello world' Enter", file=sys.stderr)
        return 1

    # Parse session@machine format
    session, machine_id = _parse_session_target(session_full)

    if machine_id:
        # Remote: SSH and run tmux commands
        machine = _get_machine_config(machine_id)
        if machine is None:
            print(f"Machine '{machine_id}' not found in machines.json", file=sys.stderr)
            return 1

        # Build remote command with pauses between keys
        quoted_session = shlex.quote(session)
        cmd_parts = []
        for i, key in enumerate(keys):
            cmd_parts.append(f"tmux send-keys -t {quoted_session} {shlex.quote(key)}")
            if i < len(keys) - 1:
                cmd_parts.append("sleep 0.1")

        cmd = " && ".join(cmd_parts)

        result = _run_remote(machine_id, cmd)
        if result.returncode != 0:
            print(f"Failed to send keys to {session_full}: {result.stderr}", file=sys.stderr)
            return 1

        print(f"Sent keys to {session_full}")
        return 0

    # Local: existing logic
    # Check if session exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    if result.returncode != 0:
        print(f"Session '{session}' not found", file=sys.stderr)
        return 1

    # Send each key group with a pause between
    for i, key in enumerate(keys):
        subprocess.run(
            ["tmux", "send-keys", "-t", session, key],
            check=True
        )
        # Brief pause between key groups (not after last one)
        if i < len(keys) - 1:
            time.sleep(0.1)

    print(f"Sent keys to {session}")
    return 0


# === Wave 5: Recreate and Fork Commands ===


def cmd_recreate(args) -> int:
    """Destroy and recreate a session with fresh worktree.

    Steps:
    1. Kill existing session (local or remote)
    2. Remove worktree
    3. Pull latest on main repo
    4. Create new worktree with timestamp branch
    5. Create new session

    Supports remote sessions with session@machine format.
    """
    session_full = args.session
    json_mode = getattr(args, 'json', False)

    if not session_full:
        return _output_result(False, json_mode, "Usage: agentwire recreate -s <session>")

    # Parse session name
    project, branch, machine_id = parse_session_name(session_full)

    # Load config
    config = load_config()
    projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser()
    worktrees_config = config.get("projects", {}).get("worktrees", {})
    worktree_suffix = worktrees_config.get("suffix", "-worktrees")

    # Build session name for tmux (preserve slashes, convert dots to underscores)
    if branch:
        session_name = f"{project}/{branch}".replace(".", "_")
    else:
        session_name = project.replace(".", "_")

    if machine_id:
        # Remote recreate
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        remote_projects_dir = machine.get("projects_dir", "~/projects")

        # Step 1: Kill existing session
        kill_cmd = f"tmux send-keys -t {shlex.quote(session_name)} /exit Enter 2>/dev/null; sleep 2; tmux kill-session -t {shlex.quote(session_name)} 2>/dev/null"
        _run_remote(machine_id, kill_cmd)

        # Determine paths
        project_path = f"{remote_projects_dir}/{project}"
        if branch:
            worktree_path = f"{remote_projects_dir}/{project}{worktree_suffix}/{branch}"
        else:
            worktree_path = project_path

        # Step 2: Remove worktree (if branch session)
        if branch:
            remove_cmd = f"cd {shlex.quote(project_path)} && git worktree remove {shlex.quote(worktree_path)} --force 2>/dev/null; rm -rf {shlex.quote(worktree_path)}"
            _run_remote(machine_id, remove_cmd)

        # Step 3: Pull latest on main repo
        pull_cmd = f"cd {shlex.quote(project_path)} && git pull origin main 2>/dev/null || git pull origin master 2>/dev/null || true"
        _run_remote(machine_id, pull_cmd)

        # Step 4: Create new worktree with timestamp branch
        if branch:
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            new_branch = f"{branch}-{timestamp}"

            create_wt_cmd = f"cd {shlex.quote(project_path)} && mkdir -p $(dirname {shlex.quote(worktree_path)}) && git worktree add -b {shlex.quote(new_branch)} {shlex.quote(worktree_path)}"
            result = _run_remote(machine_id, create_wt_cmd)
            if result.returncode != 0:
                return _output_result(False, json_mode, f"Failed to create worktree: {result.stderr}")

        # Step 5: Create new session
        session_path = worktree_path if branch else project_path

        # Determine session type from --type flag or detect default
        agent_type = detect_default_agent_type()
        type_arg = getattr(args, 'type', None)
        if type_arg:
            session_type_str = normalize_session_type(type_arg, agent_type)
        else:
            # Fall back to agent-bypass
            session_type_str = f"{agent_type}-bypass"

        # Build agent command using the standard function
        agent = build_agent_command(session_type_str)
        agent.env.update(parse_env_args(getattr(args, 'env', None)))
        agent_cmd = agent.command
        env_flags = _build_tmux_env_flags_shell(agent.env)

        create_cmd = (
            f"tmux new-session -d -s {shlex.quote(session_name)} -c {shlex.quote(session_path)} {env_flags}&& "
            f"tmux send-keys -t {shlex.quote(session_name)} 'cd {shlex.quote(session_path)}' Enter && "
            f"sleep 0.1 && "
            f"tmux send-keys -t {shlex.quote(session_name)} {shlex.quote(agent_cmd)} Enter"
        )

        result = _run_remote(machine_id, create_cmd)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create session: {result.stderr}")

        if json_mode:
            _output_json({
                "success": True,
                "session": session_name,
                "path": session_path,
                "branch": new_branch if branch else None,
                "machine": machine_id,
            })
        else:
            print(f"Recreated session '{session_name}' on {machine_id} in {session_path}")

        return 0

    # Local recreate
    # Step 1: Kill existing session
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={session_name}"],
        capture_output=True
    )
    if result.returncode == 0:
        subprocess.run(["tmux", "send-keys", "-t", session_name, "/exit", "Enter"])
        time.sleep(2)
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)

    # Determine paths
    project_path = projects_dir / project
    if branch:
        worktree_path = projects_dir / f"{project}{worktree_suffix}" / branch
    else:
        worktree_path = project_path

    # Step 2: Remove worktree (if branch session)
    if branch and worktree_path.exists():
        remove_worktree(project_path, worktree_path)
        # Force remove if git worktree remove failed
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Step 3: Pull latest on main repo
    if project_path.exists():
        subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=project_path,
            capture_output=True
        )

    # Step 4: Create new worktree with timestamp branch
    if branch:
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        new_branch = f"{branch}-{timestamp}"

        success = ensure_worktree(
            project_path,
            new_branch,
            worktree_path,
            auto_create_branch=True,
        )
        if not success:
            return _output_result(False, json_mode, f"Failed to create worktree for branch '{new_branch}'")

    session_path = worktree_path if branch else project_path

    if not session_path.exists():
        return _output_result(False, json_mode, f"Path does not exist: {session_path}")

    # Determine session type from CLI --type flag or existing config
    agent_type = detect_default_agent_type()
    type_arg = getattr(args, 'type', None)
    project_config = load_project_config(session_path)

    if type_arg:
        # CLI flag specified - use it directly and normalize (session-level only, not saved)
        session_type_str = normalize_session_type(type_arg, agent_type)
        roles = None
        if project_config and project_config.roles:
            roles, _ = load_roles(project_config.roles, session_path)
    elif project_config:
        # Use existing config
        session_type_str = normalize_session_type(project_config.type.value, agent_type)
        roles = None
        if project_config.roles:
            roles, _ = load_roles(project_config.roles, session_path)
    else:
        # Default to agent-bypass based on detected agent
        session_type_str = f"{agent_type}-bypass"
        roles = None

    # Build agent command
    agent = build_agent_command(session_type_str, roles)
    agent.env.update(parse_env_args(getattr(args, 'env', None)))

    agent_cmd = agent.command

    # Step 5: Create new session with env vars injected at creation time so
    # the initial shell sees them (post-hoc set-environment doesn't reach the
    # already-running shell — see _build_tmux_env_flags docstring).
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(session_path),
         *_build_tmux_env_flags(agent.env)],
        check=True
    )

    # Ensure agent starts in correct directory
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, f"cd {shlex.quote(str(session_path))}", "Enter"],
        check=True
    )
    time.sleep(0.1)

    # Start the agent with appropriate command
    if agent_cmd:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, agent_cmd, "Enter"],
            check=True
        )

    if json_mode:
        _output_json({
            "success": True,
            "session": session_name,
            "path": str(session_path),
            "branch": new_branch if branch else None,
            "machine": None,
        })
    else:
        print(f"Recreated session '{session_name}' in {session_path}")
        print(f"Attach with: tmux attach -t {session_name}")

    return 0


def cmd_worktree(args) -> int:
    """Create a git worktree + agentwire session in one command.

    Three modes:
    - Default: new branch from base. `agentwire worktree fix-bug`
    - --existing: checkout existing branch. `agentwire worktree feature/auth --existing`
    - --ref: detached at a ref. `agentwire worktree review-v2 --ref v2.0.0`

    If the worktree already exists, reattaches to the existing session.
    """
    json_mode = getattr(args, 'json', False)
    name = args.name
    use_existing = getattr(args, 'existing', False)
    ref = getattr(args, 'ref', None)

    if not _check_config_exists():
        return _output_result(False, json_mode, "AgentWire not configured. Run 'agentwire init'")
    if not _check_tmux_installed():
        return _output_result(False, json_mode, "tmux is required but not installed")

    config = load_config()
    wt_config = config.get("worktree", config.get("quicktask", {}))

    # Resolve project repo path
    project_arg = getattr(args, 'project', None)
    project_path = Path(
        project_arg or wt_config.get("default_project") or os.getcwd()
    ).expanduser().resolve()
    if not (project_path / ".git").exists():
        return _output_result(False, json_mode, f"Not a git repo: {project_path}")

    worktree_dir = Path(wt_config.get("worktree_dir", "~/worktrees")).expanduser().resolve()
    project_name = project_path.name
    session_name = f"{project_name}-{name}"
    worktree_path = worktree_dir / session_name

    def _launch_session():
        return cmd_new(type('Args', (), {
            'session': session_name, 'path': str(worktree_path), 'json': json_mode,
            'force': False, 'bare': False, 'restricted': False, 'prompted': False,
            'type': getattr(args, 'type', None), 'roles': getattr(args, 'roles', None),
            'instructions': None, 'persist': False,
            'env': getattr(args, 'env', None),
        })())

    # If worktree already exists, reattach
    if worktree_path.exists():
        if json_mode:
            _output_json({"success": True, "session": session_name, "path": str(worktree_path), "reattached": True})
        else:
            print(f"Worktree exists: {worktree_path}")
        if tmux_session_exists(session_name):
            subprocess.run(["tmux", "attach-session", "-t", session_name])
        else:
            return _launch_session()
        return 0

    # Resolve the git ref to create the worktree from
    if ref:
        # --ref mode: detached at a specific ref (tag, commit, branch)
        git_ref = ref
        new_branch = None
        mode_label = f"detached at {ref}"
    elif use_existing:
        # --existing mode: checkout an existing branch
        git_ref = name
        new_branch = None
        mode_label = f"existing branch {name}"
    else:
        # Default mode: new branch from base
        if getattr(args, 'current', False):
            result = subprocess.run(
                ["git", "-C", str(project_path), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return _output_result(False, json_mode, f"Failed to detect current branch in {project_path}")
            base_branch = result.stdout.strip()
        else:
            base_branch = getattr(args, 'base', None) or wt_config.get("default_base", "main")
        git_ref = f"origin/{base_branch}"
        new_branch = name
        mode_label = f"new branch {name} from {git_ref}"

        # Fetch the base branch
        if not json_mode:
            print(f"Fetching origin/{base_branch}...")
        fetch_result = subprocess.run(
            ["git", "-C", str(project_path), "fetch", "origin", base_branch, "--quiet"],
            capture_output=True, text=True,
        )
        if fetch_result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to fetch origin/{base_branch}: {fetch_result.stderr.strip()}")

    # Create worktree
    worktree_dir.mkdir(parents=True, exist_ok=True)
    wt_cmd = ["git", "-C", str(project_path), "worktree", "add"]
    if new_branch:
        wt_cmd += [str(worktree_path), git_ref, "--detach", "--quiet"]
    elif use_existing:
        wt_cmd += [str(worktree_path), git_ref, "--quiet"]
    else:
        wt_cmd += [str(worktree_path), git_ref, "--detach", "--quiet"]

    wt_result = subprocess.run(wt_cmd, capture_output=True, text=True)
    if wt_result.returncode != 0:
        return _output_result(False, json_mode, f"Failed to create worktree: {wt_result.stderr.strip()}")

    # Create new branch if in default mode
    if new_branch:
        branch_result = subprocess.run(
            ["git", "-C", str(worktree_path), "checkout", "-b", new_branch, "--quiet"],
            capture_output=True, text=True,
        )
        if branch_result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create branch '{new_branch}': {branch_result.stderr.strip()}")

    if json_mode:
        _output_json({
            "success": True, "session": session_name, "path": str(worktree_path),
            "branch": new_branch, "ref": git_ref, "reattached": False,
        })
    else:
        print(f"Created worktree: {worktree_path}")
        print(f"Mode: {mode_label}")

    return _launch_session()


def cmd_fork(args) -> int:
    """Fork a session into a new worktree with copied Claude context.

    Creates a new worktree from current branch state and optionally
    copies Claude session file for conversation continuity.

    Supports remote sessions with session@machine format.
    """
    source_full = args.source
    target_full = args.target
    json_mode = getattr(args, 'json', False)

    if not source_full or not target_full:
        return _output_result(False, json_mode, "Usage: agentwire fork -s <source> -t <target>")

    # Parse session names
    source_project, source_branch, source_machine = parse_session_name(source_full)
    target_project, target_branch, target_machine = parse_session_name(target_full)

    # Non-worktree fork: both have no branch (same directory, fork Claude context)
    # Worktree fork: at least one has a branch (create new worktree)
    is_non_worktree_fork = not source_branch and not target_branch

    # For worktree forks, validate project names match and target has a branch
    if not is_non_worktree_fork:
        if source_project != target_project:
            return _output_result(False, json_mode, f"For worktree forks, source and target must be same project (got {source_project} vs {target_project})")
        if not target_branch:
            return _output_result(False, json_mode, "For worktree forks, target must include a branch name (e.g., project/new-branch)")

    # Machines must match
    if source_machine != target_machine:
        return _output_result(False, json_mode, f"Source and target must be on same machine (got {source_machine} vs {target_machine})")

    machine_id = source_machine

    # Load config
    config = load_config()
    projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser()
    worktrees_config = config.get("projects", {}).get("worktrees", {})
    worktree_suffix = worktrees_config.get("suffix", "-worktrees")

    # Build session names (preserve slashes, convert dots to underscores)
    if source_branch:
        source_session = f"{source_project}/{source_branch}".replace(".", "_")
    else:
        source_session = source_project.replace(".", "_")

    if target_branch:
        target_session = f"{target_project}/{target_branch}".replace(".", "_")
    else:
        # Non-worktree fork: use target project name directly
        target_session = target_project.replace(".", "_")

    if machine_id:
        # Remote fork
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        remote_projects_dir = machine.get("projects_dir", "~/projects")

        # Build paths
        project_path = f"{remote_projects_dir}/{source_project}"
        if source_branch:
            source_path = f"{remote_projects_dir}/{source_project}{worktree_suffix}/{source_branch}"
        else:
            source_path = project_path
        target_path = f"{remote_projects_dir}/{target_project}{worktree_suffix}/{target_branch}"

        # Check if target already exists
        check_cmd = f"test -d {shlex.quote(target_path)}"
        result = _run_remote(machine_id, check_cmd)
        if result.returncode == 0:
            return _output_result(False, json_mode, f"Target worktree already exists: {target_path}")

        # Create new worktree from source
        create_cmd = f"cd {shlex.quote(source_path)} && mkdir -p $(dirname {shlex.quote(target_path)}) && git worktree add -b {shlex.quote(target_branch)} {shlex.quote(target_path)}"
        result = _run_remote(machine_id, create_cmd)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create worktree: {result.stderr}")

        # Determine session type from --type flag or source config
        agent_type = detect_default_agent_type()
        type_arg = getattr(args, 'type', None)
        source_config = load_project_config(Path(source_path))

        if type_arg:
            # CLI flag specified - use it directly
            session_type_str = normalize_session_type(type_arg, agent_type)
            roles = None
            if source_config and source_config.roles:
                roles, _ = load_roles(source_config.roles, Path(source_path))
        elif source_config:
            # Use source config
            session_type_str = normalize_session_type(source_config.type.value, agent_type)
            roles = None
            if source_config.roles:
                roles, _ = load_roles(source_config.roles, Path(source_path))
        else:
            # Default to agent-bypass based on detected agent
            session_type_str = f"{agent_type}-bypass"
            roles = None

        # Build agent command
        agent = build_agent_command(session_type_str, roles)
        agent.env.update(parse_env_args(getattr(args, 'env', None)))

        agent_cmd = agent.command
        env_flags = _build_tmux_env_flags_shell(agent.env)

        create_session_cmd = (
            f"tmux new-session -d -s {shlex.quote(target_session)} -c {shlex.quote(target_path)} {env_flags}&& "
            f"tmux send-keys -t {shlex.quote(target_session)} 'cd {shlex.quote(target_path)}' Enter && "
            f"sleep 0.1 && "
            f"tmux send-keys -t {shlex.quote(target_session)} {shlex.quote(agent_cmd)} Enter"
        )

        result = _run_remote(machine_id, create_session_cmd)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create session: {result.stderr}")

        if json_mode:
            _output_json({
                "success": True,
                "session": f"{target_session}@{machine_id}",
                "path": target_path,
                "branch": target_branch,
                "machine": machine_id,
                "forked_from": source_full,
            })
        else:
            print(f"Forked '{source_full}' to '{target_session}' on {machine_id}")
            print(f"  Path: {target_path}")

        return 0

    # Local fork
    # Build paths
    project_path = projects_dir / source_project

    # Handle non-worktree fork (same directory, different Claude session)
    if is_non_worktree_fork:
        # Check if source session exists
        check_source = subprocess.run(
            ["tmux", "has-session", "-t", source_session],
            capture_output=True
        )
        if check_source.returncode != 0:
            return _output_result(False, json_mode, f"Source session '{source_session}' does not exist")

        # For non-worktree forks, use the source session's actual CWD.
        # The session name may not match the project directory name (e.g., a session
        # named "aw-context-source" running in ~/projects/aw-feature-test/).
        cwd_result = subprocess.run(
            ["tmux", "display-message", "-t", source_session, "-p", "#{pane_current_path}"],
            capture_output=True, text=True,
        )
        if cwd_result.returncode == 0 and cwd_result.stdout.strip():
            fork_path = Path(cwd_result.stdout.strip())
        else:
            fork_path = project_path

        if not fork_path.exists():
            return _output_result(False, json_mode, f"Source path does not exist: {fork_path}")

        # Check if target session already exists
        check_target = subprocess.run(
            ["tmux", "has-session", "-t", target_session],
            capture_output=True
        )
        if check_target.returncode == 0:
            return _output_result(False, json_mode, f"Target session '{target_session}' already exists")

        # Determine session type from --type flag or source config (before
        # session creation, so we can inject env via `tmux new-session -e K=V`).
        agent_type = detect_default_agent_type()
        type_arg = getattr(args, 'type', None)
        source_project_config = load_project_config(fork_path)

        if type_arg:
            # CLI flag specified - use it directly
            session_type_str = normalize_session_type(type_arg, agent_type)
            roles = None
            if source_project_config and source_project_config.roles:
                roles, _ = load_roles(source_project_config.roles, fork_path)
        elif source_project_config:
            # Use source config
            session_type_str = normalize_session_type(source_project_config.type.value, agent_type)
            roles = None
            if source_project_config.roles:
                roles, _ = load_roles(source_project_config.roles, fork_path)
        else:
            # Default to agent-bypass based on detected agent
            session_type_str = f"{agent_type}-bypass"
            roles = None

        # Find the conversation JSONL for the source session to enable context inheritance.
        # Uses history.jsonl filtered by the source tmux session's creation time to identify
        # the correct JSONL even when multiple sessions share the same project directory.
        import json as _json
        resume_session_id = None

        # Get source session creation timestamp
        created_result = subprocess.run(
            ["tmux", "display-message", "-t", source_session, "-p", "#{session_created}"],
            capture_output=True, text=True,
        )
        session_created_unix = int(created_result.stdout.strip() or 0) if created_result.returncode == 0 else 0
        session_created_ms = session_created_unix * 1000

        history_file = Path.home() / ".claude" / "history.jsonl"
        if session_created_ms > 0 and history_file.exists():
            # Find sessions for this project that started AFTER the source tmux session was created.
            # The session whose first message is closest to (and after) creation time is the source.
            first_seen: dict[str, int] = {}
            for line in history_file.read_text().strip().splitlines():
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                    if entry.get("project") != str(fork_path):
                        continue
                    sid = entry.get("sessionId", "")
                    ts = entry.get("timestamp", 0)
                    if sid and (sid not in first_seen or ts < first_seen[sid]):
                        first_seen[sid] = ts
                except _json.JSONDecodeError:
                    continue

            # Pick the session with earliest first_seen >= session_created_ms
            candidates = [(sid, ts) for sid, ts in first_seen.items() if ts >= session_created_ms]
            if candidates:
                candidates.sort(key=lambda x: x[1])
                resume_session_id = candidates[0][0]

        if not resume_session_id:
            # Fallback: most recently modified JSONL in the project dir
            claude_projects_dir = Path.home() / ".claude" / "projects"
            from .history import encode_project_path as _encode_project_path
            encoded_path = _encode_project_path(str(fork_path))
            session_dir = claude_projects_dir / encoded_path
            if session_dir.exists():
                jsonl_files = sorted(session_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
                if jsonl_files:
                    resume_session_id = jsonl_files[0].stem

        # Build agent command
        agent = build_agent_command(session_type_str, roles)
        agent.env.update(parse_env_args(getattr(args, 'env', None)))

        # Create new tmux session with env injected at creation time so the
        # initial shell sees the vars (see _build_tmux_env_flags docstring).
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", target_session, "-c", str(fork_path),
             *_build_tmux_env_flags(agent.env)],
            check=True
        )

        # Ensure Claude starts in correct directory
        subprocess.run(
            ["tmux", "send-keys", "-t", target_session, f"cd {shlex.quote(str(fork_path))}", "Enter"],
            check=True
        )
        time.sleep(0.1)

        agent_cmd = agent.command
        if agent_cmd:
            # Inject --resume <id> --fork-session right after the 'claude' binary
            # so the forked session starts with the source conversation in context
            if resume_session_id:
                claude_pos = agent_cmd.rfind("claude")
                if claude_pos >= 0:
                    insert_pos = claude_pos + len("claude")
                    agent_cmd = (
                        agent_cmd[:insert_pos]
                        + f" --resume {resume_session_id} --fork-session"
                        + agent_cmd[insert_pos:]
                    )
            subprocess.run(
                ["tmux", "send-keys", "-t", target_session, agent_cmd, "Enter"],
                check=True
            )

        if json_mode:
            _output_json({
                "success": True,
                "session": target_session,
                "path": str(fork_path),
                "branch": None,
                "machine": None,
                "forked_from": source_full,
                "resumed_from": resume_session_id,
            })
        else:
            print(f"Forked '{source_full}' to '{target_session}' (same directory)")
            print(f"  Path: {fork_path}")
            if resume_session_id:
                print(f"  Resumed from: {resume_session_id}")

        return 0

    # Worktree fork logic
    if source_branch:
        source_path = projects_dir / f"{source_project}{worktree_suffix}" / source_branch
    else:
        source_path = project_path
    target_path = projects_dir / f"{target_project}{worktree_suffix}" / target_branch

    # Check if target already exists
    if target_path.exists():
        return _output_result(False, json_mode, f"Target worktree already exists: {target_path}")

    # Check source exists
    if not source_path.exists():
        return _output_result(False, json_mode, f"Source path does not exist: {source_path}")

    # Create new worktree from source
    fork_commit = getattr(args, "commit", None) or None
    success = ensure_worktree(
        source_path,  # Use source as base for the worktree
        target_branch,
        target_path,
        auto_create_branch=True,
        commit=fork_commit,
    )
    if not success:
        # Try from project path instead
        if project_path.exists():
            success = ensure_worktree(
                project_path,
                target_branch,
                target_path,
                auto_create_branch=True,
                commit=fork_commit,
            )

    if not success:
        return _output_result(False, json_mode, f"Failed to create worktree for branch '{target_branch}'")

    # Determine session type from --type flag or source config (before
    # session creation, so we can inject env via `tmux new-session -e K=V`).
    agent_type = detect_default_agent_type()
    type_arg = getattr(args, 'type', None)
    config_path = source_path if source_path != project_path else project_path
    source_config = load_project_config(config_path)

    if type_arg:
        # CLI flag specified - use it directly
        session_type_str = normalize_session_type(type_arg, agent_type)
        roles = None
        if source_config and source_config.roles:
            roles, _ = load_roles(source_config.roles, config_path)
    elif source_config:
        # Use source config
        session_type_str = normalize_session_type(source_config.type.value, agent_type)
        roles = None
        if source_config.roles:
            roles, _ = load_roles(source_config.roles, config_path)
    else:
        # Default to agent-bypass based on detected agent
        session_type_str = f"{agent_type}-bypass"
        roles = None

    # Build agent command
    agent = build_agent_command(session_type_str, roles)
    agent.env.update(parse_env_args(getattr(args, 'env', None)))
    agent_cmd = agent.command

    # Create new session with env injected at creation time so the initial
    # shell sees the vars (see _build_tmux_env_flags docstring).
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", target_session, "-c", str(target_path),
         *_build_tmux_env_flags(agent.env)],
        check=True
    )

    # Ensure agent starts in correct directory
    subprocess.run(
        ["tmux", "send-keys", "-t", target_session, f"cd {shlex.quote(str(target_path))}", "Enter"],
        check=True
    )
    time.sleep(0.1)

    if agent_cmd:
        subprocess.run(
            ["tmux", "send-keys", "-t", target_session, agent_cmd, "Enter"],
            check=True
        )

    if json_mode:
        _output_json({
            "success": True,
            "session": target_session,
            "path": str(target_path),
            "branch": target_branch,
            "machine": None,
            "forked_from": source_full,
        })
    else:
        print(f"Forked '{source_full}' to '{target_session}'")
        print(f"  Path: {target_path}")
        print(f"Attach with: tmux attach -t {target_session}")

    return 0


# === History Commands ===

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


def cmd_history_list(args) -> int:
    """List conversation history for a project."""
    from .history import get_history

    # Determine project path
    if args.project:
        project_path = Path(args.project).resolve()
        if not project_path.exists():
            print(f"Project path not found: {project_path}", file=sys.stderr)
            return 1
    else:
        # Check if cwd is a tracked project
        config = load_project_config()
        if config is None:
            print("Not in a tracked project directory.", file=sys.stderr)
            print("Use --project <path> or run from a directory with .agentwire.yml", file=sys.stderr)
            return 1
        project_path = Path.cwd().resolve()

    # Get history
    sessions = get_history(
        project_path=str(project_path),
        machine=args.machine,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0

    if not sessions:
        print(f"No history found for {project_path}")
        return 0

    print(f"Session history for {project_path.name} ({len(sessions)} sessions):")
    print()

    for session in sessions:
        session_id = session.get("sessionId", "")
        short_id = session_id[:8] if session_id else "?"
        timestamp = session.get("timestamp", 0)
        relative_time = format_relative_time(timestamp) if timestamp else "unknown"
        message_count = session.get("messageCount", 0)
        last_summary = session.get("lastSummary") or session.get("firstMessage", "")

        # Truncate summary for display
        if last_summary and len(last_summary) > 60:
            last_summary = last_summary[:57] + "..."

        print(f"  {short_id}  {relative_time:>15}  ({message_count} msgs)")
        if last_summary:
            print(f"           {last_summary}")
        print()

    return 0


def cmd_history_show(args) -> int:
    """Show details for a specific session."""
    from .history import get_session_detail

    session_id = args.session_id

    # Get session details
    detail = get_session_detail(
        session_id=session_id,
        machine=args.machine,
    )

    if detail is None:
        print(f"Session not found: {session_id}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(detail, indent=2))
        return 0

    # Display formatted output
    full_id = detail.get("sessionId", "?")
    message_count = detail.get("messageCount", 0)
    git_branch = detail.get("gitBranch")
    first_message = detail.get("firstMessage", "")
    summaries = detail.get("summaries", [])
    timestamps = detail.get("timestamps", {})

    start_ts = timestamps.get("start")
    end_ts = timestamps.get("end")

    print(f"Session: {full_id}")
    print()

    if start_ts:
        from datetime import datetime
        start_dt = datetime.fromtimestamp(start_ts / 1000)
        print(f"  Started:  {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    if end_ts:
        from datetime import datetime
        end_dt = datetime.fromtimestamp(end_ts / 1000)
        print(f"  Last msg: {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    print(f"  Messages: {message_count}")

    if git_branch:
        print(f"  Branch:   {git_branch}")

    print()

    if first_message:
        # Truncate for display
        preview = first_message[:200] + "..." if len(first_message) > 200 else first_message
        print("First message:")
        print(f"  {preview}")
        print()

    if summaries:
        print(f"Summaries ({len(summaries)}):")
        for i, summary in enumerate(summaries, 1):
            # Truncate each summary
            if len(summary) > 100:
                summary = summary[:97] + "..."
            print(f"  {i}. {summary}")
        print()

    return 0


def cmd_history_resume(args) -> int:
    """Resume a Claude Code session.

    Creates a new tmux session and runs: `claude --resume <session-id> --fork-session`

    Flags are applied based on the project's .agentwire.yml config.
    """
    session_id = args.session_id
    name = getattr(args, 'name', None)
    machine_id = getattr(args, 'machine', 'local')
    project_path_str = args.project
    json_mode = getattr(args, 'json', False)

    agent_type = "claude"
    # Resolve prefix to full UUID for Claude Code sessions
    from .history import resolve_session_id
    resolved = resolve_session_id(session_id, machine_id)
    if resolved:
        session_id = resolved

    # Resolve project path
    project_path = Path(project_path_str).expanduser().resolve()

    # Load project config for type and roles
    project_config = load_project_config(project_path)
    if project_config is None:
        project_config = ProjectConfig(type=SessionType.CLAUDE_BYPASS, roles=[])

    # Generate session name if not provided
    if not name:
        base_name = project_path.name.replace(".", "_")
        # Find unique name with -fork-N suffix
        name = f"{base_name}-fork-1"
        counter = 1
        while True:
            # Check if session exists locally
            check_result = subprocess.run(
                ["tmux", "has-session", "-t", f"={name}"],
                capture_output=True
            )
            if check_result.returncode != 0:
                break  # Session doesn't exist, use this name
            counter += 1
            name = f"{base_name}-fork-{counter}"

    # Build resume command
    temp_file = None
    cmd_parts = ["claude", "--resume", session_id, "--fork-session"]
    cmd_parts.extend(project_config.type.to_cli_flags())

    # Load and apply roles if specified in config
    if project_config.roles:
        roles, missing = load_roles(project_config.roles, project_path)
        if not missing and roles:
            merged = merge_roles(roles)
            if merged.tools:
                cmd_parts.append("--tools")
                cmd_parts.extend(sorted(merged.tools))
            if merged.disallowed_tools:
                cmd_parts.append("--disallowedTools")
                cmd_parts.extend(sorted(merged.disallowed_tools))
            if merged.instructions:
                # Write to temp file to avoid shell escaping issues
                f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
                f.write(merged.instructions)
                f.close()
                temp_file = f.name
                cmd_parts.append(f'--append-system-prompt "$(<{temp_file})"')

    agent_cmd = " ".join(cmd_parts)

    if machine_id and machine_id != "local":
        # Remote machine
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        remote_path = str(project_path)

        # Check if session already exists on remote
        check_cmd = f"tmux has-session -t ={shlex.quote(name)} 2>/dev/null"
        result = _run_remote(machine_id, check_cmd)
        if result.returncode == 0:
            return _output_result(False, json_mode, f"Session '{name}' already exists on {machine_id}")

        # Create remote tmux session and send claude command
        create_cmd = (
            f"tmux new-session -d -s {shlex.quote(name)} -c {shlex.quote(remote_path)} && "
            f"tmux send-keys -t {shlex.quote(name)} 'cd {shlex.quote(remote_path)}' Enter && "
            f"sleep 0.1 && "
            f"tmux send-keys -t {shlex.quote(name)} {shlex.quote(agent_cmd)} Enter"
        )

        result = _run_remote(machine_id, create_cmd)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create remote session: {result.stderr}")

        if json_mode:
            _output_json({
                "success": True,
                "session": f"{name}@{machine_id}",
                "resumed_from": session_id,
                "path": remote_path,
                "machine": machine_id,
                "type": project_config.type.value,
            })
        else:
            host = machine.get('host', machine_id)
            print(f"Resumed session '{name}' on {machine_id} (forked from {session_id})")
            print(f"Attach via portal or: ssh {host} -t tmux attach -t {name}")

        _notify_portal_sessions_changed()
        return 0

    # Local session
    if not project_path.exists():
        return _output_result(False, json_mode, f"Project path does not exist: {project_path}")

    # Check if session already exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"],
        capture_output=True
    )
    if result.returncode == 0:
        return _output_result(False, json_mode, f"Session '{name}' already exists. Choose a different name with --name.")

    # Create new tmux session
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(project_path)],
        check=True
    )

    # Ensure Claude starts in correct directory
    subprocess.run(
        ["tmux", "send-keys", "-t", name, f"cd {shlex.quote(str(project_path))}", "Enter"],
        check=True
    )
    time.sleep(0.1)

    # Send the claude resume command
    subprocess.run(
        ["tmux", "send-keys", "-t", name, agent_cmd, "Enter"],
        check=True
    )

    if json_mode:
        _output_json({
            "success": True,
            "session": name,
            "resumed_from": session_id,
            "path": str(project_path),
            "machine": None,
            "type": project_config.type.value,
        })
    else:
        print(f"Resumed session '{name}' (forked from {session_id})")
        print(f"Project: {project_path}")
        print(f"Attach with: tmux attach -t {name}")

    _notify_portal_sessions_changed()
    return 0


# === Machine Commands ===

def cmd_machine_add(args) -> int:
    """Add a machine to the AgentWire network."""
    machine_id = args.machine_id
    host = args.host or machine_id  # Default host to id if not specified
    user = args.user
    projects_dir = args.projects_dir

    machines_file = CONFIG_DIR / "machines.json"
    machines_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing machines
    machines = []
    if machines_file.exists():
        try:
            with open(machines_file) as f:
                machines = json.load(f).get("machines", [])
        except (json.JSONDecodeError, IOError):
            pass

    # Check for duplicate ID
    if any(m.get("id") == machine_id for m in machines):
        print(f"Machine '{machine_id}' already exists", file=sys.stderr)
        return 1

    # Build machine entry
    new_machine = {"id": machine_id, "host": host}
    if user:
        new_machine["user"] = user
    if projects_dir:
        new_machine["projects_dir"] = projects_dir

    machines.append(new_machine)

    # Save
    with open(machines_file, "w") as f:
        json.dump({"machines": machines}, f, indent=2)
        f.write("\n")

    print(f"Added machine '{machine_id}'")
    print(f"  Host: {host}")
    if user:
        print(f"  User: {user}")
    if projects_dir:
        print(f"  Projects: {projects_dir}")
    print()
    print("Next steps:")
    print("  1. Ensure SSH access: ssh", f"{user}@{host}" if user else host)
    print("  2. Start tunnel: autossh -M 0 -f -N -R 8765:localhost:8765", machine_id)
    print("  3. Restart portal: agentwire portal stop && agentwire portal start")
    print()
    print("For full setup guide, run: /machine-setup in a Claude session")

    return 0


def cmd_machine_remove(args) -> int:
    """Remove a machine from the AgentWire network."""
    machine_id = args.machine_id

    machines_file = CONFIG_DIR / "machines.json"

    # Step 1: Load and check machines.json
    if not machines_file.exists():
        print(f"No machines.json found at {machines_file}", file=sys.stderr)
        return 1

    try:
        with open(machines_file) as f:
            machines_data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Invalid machines.json: {e}", file=sys.stderr)
        return 1

    machines = machines_data.get("machines", [])
    machine = next((m for m in machines if m.get("id") == machine_id), None)

    if not machine:
        print(f"Machine '{machine_id}' not found in machines.json", file=sys.stderr)
        print(f"Available machines: {', '.join(m.get('id', '?') for m in machines)}")
        return 1

    host = machine.get("host", machine_id)

    print(f"Removing machine '{machine_id}' (host: {host})...")
    print()

    # Step 2: Kill autossh tunnel
    print("Stopping tunnel...")
    result = subprocess.run(
        ["pkill", "-f", f"autossh.*{machine_id}"],
        capture_output=True,
    )
    if result.returncode == 0:
        print(f"  ✓ Killed autossh tunnel for {machine_id}")
    else:
        # Also try by host if different from id
        if host != machine_id:
            result = subprocess.run(
                ["pkill", "-f", f"autossh.*{host}"],
                capture_output=True,
            )
            if result.returncode == 0:
                print(f"  ✓ Killed autossh tunnel for {host}")
            else:
                print("  - No tunnel running (or already stopped)")
        else:
            print("  - No tunnel running (or already stopped)")

    # Step 3: Remove from machines.json
    print("Updating machines.json...")
    machines_data["machines"] = [m for m in machines if m.get("id") != machine_id]
    with open(machines_file, "w") as f:
        json.dump(machines_data, f, indent=2)
        f.write("\n")
    print(f"  ✓ Removed '{machine_id}' from machines.json")

    # Step 4: Print manual steps
    print()
    print("=" * 50)
    print("MANUAL STEPS REQUIRED:")
    print("=" * 50)
    print()
    print("1. Remove SSH config entry:")
    print(f"   Edit ~/.ssh/config and remove the 'Host {machine_id}' block")
    print()
    print("2. Remove from tunnel startup script (if using):")
    print("   Edit ~/.local/bin/agentwire-tunnels")
    print(f"   Remove '{machine_id}' from the MACHINES list")
    print()
    print("3. Delete GitHub deploy keys:")
    print("   gh repo deploy-key list --repo <user>/<repo>")
    print(f"   # Find keys titled '{machine_id}' and delete them:")
    print("   gh repo deploy-key delete <key-id> --repo <user>/<repo>")
    print()
    print("4. Destroy remote machine:")
    print("   Option A: Delete user only")
    print("     ssh root@<ip> 'pkill -u agentwire; userdel -r agentwire'")
    print("   Option B: Destroy the VM entirely via provider console")
    print()
    print("5. Restart portal to pick up changes:")
    print("   agentwire portal stop && agentwire portal start")
    print()

    return 0


def cmd_machine_list(args) -> int:
    """List registered machines."""
    json_mode = getattr(args, 'json', False)
    machines_file = CONFIG_DIR / "machines.json"

    if not machines_file.exists():
        if json_mode:
            _output_json({"success": True, "machines": []})
        else:
            print("No machines registered.")
            print(f"  Config: {machines_file}")
        return 0

    try:
        with open(machines_file) as f:
            machines_data = json.load(f)
    except json.JSONDecodeError as e:
        if json_mode:
            _output_json({"success": False, "error": f"Invalid machines.json: {e}"})
        else:
            print(f"Invalid machines.json: {e}", file=sys.stderr)
        return 1

    machines = machines_data.get("machines", [])

    if not machines:
        if json_mode:
            _output_json({"success": True, "machines": []})
        else:
            print("No machines registered.")
        return 0

    # Enrich with tunnel status
    result_machines = []
    for m in machines:
        machine_id = m.get("id", "?")
        host = m.get("host", machine_id)
        user = m.get("user", "")
        projects_dir = m.get("projects_dir", "~")

        # Check if tunnel is running
        result = subprocess.run(
            ["pgrep", "-f", f"autossh.*{machine_id}"],
            capture_output=True,
        )
        has_tunnel = result.returncode == 0

        result_machines.append({
            "id": machine_id,
            "host": host,
            "user": user,
            "projects_dir": projects_dir,
            "status": "tunnel" if has_tunnel else "no tunnel",
        })

    if json_mode:
        _output_json({"success": True, "machines": result_machines})
    else:
        print(f"Registered machines ({len(machines)}):")
        print()
        for m in result_machines:
            tunnel_status = "✓ tunnel" if m["status"] == "tunnel" else "✗ no tunnel"
            print(f"  {m['id']}")
            print(f"    Host: {m['host']}")
            print(f"    Projects: {m['projects_dir']}")
            print(f"    Status: {tunnel_status}")
            print()

    return 0


# === Dev Command ===

def cmd_dev(args) -> int:
    """Start or attach to the AgentWire dev/agentwire session."""
    session_name = "agentwire"
    project_dir = get_source_dir()

    if tmux_session_exists(session_name):
        print(f"Dev session exists. Attaching to '{session_name}'...")
        subprocess.run(["tmux", "attach-session", "-t", session_name])
        return 0

    if not project_dir.exists():
        print(f"Project directory not found: {project_dir}", file=sys.stderr)
        return 1

    # Dev session uses agentwire role by default
    role_names = ["agentwire"]
    roles, missing = load_roles(role_names, project_dir)
    if missing:
        print(f"Warning: Roles not found: {', '.join(missing)}", file=sys.stderr)
        roles = None

    # Use bypass session type for dev session (full permissions)
    agent_type = detect_default_agent_type()
    session_type_str = f"{agent_type}-bypass"

    # Build agent command
    agent = build_agent_command(session_type_str, roles)

    agent_cmd = agent.command

    # Create session with env injected at creation time so the initial
    # shell sees the vars (see _build_tmux_env_flags docstring).
    print(f"Creating dev session '{session_name}' in {project_dir}...")
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name, "-c", str(project_dir),
        *_build_tmux_env_flags(agent.env),
    ])

    # Start agent with agentwire config
    if agent_cmd:
        wait_for_shell_prompt(session_name)
        subprocess.run([
            "tmux", "send-keys", "-t", session_name, agent_cmd, "Enter",
        ])

    print("Attaching... (Ctrl+B D to detach)")
    subprocess.run(["tmux", "attach-session", "-t", session_name])
    return 0


# === Init Command ===

def cmd_init(args) -> int:
    """Initialize AgentWire configuration with interactive wizard.

    Default behavior: Run full wizard with optional agentwire setup at the end.
    Quick mode (--quick): Run wizard only, skip agentwire setup prompt.
    """
    # Check Python version first
    if not check_python_version():
        return 1

    # Check for externally-managed environment (Ubuntu)
    if not check_pip_environment():
        print("Please set up a virtual environment before running init.")
        return 1

    from .onboarding import run_onboarding

    if args.quick:
        # Quick mode: run wizard but skip agentwire step
        # We do this by running onboarding and returning before agentwire prompt
        # The onboarding module handles this internally
        return run_onboarding(skip_session=True)

    # Default: run full wizard (ends with optional agentwire setup)
    return run_onboarding()


def cmd_generate_certs(args) -> int:
    """Generate SSL certificates."""
    return generate_certs()


# === Listen Commands ===

def cmd_listen_start(args) -> int:
    """Start voice recording."""
    from .listen import start_recording
    return start_recording()


def cmd_listen_stop(args) -> int:
    """Stop recording, transcribe, send to session or type at cursor."""
    from .listen import stop_recording
    session = args.session or "agentwire"
    type_at_cursor = getattr(args, 'type', False)
    return stop_recording(session, voice_prompt=not args.no_prompt, type_at_cursor=type_at_cursor)


def cmd_listen_cancel(args) -> int:
    """Cancel current recording."""
    from .listen import cancel_recording
    return cancel_recording()


def cmd_listen_toggle(args) -> int:
    """Toggle recording (start if not recording, stop if recording)."""
    from .listen import is_recording, start_recording, stop_recording
    session = args.session or "agentwire"
    if is_recording():
        return stop_recording(session, voice_prompt=not args.no_prompt)
    else:
        return start_recording()


# === Network Commands ===


def cmd_network_status(args) -> int:
    """Show complete network health at a glance."""
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health, test_ssh_connectivity

    ctx = NetworkContext.from_config()
    tm = TunnelManager()
    issues = []

    # Print header
    print("AgentWire Network Status")
    print("=" * 60)
    hostname = ctx.local_machine_id or socket.gethostname()
    print(f"\nYou are on: {hostname}")

    # Check machines (SSH connectivity)
    print("\nMachines")
    print("-" * 60)

    for machine_id, machine in ctx.machines.items():
        is_local = machine_id == ctx.local_machine_id
        host = machine.get("host", machine_id)
        user = machine.get("user")

        if is_local:
            print(f"  {machine_id:<16}(this machine)    [ok] reachable")
        else:
            latency = test_ssh_connectivity(host, user, timeout=5)
            if latency is not None:
                print(f"  {machine_id:<16}{host:<18}[ok] reachable (ssh: {latency}ms)")
            else:
                print(f"  {machine_id:<16}{host:<18}[!!] unreachable")
                issues.append({
                    "type": "machine_unreachable",
                    "machine": machine_id,
                    "host": host,
                })

    # Check services
    print("\nServices")
    print("-" * 60)

    # Check TTS backend - if using RunPod, skip local health check
    tts_config = load_config().get("tts", {})
    tts_backend = tts_config.get("backend", "chatterbox")

    for service_name in ["portal", "tts"]:
        # Skip TTS local check if using RunPod backend
        if service_name == "tts" and tts_backend == "runpod":
            print(f"  {'Tts':<16}{'RunPod API':<18}[ok] cloud backend")
            continue

        service_config = getattr(ctx.config.services, service_name, None)
        if service_config is None:
            continue

        if ctx.is_local(service_name):
            location = f"localhost:{service_config.port}"
            via = "(local)"
        else:
            machine = service_config.machine
            location = f"{machine}:{service_config.port}"
            via = "(via tunnel)"

        # Test the service health
        url = ctx.get_service_url(service_name)
        health_url = f"{url}{service_config.health_endpoint}"
        is_healthy, error = test_service_health(health_url, timeout=3)

        if is_healthy:
            print(f"  {service_name.capitalize():<16}{location:<18}[ok] running {via}")
        else:
            print(f"  {service_name.capitalize():<16}{location:<18}[!!] not responding")
            issues.append({
                "type": "service_down",
                "service": service_name,
                "location": location,
                "error": error,
            })

    # Check tunnels
    required_tunnels = ctx.get_required_tunnels()
    if required_tunnels:
        print("\nTunnels (this machine)")
        print("-" * 60)

        for spec in required_tunnels:
            status = tm.check_tunnel(spec)
            target = f"localhost:{spec.local_port}"

            if status.status == "up":
                print(f"  -> {spec.remote_machine:<12}{target:<18}[ok] up (PID {status.pid})")
            else:
                print(f"  -> {spec.remote_machine:<12}{target:<18}[!!] down")
                issues.append({
                    "type": "tunnel_down",
                    "spec": spec,
                    "error": status.error,
                })

    # Check for worker sessions
    print("\nWorker Sessions")
    print("-" * 60)

    # Local sessions
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        sessions = [s for s in result.stdout.strip().split("\n") if s and not s.startswith("agentwire")]
        if sessions:
            print(f"  {hostname:<16}{len(sessions)} sessions    {', '.join(sessions[:5])}")
            if len(sessions) > 5:
                print(f"  {'':<16}... and {len(sessions) - 5} more")
        else:
            print(f"  {hostname:<16}0 sessions")
    else:
        print(f"  {hostname:<16}(no tmux server)")

    # Remote sessions
    for machine_id, machine in ctx.machines.items():
        if machine_id == ctx.local_machine_id:
            continue

        result = _run_remote(machine_id, "tmux list-sessions -F '#{session_name}' 2>/dev/null")
        if result.returncode == 0 and result.stdout.strip():
            sessions = [s for s in result.stdout.strip().split("\n") if s]
            if sessions:
                print(f"  {machine_id:<16}{len(sessions)} sessions    {', '.join(sessions[:5])}")
                if len(sessions) > 5:
                    print(f"  {'':<16}... and {len(sessions) - 5} more")
        else:
            print(f"  {machine_id:<16}0 sessions")

    # Summary
    print()
    if not issues:
        print("Everything looks good!")
    else:
        print(f"Issues detected: {len(issues)}")
        print()
        for i, issue in enumerate(issues, 1):
            if issue["type"] == "machine_unreachable":
                print(f"  {i}. Machine '{issue['machine']}' unreachable")
                print(f"     Host: {issue['host']}")
                print()
                print("     To fix:")
                print(f"       Check SSH connectivity: ssh {issue['host']}")
                print("       Verify machine is running")
                print()

            elif issue["type"] == "service_down":
                print(f"  {i}. {issue['service'].capitalize()} not responding")
                print(f"     Location: {issue['location']}")
                if issue.get("error"):
                    print(f"     Error: {issue['error']}")
                print()
                print("     To fix:")
                if issue["service"] == "portal":
                    print("       agentwire portal start")
                elif issue["service"] == "tts":
                    print("       agentwire tts start")
                print("       agentwire tunnels check  # Verify tunnel health")
                print()

            elif issue["type"] == "tunnel_down":
                spec = issue["spec"]
                print(f"  {i}. Missing tunnel")
                print(f"     Required: localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port}")
                if issue.get("error"):
                    print(f"     Error: {issue['error']}")
                print()
                print("     To fix:")
                print("       agentwire tunnels up")
                print()

        print("-" * 60)
        print()
        print("Run: agentwire doctor    # Auto-fix common issues")

    return 0 if not issues else 1


def cmd_safety_check(args) -> int:
    """CLI command: agentwire safety check"""
    command = args.command
    verbose = getattr(args, 'verbose', False)
    return cli_safety.safety_check_cmd(command, verbose)


def cmd_safety_status(args) -> int:
    """CLI command: agentwire safety status"""
    return cli_safety.safety_status_cmd()


def cmd_safety_logs(args) -> int:
    """CLI command: agentwire safety logs"""
    tail = getattr(args, 'tail', None)
    session = getattr(args, 'session', None)
    today = getattr(args, 'today', False)
    pattern = getattr(args, 'pattern', None)
    return cli_safety.safety_logs_cmd(tail, session, today, pattern)


def cmd_safety_install(args) -> int:
    """CLI command: agentwire safety install"""
    return cli_safety.safety_install_cmd()


def cmd_safety_tooldefs_list(args) -> int:
    """CLI command: agentwire safety tooldefs list"""
    return cli_safety.safety_tooldefs_list_cmd()


def cmd_safety_tooldefs_show(args) -> int:
    """CLI command: agentwire safety tooldefs show <tool>"""
    return cli_safety.safety_tooldefs_show_cmd(args.tool)


def cmd_doctor(args) -> int:
    """Auto-diagnose and fix common issues."""
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health, test_ssh_connectivity
    from .validation import validate_config

    dry_run = getattr(args, 'dry_run', False)
    auto_confirm = getattr(args, 'yes', False)

    print("AgentWire Doctor")
    print("=" * 60)

    issues_found = 0
    issues_fixed = 0

    # 1. Check Python version
    print("\nChecking Python version...")
    py_version = sys.version_info
    version_str = f"{py_version.major}.{py_version.minor}.{py_version.micro}"
    if py_version >= (3, 10):
        print(f"  [ok] Python {version_str} (>=3.10 required)")
    else:
        print(f"  [!!] Python {version_str} (>=3.10 required)")
        print("     macOS: pyenv install 3.12.0 && pyenv global 3.12.0")
        print("     Ubuntu: sudo apt update && sudo apt install python3.12")
        issues_found += 1

    # 2. Check system dependencies
    print("\nChecking system dependencies...")

    # Check tmux (required)
    tmux_path = shutil.which("tmux")
    if tmux_path:
        print(f"  [ok] tmux: {tmux_path}")
    else:
        print("  [!!] tmux: not found (required)")
        print("     macOS: brew install tmux")
        print("     Ubuntu: sudo apt install tmux")
        issues_found += 1

    # Check ffmpeg (optional)
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        print(f"  [ok] ffmpeg: {ffmpeg_path}")
    else:
        print("  [..] ffmpeg: not found (optional, needed for voice input)")
        print("     macOS: brew install ffmpeg")
        print("     Ubuntu: sudo apt install ffmpeg")

    # Check Claude Code (optional)
    claude_path = shutil.which("claude")
    if claude_path:
        print(f"  [ok] claude: {claude_path}")
    else:
        print("  [..] claude: not found (optional, use --bare sessions or other agents)")
        print("     Install: https://github.com/anthropics/claude-code")

    # Check Pi coding agent (optional, for pi-* session types)
    pi_path = shutil.which("pi")
    if pi_path:
        try:
            # Pi prints --version to stderr, so merge with stdout
            result = subprocess.run(
                [pi_path, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            pi_version = (result.stdout + result.stderr).strip()
            print(f"  [ok] pi: {pi_path} (v{pi_version})")
        except Exception:
            print(f"  [ok] pi: {pi_path}")
    else:
        print("  [..] pi: not found (optional, required for pi-* session types)")
        print("     Install: npm install -g @mariozechner/pi-coding-agent")

    # 3. Check AgentWire scripts
    print("\nChecking AgentWire scripts...")

    say_path = shutil.which("say")
    if say_path:
        print(f"  [ok] say: {say_path}")
    else:
        print("  [..] say: not found (optional, use 'agentwire say' directly)")

    # 4. Check Claude Code hooks
    print("\nChecking Claude Code hooks...")

    permission_hook = CLAUDE_HOOKS_DIR / "agentwire-permission.sh"
    if permission_hook.exists():
        print(f"  [ok] Permission hook: {permission_hook}")
    else:
        print("  [..] Permission hook: not found (optional for prompted sessions)")
        print("     Run: agentwire hooks install")

    # Check Claude Code idle notification hook
    idle_hook = CLAUDE_HOOKS_DIR / "idle-handler.sh"
    if idle_hook.exists():
        print(f"  [ok] Idle notification hook: {idle_hook}")
    else:
        print("  [!!] Idle notification hook: not found (required for worker notifications)")
        print("     This hook enables output capture and auto-kill for Claude Code workers.")
        issues_found += 1

    # Check queue processor
    queue_processor = Path.home() / ".agentwire" / "queue-processor.sh"
    if queue_processor.exists():
        print(f"  [ok] Queue processor: {queue_processor}")
    else:
        print("  [!!] Queue processor: not found (required for notification queuing)")
        issues_found += 1

    # 5. Validate config
    print("\nChecking configuration...")
    try:
        from .config import load_config as load_config_typed
        config = load_config_typed()
        print("  [ok] Config file valid")
    except Exception as e:
        print(f"  [!!] Config file error: {e}")
        print("     Run: agentwire init")
        issues_found += 1
        return 1  # Can't proceed without valid config

    machines_file = config.machines.file
    warnings, errors = validate_config(config, machines_file)

    if not errors:
        print("  [ok] Machines.json valid")
    else:
        for err in errors:
            print(f"  [!!] {err.message}")
            issues_found += 1

    if not warnings:
        print("  [ok] All config checks passed")
    else:
        for warn in warnings:
            print(f"  [..] {warn.message}")

    # 6. Check SSH connectivity
    print("\nChecking SSH connectivity...")
    ctx = NetworkContext.from_config()

    for machine_id, machine in ctx.machines.items():
        if machine_id == ctx.local_machine_id:
            continue

        host = machine.get("host", machine_id)
        user = machine.get("user")

        latency = test_ssh_connectivity(host, user, timeout=5)
        if latency is not None:
            print(f"  [ok] {machine_id}: reachable ({latency}ms)")
        else:
            print(f"  [!!] {machine_id}: unreachable")
            issues_found += 1

    # 7. Check/create tunnels
    print("\nChecking tunnels...")
    tm = TunnelManager()
    required_tunnels = ctx.get_required_tunnels()

    if not required_tunnels:
        print("  [ok] No tunnels required (services are local)")
    else:
        for spec in required_tunnels:
            status = tm.check_tunnel(spec)

            if status.status == "up":
                print(f"  [ok] localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port} (PID {status.pid})")
            else:
                print(f"  [!!] Missing: localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port}")
                issues_found += 1

                if not dry_run:
                    if auto_confirm or _confirm("     Create tunnel?"):
                        print("     -> Creating tunnel...", end=" ", flush=True)
                        result = tm.create_tunnel(spec, ctx)
                        if result.status == "up":
                            print(f"[ok] created (PID {result.pid})")
                            issues_fixed += 1
                        else:
                            print(f"[!!] failed: {result.error}")
                else:
                    print("     -> Would create tunnel (dry-run)")

    # 8. Check services
    print("\nChecking services...")

    # Check TTS backend - if using RunPod, skip local health check
    tts_config = load_config().get("tts", {})
    tts_backend = tts_config.get("backend", "chatterbox")

    for service_name in ["portal", "tts"]:
        # Skip TTS local check if using RunPod backend
        if service_name == "tts" and tts_backend == "runpod":
            print("  [ok] Tts: using RunPod backend (no local service needed)")
            continue

        service_config = getattr(ctx.config.services, service_name, None)
        if service_config is None:
            continue

        url = ctx.get_service_url(service_name)
        health_url = f"{url}{service_config.health_endpoint}"
        is_healthy, error = test_service_health(health_url, timeout=3)

        if is_healthy:
            print(f"  [ok] {service_name.capitalize()}: responding on {url}")
        else:
            print(f"  [!!] {service_name.capitalize()}: not responding on {url}")
            if error:
                print(f"       Error: {error}")
            issues_found += 1

            # Only try to fix if service is local
            if ctx.is_local(service_name):
                if not dry_run:
                    if auto_confirm or _confirm(f"     Start {service_name}?"):
                        print(f"     -> Starting {service_name}...", end=" ", flush=True)

                        if service_name == "portal":
                            session_name = get_portal_session_name()
                            if tmux_session_exists(session_name):
                                print("[ok] already running in tmux")
                            else:
                                subprocess.run(
                                    ["tmux", "new-session", "-d", "-s", session_name],
                                    capture_output=True,
                                )
                                subprocess.run(
                                    ["tmux", "send-keys", "-t", session_name, "agentwire portal serve", "Enter"],
                                    capture_output=True,
                                )
                                print("[ok] started")
                                issues_fixed += 1

                        elif service_name == "tts":
                            session_name = get_tts_session_name()
                            if tmux_session_exists(session_name):
                                print("[ok] already running in tmux")
                            else:
                                subprocess.run(
                                    ["tmux", "new-session", "-d", "-s", session_name],
                                    capture_output=True,
                                )
                                subprocess.run(
                                    ["tmux", "send-keys", "-t", session_name, "agentwire tts serve", "Enter"],
                                    capture_output=True,
                                )
                                print("[ok] started")
                                issues_fixed += 1
                else:
                    print(f"     -> Would start {service_name} (dry-run)")
            else:
                print(f"     -> Service is remote, start it on {service_config.machine}")

    # 8b. Check Telegram bridge
    print("\nChecking Telegram bridge...")
    telegram_session = get_telegram_session_name()
    if tmux_session_exists(telegram_session):
        print(f"  [ok] Telegram bridge: running in tmux session '{telegram_session}'")
    else:
        print(f"  [--] Telegram bridge: not running")
    try:
        from .channels.telegram import check_telegram_bot
        healthy, info = check_telegram_bot()
        if healthy:
            print(f"  [ok] Telegram bot: {info}")
        else:
            print(f"  [!!] Telegram bot: {info}")
            issues_found += 1
    except Exception as e:
        print(f"  [--] Telegram bot: not configured ({e})")

    # 9. Validate remote machines
    print("\nChecking remote machines...")
    remote_machines = {mid: m for mid, m in ctx.machines.items() if mid != ctx.local_machine_id}

    if not remote_machines:
        print("  [ok] No remote machines configured")
    else:
        for machine_id, machine in remote_machines.items():
            host = machine.get("host", machine_id)
            user = machine.get("user")
            target = f"{user}@{host}" if user else host

            print(f"\n  {machine_id}:")

            # Check SSH connectivity (already done above, but include latency here)
            latency = test_ssh_connectivity(host, user, timeout=5)
            if latency is not None:
                print(f"    [ok] SSH connectivity ({latency}ms)")
            else:
                print("    [!!] SSH connectivity failed")
                print(f"         Fix: ssh {target}")
                issues_found += 1
                continue  # Can't check further if SSH fails

            # Check if agentwire is installed
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "agentwire --version"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    version = result.stdout.strip()
                    print(f"    [ok] agentwire installed ({version})")
                else:
                    print("    [!!] agentwire not installed")
                    print(f"         Fix: ssh {target} 'pip install agentwire-dev'")
                    issues_found += 1
                    continue
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [!!] agentwire not installed")
                print(f"         Fix: ssh {target} 'pip install agentwire-dev'")
                issues_found += 1
                continue

            # Check portal_url file
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "cat ~/.agentwire/portal_url"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    portal_url = result.stdout.strip()
                    print(f"    [ok] portal_url set ({portal_url})")
                else:
                    print("    [!!] portal_url not set")
                    print(f"         Fix: ssh {target} 'echo \"https://localhost:8765\" > ~/.agentwire/portal_url'")
                    issues_found += 1
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [!!] portal_url not set")
                print(f"         Fix: ssh {target} 'echo \"https://localhost:8765\" > ~/.agentwire/portal_url'")
                issues_found += 1

            # Test say command (optional)
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "which say"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    print("    [ok] say command available")
                else:
                    print("    [..] say: not found (optional, use 'agentwire say' directly)")
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [..] say: not found (optional, use 'agentwire say' directly)")

    # Summary
    print()
    print("-" * 60)
    if issues_found == 0:
        print("All checks passed!")
    elif issues_fixed == issues_found:
        print(f"All issues resolved! ({issues_fixed} fixed)")
    elif issues_fixed > 0:
        print(f"Fixed {issues_fixed} of {issues_found} issues")
    else:
        print(f"Found {issues_found} issues")

    return 0 if issues_found == issues_fixed else 1


def _confirm(prompt: str) -> bool:
    """Ask for user confirmation."""
    try:
        response = input(f"{prompt} [y/N] ").strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# === Voice Clone Commands ===

def cmd_voiceclone_start(args) -> int:
    """Start voice recording for cloning."""
    from .voiceclone import start_recording
    return start_recording()


def cmd_voiceclone_stop(args) -> int:
    """Stop recording and upload voice clone."""
    from .voiceclone import stop_recording
    return stop_recording(args.name)


def cmd_voiceclone_cancel(args) -> int:
    """Cancel current recording."""
    from .voiceclone import cancel_recording
    return cancel_recording()


def cmd_voiceclone_list(args) -> int:
    """List available voices."""
    json_mode = getattr(args, 'json', False)

    from .voiceclone import is_runpod_backend, list_voices_runpod, get_tts_url
    import requests

    if is_runpod_backend():
        success, result = list_voices_runpod()
        if success:
            voices = result
            if json_mode:
                # Normalize voice format
                voice_list = []
                for v in voices:
                    if isinstance(v, str):
                        voice_list.append({"name": v})
                    else:
                        voice_list.append(v)
                _output_json({"success": True, "voices": voice_list})
            else:
                if not voices:
                    print("No voices available")
                    return 0
                print(f"Available voices ({len(voices)}):")
                for v in sorted(voices):
                    if isinstance(v, str):
                        print(f"  {v}")
                    else:
                        name = v.get("name", "?")
                        duration = v.get("duration", "?")
                        print(f"  {name}: {duration}s")
            return 0
        else:
            if json_mode:
                _output_json({"success": False, "error": str(result)})
            else:
                print(f"Failed to list voices: {result}")
            return 1
    else:
        tts_url = get_tts_url()
        if not tts_url:
            if json_mode:
                _output_json({"success": False, "error": "tts.url not configured"})
            else:
                print("Error: tts.url not configured in config.yaml")
            return 1
        try:
            response = requests.get(f"{tts_url}/voices", timeout=10)
            if response.status_code == 200:
                data = response.json()
                voices = data.get("voices", data) if isinstance(data, dict) else data
                if json_mode:
                    _output_json({"success": True, "voices": voices or []})
                else:
                    if not voices:
                        print("No voices available")
                        return 0
                    print(f"Available voices ({len(voices)}):")
                    for v in sorted(voices, key=lambda x: x.get("name", "")):
                        name = v.get("name", "?")
                        duration = v.get("duration", "?")
                        print(f"  {name}: {duration}s")
                return 0
            else:
                if json_mode:
                    _output_json({"success": False, "error": f"HTTP {response.status_code}"})
                else:
                    print(f"Failed to list voices: {response.status_code}")
                return 1
        except requests.RequestException as e:
            if json_mode:
                _output_json({"success": False, "error": str(e)})
            else:
                print(f"Connection failed: {e}")
            return 1


def cmd_voiceclone_delete(args) -> int:
    """Delete a voice."""
    from .voiceclone import delete_voice
    return delete_voice(args.name)


# === Rebuild/Uninstall Commands ===

UV_CACHE_DIR = Path.home() / ".cache" / "uv"


def cmd_rebuild(args) -> int:
    """Rebuild: clear uv cache, uninstall, reinstall from source.

    This is the correct way to pick up source changes when developing.
    `uv tool install . --force` does NOT work - it uses cached wheels.
    """
    print("Rebuilding agentwire-dev...")
    print()

    # Step 1: Clear uv cache
    if UV_CACHE_DIR.exists():
        print(f"Clearing uv cache ({UV_CACHE_DIR})...")
        shutil.rmtree(UV_CACHE_DIR)
        print("  ✓ Cache cleared")
    else:
        print("  - No cache to clear")

    # Step 2: Uninstall
    print("Uninstalling agentwire-dev...")
    result = subprocess.run(
        ["uv", "tool", "uninstall", "agentwire-dev"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  ✓ Uninstalled")
    else:
        # Might not be installed, that's fine
        print("  - Not installed (continuing)")

    # Step 3: Reinstall from current directory
    # Find the project root (where pyproject.toml is)
    project_root = Path(__file__).parent.parent
    if not (project_root / "pyproject.toml").exists():
        # Fallback to configured source directory
        project_root = get_source_dir()

    print(f"Installing from {project_root}...")
    result = subprocess.run(
        ["uv", "tool", "install", "."],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ✗ Install failed: {result.stderr}", file=sys.stderr)
        return 1

    print("  ✓ Installed")
    print()
    print("Rebuild complete. New version is active.")
    return 0


def cmd_uninstall(args) -> int:
    """Uninstall: clear uv cache and remove agentwire-dev tool."""
    print("Uninstalling agentwire-dev...")
    print()

    # Step 1: Clear uv cache
    if UV_CACHE_DIR.exists():
        print(f"Clearing uv cache ({UV_CACHE_DIR})...")
        shutil.rmtree(UV_CACHE_DIR)
        print("  ✓ Cache cleared")
    else:
        print("  - No cache to clear")

    # Step 2: Uninstall
    print("Uninstalling tool...")
    result = subprocess.run(
        ["uv", "tool", "uninstall", "agentwire-dev"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  ✓ Uninstalled")
    else:
        print(f"  - {result.stderr.strip() or 'Not installed'}")

    print()
    print("Uninstall complete.")
    print(f"To reinstall: cd {get_source_dir()} && uv tool install .")
    return 0


# === MCP Server Command ===


def cmd_mcp(args) -> int:
    """Run the MCP server on stdio.

    Exposes AgentWire capabilities as MCP tools for external agents
    like MoltBot, Claude Desktop, etc.
    """
    from .mcp_server import run_server
    run_server()
    return 0


# === Hooks Commands ===

CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks"


# =============================================================================
# Roles Commands
# =============================================================================


def cmd_roles_list(args) -> int:
    """List available roles from all sources."""
    from .roles import parse_role_file

    json_mode = getattr(args, 'json', False)

    # Collect roles from all sources
    roles_data = []

    # User roles (~/.agentwire/roles/)
    user_roles_dir = Path.home() / ".agentwire" / "roles"
    if user_roles_dir.exists():
        for role_file in user_roles_dir.glob("*.md"):
            role = parse_role_file(role_file)
            if role:
                roles_data.append({
                    "name": role.name,
                    "description": role.description,
                    "source": "user",
                    "path": str(role_file),
                    "disallowed_tools": role.disallowed_tools,
                })

    # Bundled roles (agentwire/roles/)
    try:
        bundled_dir = Path(__file__).parent / "roles"
        if bundled_dir.exists():
            for role_file in bundled_dir.glob("*.md"):
                # Skip if user already has this role
                if any(r["name"] == role_file.stem for r in roles_data):
                    continue
                role = parse_role_file(role_file)
                if role:
                    roles_data.append({
                        "name": role.name,
                        "description": role.description,
                        "source": "bundled",
                        "path": str(role_file),
                        "disallowed_tools": role.disallowed_tools,
                        })
    except Exception:
        pass

    if json_mode:
        _output_json({"roles": roles_data})
        return 0

    if not roles_data:
        print("No roles found.")
        print("Create roles in: ~/.agentwire/roles/")
        return 0

    # Print table
    print("Available Roles:")
    print()
    print(f"{'Name':<20} {'Source':<10} {'Description':<40}")
    print("-" * 70)
    for r in sorted(roles_data, key=lambda x: x["name"]):
        desc = r["description"][:37] + "..." if len(r["description"]) > 40 else r["description"]
        print(f"{r['name']:<20} {r['source']:<10} {desc:<40}")

    print()
    print("User roles: ~/.agentwire/roles/")
    print("Use 'agentwire roles show <name>' for details")
    return 0


def cmd_projects_list(args) -> int:
    """List discovered projects."""
    from .projects import get_projects

    json_mode = getattr(args, 'json', False)
    machine_filter = getattr(args, 'machine', None)

    projects = get_projects(machine=machine_filter)

    if json_mode:
        _output_json({"projects": projects})
        return 0

    if not projects:
        print("No projects found.")
        print("Projects need a .agentwire.yml file in their directory.")
        return 0

    # Print table
    print(f"Discovered Projects ({len(projects)}):\n")
    print(f"{'Name':<25} {'Type':<15} {'Path':<40}")
    print("-" * 80)
    for p in projects:
        # Truncate long paths
        path = p["path"]
        if len(path) > 40:
            path = "..." + path[-37:]
        machine_suffix = f" @{p['machine']}" if p['machine'] != 'local' else ""
        print(f"{p['name']:<25} {p['type']:<15} {path:<40}{machine_suffix}")

    print()
    return 0


def cmd_roles_show(args) -> int:
    """Show details for a specific role."""
    from .roles import discover_role, parse_role_file

    name = args.name
    json_mode = getattr(args, 'json', False)

    # Discover role
    role_path = discover_role(name)
    if not role_path:
        if json_mode:
            _output_json({"error": f"Role '{name}' not found"})
        else:
            print(f"Role '{name}' not found.", file=sys.stderr)
            print("Available locations:")
            print(f"  User: ~/.agentwire/roles/{name}.md")
            print(f"  Bundled: agentwire/roles/{name}.md")
        return 1

    role = parse_role_file(role_path)
    if not role:
        if json_mode:
            _output_json({"error": "Failed to parse role file"})
        else:
            print(f"Failed to parse role file: {role_path}", file=sys.stderr)
        return 1

    if json_mode:
        _output_json({
            "name": role.name,
            "description": role.description,
            "path": str(role_path),
            "tools": role.tools,
            "disallowed_tools": role.disallowed_tools,
            "color": role.color,
            "instructions": role.instructions,
        })
        return 0

    print(f"Role: {role.name}")
    print(f"Description: {role.description or '(none)'}")
    print(f"Path: {role_path}")
    if role.tools:
        print(f"Tools (whitelist): {', '.join(role.tools)}")
    if role.disallowed_tools:
        print(f"Disallowed Tools: {', '.join(role.disallowed_tools)}")
    print()
    if role.instructions:
        print("Instructions:")
        print("-" * 40)
        print(role.instructions)
        print("-" * 40)
    else:
        print("Instructions: (none)")

    return 0


def get_hooks_source() -> Path:
    """Get the path to the hooks directory in the installed package."""
    # First try: hooks directory inside the agentwire package
    package_dir = Path(__file__).parent
    hooks_dir = package_dir / "hooks"
    if hooks_dir.exists():
        return hooks_dir

    # Fallback: try importlib.resources (for installed packages)
    try:
        with importlib.resources.files("agentwire").joinpath("hooks") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find hooks directory in package")


CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"


def get_commands_source() -> Path:
    """Get the path to the commands directory in the installed package."""
    package_dir = Path(__file__).parent
    commands_dir = package_dir / "commands"
    if commands_dir.exists():
        return commands_dir

    try:
        with importlib.resources.files("agentwire").joinpath("commands") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find commands directory in package")


def install_commands(force: bool = False) -> list[str]:
    """Symlink bundled slash commands into ~/.claude/commands/.

    Returns list of command names that were installed or updated.
    """
    try:
        commands_source = get_commands_source()
    except FileNotFoundError:
        return []

    CLAUDE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)

    installed = []
    for src_file in commands_source.glob("*.md"):
        target = CLAUDE_COMMANDS_DIR / src_file.name
        if target.exists() or target.is_symlink():
            if target.is_symlink() and target.resolve() == src_file.resolve() and not force:
                continue  # Already correctly symlinked
            target.unlink()

        target.symlink_to(src_file.resolve())
        installed.append(src_file.stem)

    return installed


def register_hook_in_settings() -> bool:
    """Register the permission hook in Claude's settings.json.

    Returns True if settings were updated, False if already configured.

    Claude Code hook format:
    {
      "hooks": {
        "PermissionRequest": [
          {
            "matcher": ".*",
            "hooks": [
              {"type": "command", "command": "~/.claude/hooks/agentwire-permission.sh"}
            ]
          }
        ]
      }
    }
    """
    settings_file = Path.home() / ".claude" / "settings.json"
    # Use ~ for portability
    hook_command = "~/.claude/hooks/agentwire-permission.sh"

    # Load existing settings or create new
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    # Ensure hooks structure exists
    if "hooks" not in settings:
        settings["hooks"] = {}
    if "PermissionRequest" not in settings["hooks"]:
        settings["hooks"]["PermissionRequest"] = []

    # Check if already registered (check nested hooks array)
    for entry in settings["hooks"]["PermissionRequest"]:
        if "hooks" in entry:
            for h in entry["hooks"]:
                if h.get("command") == hook_command:
                    return False  # Already registered

    # Add hook with correct Claude Code format
    hook_entry = {
        "matcher": ".*",
        "hooks": [
            {"type": "command", "command": hook_command}
        ]
    }
    settings["hooks"]["PermissionRequest"].append(hook_entry)

    # Write back
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(settings, indent=2))

    return True


def install_permission_hook(force: bool = False, copy: bool = False) -> bool:
    """Install the permission hook for Claude Code integration.

    Returns True if hook was installed/updated, False if skipped.
    """
    hook_name = "agentwire-permission.sh"

    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        print("  Warning: hooks directory not found, skipping hook installation")
        return False

    source_hook = hooks_source / hook_name
    if not source_hook.exists():
        print(f"  Warning: {hook_name} not found in package, skipping hook installation")
        return False

    # Create ~/.claude/hooks if it doesn't exist
    CLAUDE_HOOKS_DIR.mkdir(parents=True, exist_ok=True)

    target_hook = CLAUDE_HOOKS_DIR / hook_name

    # Check if already installed
    file_updated = False
    if target_hook.exists():
        if target_hook.is_symlink():
            current_target = target_hook.resolve()
            if current_target == source_hook.resolve() and not force:
                file_updated = False  # File already correctly installed
            else:
                target_hook.unlink()
                file_updated = True
        elif not force:
            print(f"  Hook already exists at {target_hook}")
            file_updated = False
        else:
            target_hook.unlink()
            file_updated = True
    else:
        file_updated = True

    # Create symlink (preferred) or copy if needed
    if file_updated or not target_hook.exists():
        if copy:
            shutil.copy2(source_hook, target_hook)
        else:
            target_hook.symlink_to(source_hook)
        # Make executable
        target_hook.chmod(0o755)

    # Register in settings.json
    settings_updated = register_hook_in_settings()

    return file_updated or settings_updated


def cmd_hooks_install(args) -> int:
    """Install Claude Code permission hook and slash commands for AgentWire integration."""
    hook_installed = install_permission_hook(force=args.force, copy=args.copy)
    if hook_installed:
        print(f"Installed permission hook to {CLAUDE_HOOKS_DIR / 'agentwire-permission.sh'}")
        print("\nPermission hook enables prompted sessions to show permission dialogs in the portal.")
    else:
        print("Permission hook already installed.")

    installed_commands = install_commands(force=args.force)
    if installed_commands:
        print(f"\nInstalled slash commands to {CLAUDE_COMMANDS_DIR}:")
        for name in installed_commands:
            print(f"  /{name}")
    else:
        print("Slash commands already installed.")

    return 0


def unregister_hook_from_settings() -> bool:
    """Remove the permission hook from Claude's settings.json.

    Returns True if settings were updated, False if not found.
    """
    settings_file = Path.home() / ".claude" / "settings.json"
    hook_command = "~/.claude/hooks/agentwire-permission.sh"

    if not settings_file.exists():
        return False

    try:
        settings = json.loads(settings_file.read_text())
    except json.JSONDecodeError:
        return False

    if "hooks" not in settings or "PermissionRequest" not in settings["hooks"]:
        return False

    # Filter out entries containing our hook
    original_len = len(settings["hooks"]["PermissionRequest"])
    new_entries = []
    for entry in settings["hooks"]["PermissionRequest"]:
        if "hooks" in entry:
            # Check if any hook in this entry matches ours
            has_our_hook = any(h.get("command") == hook_command for h in entry["hooks"])
            if not has_our_hook:
                new_entries.append(entry)
        else:
            new_entries.append(entry)

    settings["hooks"]["PermissionRequest"] = new_entries

    if len(settings["hooks"]["PermissionRequest"]) == original_len:
        return False  # Hook wasn't registered

    # Clean up empty structures
    if not settings["hooks"]["PermissionRequest"]:
        del settings["hooks"]["PermissionRequest"]
    if not settings["hooks"]:
        del settings["hooks"]

    # Write back
    settings_file.write_text(json.dumps(settings, indent=2))
    return True


def is_hook_registered() -> bool:
    """Check if the permission hook is registered in Claude's settings.json."""
    settings_file = Path.home() / ".claude" / "settings.json"
    hook_command = "~/.claude/hooks/agentwire-permission.sh"

    if not settings_file.exists():
        return False

    try:
        settings = json.loads(settings_file.read_text())
    except json.JSONDecodeError:
        return False

    if "hooks" not in settings or "PermissionRequest" not in settings["hooks"]:
        return False

    # Check nested hooks array for our command
    for entry in settings["hooks"]["PermissionRequest"]:
        if "hooks" in entry:
            for h in entry["hooks"]:
                if h.get("command") == hook_command:
                    return True
    return False


def cmd_hooks_uninstall(args) -> int:
    """Uninstall Claude Code permission hook."""
    hook_file = CLAUDE_HOOKS_DIR / "agentwire-permission.sh"
    hook_removed = False

    if hook_file.exists():
        hook_file.unlink()
        print(f"Removed hook: {hook_file}")
        hook_removed = True

    # Also unregister from settings.json
    if unregister_hook_from_settings():
        print("Unregistered hook from Claude settings.json")

    if not hook_removed:
        print("Hook not installed")

    return 0


def cmd_hooks_status(args) -> int:
    """Check Claude Code permission hook and tmux portal sync hooks."""
    # Claude Code permission hook
    print("=== Claude Code Permission Hook ===")
    hook_file = CLAUDE_HOOKS_DIR / "agentwire-permission.sh"
    hook_installed = hook_file.exists()
    hook_registered = is_hook_registered()

    if hook_installed:
        if hook_file.is_symlink():
            source = hook_file.resolve()
            print("Status: installed (symlink)")
            print(f"  Location: {hook_file} -> {source}")
        else:
            print("Status: installed (copy)")
            print(f"  Location: {hook_file}")
        if hook_registered:
            print("  Registered: yes (in ~/.claude/settings.json)")
        else:
            print("  Registered: NO - run 'agentwire hooks install --force' to fix")
    else:
        print("Status: not installed")
        print("  Run 'agentwire hooks install' to enable permission dialogs in portal")

    # Tmux portal sync hooks
    print("\n=== Tmux Portal Sync Hooks ===")
    try:
        # Check global hooks first
        global_result = subprocess.run(
            ["tmux", "show-hooks", "-g"],
            capture_output=True,
            text=True,
        )
        global_hooks = global_result.stdout.strip()

        print("Global hooks:")
        has_global_created = "session-created" in global_hooks
        has_global_closed = "session-closed" in global_hooks

        if has_global_created or has_global_closed:
            parts = []
            if has_global_created:
                parts.append("session-created")
            if has_global_closed:
                parts.append("session-closed")
            print(f"  {', '.join(parts)}")
        else:
            print("  none (run 'agentwire portal restart' to install)")

        # Get list of sessions for per-session hooks
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("\nNo tmux sessions running")
            return 0 if hook_installed else 1

        sessions = result.stdout.strip().split("\n") if result.stdout.strip() else []

        if sessions:
            print("\nPer-session hooks:")
            for session in sessions:
                hooks_result = subprocess.run(
                    ["tmux", "show-hooks", "-t", session],
                    capture_output=True,
                    text=True,
                )
                hooks_output = hooks_result.stdout.strip()

                has_session_closed = "session-closed" in hooks_output
                has_kill_pane = "after-kill-pane" in hooks_output

                status_parts = []
                if has_session_closed:
                    status_parts.append("session-closed")
                if has_kill_pane:
                    status_parts.append("after-kill-pane")

                if status_parts:
                    print(f"  {session}: {', '.join(status_parts)}")
                else:
                    print(f"  {session}: none")

    except Exception as e:
        print(f"Error checking tmux hooks: {e}")

    return 0 if hook_installed else 1


# === Tunnel Commands ===


def cmd_tunnels_up(args) -> int:
    """Create all required tunnels."""
    from .network import NetworkContext
    from .tunnels import TunnelManager

    ctx = NetworkContext.from_config()
    manager = TunnelManager()
    required = ctx.get_required_tunnels()

    if not required:
        print("No tunnels required for this machine's configuration.")
        print("(All services run locally or no remote services configured)")
        return 0

    print("Creating tunnels for this machine...\n")

    all_success = True
    for i, spec in enumerate(required, 1):
        # Get service name for display
        service_name = _get_service_for_tunnel(ctx, spec)

        print(f"[{i}/{len(required)}] {service_name} (localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port})")

        status = manager.create_tunnel(spec, ctx)

        if status.status == "up":
            if status.error:
                # Tunnel up but service not responding
                print(f"      ! Tunnel created (PID {status.pid})")
                print(f"      ! Warning: {status.error}")
            else:
                print(f"      + Tunnel created (PID {status.pid})")
        else:
            all_success = False
            print(f"      x Failed: {status.error}")
            _print_tunnel_help(spec, status.error)

        print()

    if all_success:
        print("All tunnels up. Services should be reachable.")
    else:
        print("Some tunnels failed. Check errors above.")
        return 1

    return 0


def cmd_tunnels_down(args) -> int:
    """Tear down all tunnels."""
    from .tunnels import TunnelManager

    manager = TunnelManager()
    count = manager.destroy_all_tunnels()

    if count == 0:
        print("No active tunnels to tear down.")
    else:
        print(f"Killed {count} tunnel(s).")

    return 0


def cmd_tunnels_status(args) -> int:
    """Show tunnel health."""
    from .network import NetworkContext
    from .tunnels import TunnelManager

    ctx = NetworkContext.from_config()
    manager = TunnelManager()

    # Get both required and active tunnels
    required = ctx.get_required_tunnels()
    active = manager.list_tunnels()

    print("AgentWire Tunnels")
    print("-" * 55)

    if not required and not active:
        print("\nNo tunnels configured or active.")
        print("(All services run locally or no remote services configured)")
        return 0

    # Show required tunnels
    for spec in required:
        service_name = _get_service_for_tunnel(ctx, spec)

        print(f"\n{service_name} (localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port})")

        status = manager.check_tunnel(spec)

        if status.status == "up":
            print(f"  Status: + UP (PID {status.pid})")
        elif status.status == "down":
            print("  Status: - DOWN")
        else:
            print("  Status: x ERROR")
            if status.error:
                print(f"  Error: {status.error}")

    # Show any orphaned tunnels (active but not required)
    required_ids = {s.id for s in required}
    orphaned = [t for t in active if t.spec.id not in required_ids]
    if orphaned:
        print("\n" + "-" * 55)
        print("\nOrphaned tunnels (active but no longer required):")
        for t in orphaned:
            print(f"  localhost:{t.spec.local_port} -> {t.spec.remote_machine}:{t.spec.remote_port}")
            print(f"    PID: {t.pid}, Status: {t.status}")

    print("\n" + "-" * 55)

    # Show next steps
    down_tunnels = [s for s in required if manager.check_tunnel(s).status != "up"]
    if down_tunnels:
        print("To create missing tunnels: agentwire tunnels up")

    return 0


def cmd_tunnels_check(args) -> int:
    """Verify tunnels are working with health checks."""
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health

    ctx = NetworkContext.from_config()
    manager = TunnelManager()
    required = ctx.get_required_tunnels()

    if not required:
        print("No tunnels required for this machine.")
        return 0

    print("Checking tunnel health...\n")

    all_healthy = True
    for spec in required:
        service_name = _get_service_for_tunnel(ctx, spec)
        status = manager.check_tunnel(spec)

        if status.status == "up":
            # Also test the actual service through the tunnel
            url = f"http://localhost:{spec.local_port}/health"
            healthy, err = test_service_health(url, timeout=3)

            if healthy:
                print(f"+ {service_name}: healthy")
            else:
                print(f"! {service_name}: tunnel up but service not responding")
                if err:
                    print(f"  {err}")
                all_healthy = False
        elif status.status == "down":
            print(f"x {service_name}: down")
            all_healthy = False
        else:
            print(f"x {service_name}: error - {status.error}")
            all_healthy = False

    if all_healthy:
        print("\nAll tunnels healthy.")
        return 0
    else:
        print("\nSome tunnels need attention. Run: agentwire tunnels up")
        return 1


def _get_service_for_tunnel(ctx, spec) -> str:
    """Get human-readable service name for a tunnel spec."""
    # Check which service this tunnel is for
    for service_name in ["portal", "tts"]:
        service_config = getattr(ctx.config.services, service_name, None)
        if service_config and service_config.machine == spec.remote_machine and service_config.port == spec.remote_port:
            return f"Portal -> {service_name.upper()}" if service_name != "portal" else "Portal"

    return f"Tunnel to {spec.remote_machine}"


def _print_tunnel_help(spec, error: str) -> None:
    """Print helpful diagnostics for tunnel errors."""
    if not error:
        return

    error_lower = error.lower()

    print("\n      Possible causes:")

    if "port" in error_lower and "in use" in error_lower:
        print("        1. Another process is using this port")
        print("        2. A previous tunnel wasn't cleaned up")
        print("\n      To diagnose:")
        print(f"        lsof -i :{spec.local_port}    # Find process using port")
        print("        agentwire tunnels down        # Clean up stale tunnels")

    elif "permission denied" in error_lower:
        print("        1. SSH key not authorized on remote machine")
        print("        2. Wrong user configured")
        print("\n      To fix:")
        print(f"        ssh-copy-id {spec.remote_machine}")

    elif "host key" in error_lower:
        print("        1. Remote machine was reinstalled/changed")
        print("        2. Possible security issue (man-in-the-middle)")
        print("\n      If expected, fix with:")
        print(f"        ssh-keygen -R {spec.remote_machine}")

    elif "connection refused" in error_lower:
        print("        1. SSH server not running on remote")
        print("        2. Firewall blocking port 22")
        print("\n      To diagnose:")
        print(f"        ssh {spec.remote_machine} echo ok")

    elif "timed out" in error_lower or "no route" in error_lower:
        print("        1. Machine is powered off or unreachable")
        print("        2. Network connectivity issue")
        print("\n      To diagnose:")
        print(f"        ping {spec.remote_machine}")

    elif "not responding" in error_lower:
        print("        1. Remote service not started")
        print("        2. Remote service on wrong port")
        print("\n      To diagnose:")
        print(f"        ssh {spec.remote_machine} 'lsof -i :{spec.remote_port}'")


# =============================================================================
# Task Commands (Scheduled Workloads)
# =============================================================================

# Exit codes for ensure command (documented in CLAUDE.md)
ENSURE_EXIT_COMPLETE = 0
ENSURE_EXIT_FAILED = 1
ENSURE_EXIT_INCOMPLETE = 2
ENSURE_EXIT_LOCK_CONFLICT = 3
ENSURE_EXIT_PRE_FAILURE = 4
ENSURE_EXIT_TIMEOUT = 5
ENSURE_EXIT_SESSION_ERROR = 6


def _ensure_remote(args, session: str, machine_id: str, json_mode: bool) -> int:
    """Delegate `ensure` to the remote machine via SSH.

    When the session target is `name@machine`, we reconstruct the full
    `agentwire ensure` command and run it on the remote machine natively.
    All local concerns (locking, idle detection, pre/post commands, summary
    files) happen on the remote machine where the session actually lives.
    """
    import shlex

    machine = _get_machine_config(machine_id)
    if machine is None:
        return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json", exit_code=ENSURE_EXIT_SESSION_ERROR)

    host = machine.get("host", machine_id)
    user = machine.get("user")
    port = machine.get("port")
    ssh_target = f"{user}@{host}" if user else host

    # Translate local project path to remote equivalent
    remote_project = None
    if hasattr(args, 'project') and args.project:
        local_path = Path(args.project).expanduser().resolve()
        # Get local projects dir from config
        config = load_config()
        local_projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser().resolve()
        # Get remote projects dir from machine config (or default)
        remote_projects_dir = machine.get("projects_dir", "~/projects")
        try:
            relative = local_path.relative_to(local_projects_dir)
            remote_project = f"{remote_projects_dir}/{relative}"
        except ValueError:
            # Path not under local projects dir — use basename only
            remote_project = f"{remote_projects_dir}/{local_path.name}"

    # Reconstruct ensure command for the remote (session without @machine)
    cmd_parts = ["agentwire", "ensure", "-s", session, "--task", args.task, "--json"]
    if remote_project:
        cmd_parts.extend(["--project", remote_project])
    if getattr(args, 'wait_lock', False):
        cmd_parts.append("--wait-lock")
    if getattr(args, 'lock_timeout', 60) != 60:
        cmd_parts.extend(["--lock-timeout", str(args.lock_timeout)])
    if getattr(args, 'skip_if_locked', False):
        cmd_parts.append("--skip-if-locked")

    remote_cmd = f"bash -l -c {shlex.quote(' '.join(shlex.quote(p) for p in cmd_parts))}"

    ssh_cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([ssh_target, remote_cmd])

    # Stream output in real-time — ensure can run for tens of minutes
    try:
        proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last_line = ""
        for line in proc.stdout:
            print(line, end="", flush=True)
            last_line = line.rstrip()
        proc.wait()
        return proc.returncode
    except Exception as e:
        return _output_result(False, json_mode, f"SSH to {machine_id} failed: {e}", exit_code=ENSURE_EXIT_SESSION_ERROR)


def cmd_ensure(args) -> int:
    """Run a named task with reliable session management.

    Full lifecycle:
    1. Acquire lock (fail if locked, or wait with --wait-lock)
    2. Ensure session exists and is healthy
    3. Wait for session to be idle
    4. Run pre-commands, validate outputs
    5. Send templated prompt
    6. Wait for idle, send system summary prompt
    7. Parse summary file for status
    8. Send on_task_end prompt if defined
    9. Run post-commands
    10. Handle retries on failure
    """
    from .completion import (
        CompletionTimeout,
        generate_summary_filename,
        get_summary_prompt,
        status_to_exit_code,
    )
    from .locking import LockConflict, LockTimeout, session_lock
    from .tasks import (
        PreCommandError,
        TaskNotFound,
        TaskValidationError,
        load_task,
        run_post_command,
        run_pre_command,
        validate_task,
    )
    from .templating import TemplateContext, TemplateError, expand_all, preview_template

    session_name = args.session
    task_name = args.task
    dry_run = getattr(args, 'dry_run', False)
    wait_lock = getattr(args, 'wait_lock', False)
    lock_timeout = getattr(args, 'lock_timeout', 60)
    skip_if_locked = getattr(args, 'skip_if_locked', False)
    json_mode = getattr(args, 'json', False)

    # Parse session target
    session, machine_id = _parse_session_target(session_name)

    if machine_id:
        return _ensure_remote(args, session, machine_id, json_mode)

    # Find project path from --project flag, or session's working directory
    if hasattr(args, 'project') and args.project:
        project_path = Path(args.project).expanduser().resolve()
    else:
        project_path = _get_session_project_path(session)

    if not project_path.exists():
        return _output_result(False, json_mode, f"Project path not found: {project_path}", exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Load task configuration
    try:
        task = load_task(project_path, task_name)
    except TaskNotFound as e:
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_SESSION_ERROR)
    except TaskValidationError as e:
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Validate task
    issues = validate_task(task)
    if issues:
        return _output_result(False, json_mode, f"Task validation failed: {', '.join(issues)}", exit_code=ENSURE_EXIT_SESSION_ERROR)

    # Determine shell
    shell = task.shell or "/bin/sh"

    # Initialize template context
    ctx = TemplateContext(
        session=session,
        task=task_name,
        project_root=str(project_path),
    )

    # Dry run mode
    if dry_run:
        print("=== DRY RUN ===\n")
        print(f"Session: {session}")
        print(f"Task: {task_name}")
        print(f"Shell: {shell}")
        print(f"Idle timeout: {task.idle_timeout}s")
        print(f"Retries: {task.retries}")
        print()

        if task.pre:
            print("Pre-commands (would execute):")
            for pre in task.pre:
                req = " (required)" if pre.required else ""
                val = f" validate: {pre.validate}" if pre.validate else ""
                print(f"  {pre.name}: {pre.cmd}{req}{val}")
            print()

        print("Prompt (with placeholders for pre-outputs):")
        print(preview_template(task.prompt, ctx))
        print()

        print("System summary prompt:")
        print(get_summary_prompt("<generated-filename>"))
        print()

        if task.on_task_end:
            print("On task end prompt:")
            print(preview_template(task.on_task_end, ctx))
            print()

        if task.post:
            print("Post-commands (would execute):")
            for cmd in task.post:
                print(f"  {preview_template(cmd, ctx)}")
            print()

        if task.output.notify:
            print(f"Notification: {task.output.notify}")
        if task.output.save:
            print(f"Save output to: {preview_template(task.output.save, ctx)}")

        return 0

    # Acquire lock
    try:
        with session_lock(session, wait=wait_lock, timeout=lock_timeout):
            return _run_ensure_task(
                args, session, task, ctx, shell, project_path, json_mode
            )
    except LockConflict as e:
        if skip_if_locked:
            return 0
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_LOCK_CONFLICT)
    except LockTimeout as e:
        if skip_if_locked:
            return 0
        return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_LOCK_CONFLICT)


def _setup_task_branch(project_path, task, json_mode) -> tuple[str, str | None]:
    """Set up git branch for a task with starting_ref.

    Checks out starting_ref, pulls latest, creates the work branch.

    Returns:
        (work_branch_name, error_message) — error_message is None on success.
    """
    from .tasks import PreCommandError  # noqa: F401 (used for caller)

    starting_ref = task.starting_ref

    # Verify the ref exists
    result = subprocess.run(
        ["git", "rev-parse", "--verify", starting_ref],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "", f"starting_ref '{starting_ref}' not found in {project_path}"

    # Checkout the starting ref
    checkout = subprocess.run(
        ["git", "checkout", starting_ref],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        return "", f"Failed to checkout '{starting_ref}': {checkout.stderr.strip()}"

    # Pull if it's a branch (not detached HEAD)
    head_check = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        cwd=project_path,
        capture_output=True,
    )
    if head_check.returncode == 0:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=project_path,
            capture_output=True,
        )

    # Determine work branch name
    work_branch = task.work_branch
    if not work_branch:
        today = datetime.date.today().isoformat()
        work_branch = f"agent/{task.name}-{today}"

    # Handle collision: append -2, -3, ... until name is free
    check = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{work_branch}"],
        cwd=project_path,
        capture_output=True,
    )
    if check.returncode == 0:
        n = 2
        base = work_branch
        while True:
            candidate = f"{base}-{n}"
            check = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{candidate}"],
                cwd=project_path,
                capture_output=True,
            )
            if check.returncode != 0:
                work_branch = candidate
                break
            n += 1

    # Create and checkout work branch
    create = subprocess.run(
        ["git", "checkout", "-b", work_branch],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        return "", f"Failed to create work branch '{work_branch}': {create.stderr.strip()}"

    if not json_mode:
        print(f"Branch: {work_branch} (from {starting_ref})")

    return work_branch, None


def _create_task_pr(project_path, task, work_branch, last_summary, json_mode) -> str | None:
    """Commit, push, and open a PR for completed task work.

    Returns:
        PR URL if created, None if skipped or failed.
    """
    pr_target = task.pr_target or task.starting_ref

    # Check for uncommitted changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    has_changes = bool(status.stdout.strip())

    if not has_changes:
        if not json_mode:
            print("No changes to commit — skipping PR creation")
        # Still reset to starting_ref
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Commit all changes
    today = datetime.date.today().isoformat()
    commit_msg = f"chore: agent task {task.name} ({today})"
    subprocess.run(["git", "add", "-A"], cwd=project_path, capture_output=True)
    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        if not json_mode:
            print(f"Warning: commit failed: {commit_result.stderr.strip()}")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Push branch
    push = subprocess.run(
        ["git", "push", "-u", "origin", work_branch],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        if not json_mode:
            print(f"Warning: push failed: {push.stderr.strip()}")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Check gh is available
    gh_check = subprocess.run(["which", "gh"], capture_output=True)
    if gh_check.returncode != 0:
        if not json_mode:
            print("Warning: 'gh' not found — skipping PR creation (branch pushed)")
        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
        return None

    # Create PR
    pr_title = f"agent: {task.name} ({today})"
    pr_body = last_summary if last_summary else f"Automated changes from agent task `{task.name}`."
    pr_cmd = [
        "gh", "pr", "create",
        "--base", pr_target,
        "--head", work_branch,
        "--title", pr_title,
        "--body", pr_body,
    ]
    if task.pr_draft:
        pr_cmd.append("--draft")

    pr_result = subprocess.run(
        pr_cmd,
        cwd=project_path,
        capture_output=True,
        text=True,
    )

    pr_url = None
    if pr_result.returncode == 0:
        pr_url = pr_result.stdout.strip()
        if not json_mode:
            print(f"PR created: {pr_url}")
    else:
        if not json_mode:
            print(f"Warning: PR creation failed: {pr_result.stderr.strip()}")

    # Reset to starting_ref
    subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)

    return pr_url


def _run_ensure_task(args, session, task, ctx, shell, project_path, json_mode) -> int:
    """Run the task (called within lock context).

    Uses hook-based completion detection:
    1. Write task context file (tells hook a scheduled task is running)
    2. Send task prompt
    3. Hook handles: first idle → send summary prompt
    4. Poll for summary file (agent writes it after receiving summary prompt)
    5. Parse summary and return result
    """
    from .completion import (
        CompletionTimeout,
        clear_task_context,
        generate_summary_filename,
        status_to_exit_code,
        wait_for_completion_signal,
        write_task_context,
    )
    from .tasks import PreCommandError, run_post_command, run_pre_command
    from .templating import TemplateError, expand_all

    max_attempts = task.retries + 1
    last_status = "incomplete"
    last_summary = ""

    for attempt in range(1, max_attempts + 1):
        ctx.attempt = attempt

        if not json_mode and max_attempts > 1:
            print(f"Attempt {attempt}/{max_attempts}")

        # Ensure session exists and has agent running.
        # The scheduler may have pre-created this session with --model and --type
        # overrides via _pre_create_session(). Don't kill it — just wait for agent.
        if not tmux_session_exists(session):
            if not json_mode:
                print(f"Creating session '{session}'...")

            # Fork starting_session if configured (carries over Claude conversation context)
            if task.starting_session and task.starting_session != session:
                if tmux_session_exists(task.starting_session):
                    if not json_mode:
                        print(f"Forking context from session '{task.starting_session}'...")
                    fork_result = subprocess.run(
                        ["agentwire", "fork", "-s", task.starting_session, "-t", session, "--json"],
                        capture_output=True, text=True,
                    )
                    if fork_result.returncode != 0 and not json_mode:
                        print(f"Warning: context fork failed, starting fresh session")
                elif not json_mode:
                    print(f"Warning: starting_session '{task.starting_session}' not found, starting fresh")

            if not tmux_session_exists(session):
                class NewArgs:
                    def __init__(self, task_role):
                        self.session = session
                        self.path = str(project_path)
                        self.force = False
                        self.type = None
                        self.roles = task_role if task_role else None
                        self.model = None
                        self.json = json_mode

                result = cmd_new(NewArgs(task.role))
                if result != 0:
                    return _output_result(False, json_mode, f"Failed to create session '{session}'", exit_code=ENSURE_EXIT_SESSION_ERROR)

        # Wait for agent to be ready to accept input.
        # Handles both freshly-created sessions (agent still loading) and
        # pre-created sessions from scheduler (agent may be mid-startup).
        if not json_mode:
            print("Waiting for agent to be ready...")
        if not _wait_for_agent_ready(session, timeout=30):
            # Agent never started — session is dead, bail out
            if not json_mode:
                print(f"Agent not ready in session '{session}' after 30s")
            return _output_result(False, json_mode, f"Agent not running in session '{session}'", exit_code=ENSURE_EXIT_SESSION_ERROR)

        # Set up work branch if starting_ref is configured
        work_branch = None
        if task.starting_ref:
            work_branch, branch_error = _setup_task_branch(project_path, task, json_mode)
            if branch_error:
                return _output_result(False, json_mode, branch_error, exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Run pre-commands
        if task.pre:
            if not json_mode:
                print("Running pre-commands...")

            for pre in task.pre:
                try:
                    output = run_pre_command(pre, shell, project_path)
                    ctx.set_pre_output(pre.name, output)
                    if not json_mode:
                        print(f"  {pre.name}: {len(output)} chars")
                except PreCommandError as e:
                    if work_branch and task.starting_ref:
                        subprocess.run(["git", "checkout", task.starting_ref], cwd=project_path, capture_output=True)
                    return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Expand prompt
        try:
            prompt = expand_all(task.prompt, ctx)
        except TemplateError as e:
            return _output_result(False, json_mode, str(e), exit_code=ENSURE_EXIT_PRE_FAILURE)

        # Generate summary filename (scoped to session to avoid collisions)
        summary_filename = generate_summary_filename(session, task.name)
        summary_path = project_path / summary_filename
        ctx.summary_file = summary_filename

        # Ensure .agentwire directory exists
        (project_path / ".agentwire").mkdir(exist_ok=True)

        # Create iterations directory for loop tasks
        if task.mode == "loop":
            (project_path / ".agentwire" / "iterations").mkdir(exist_ok=True)

        # Clear any stale completion signal from a previous run
        # This prevents immediate return if a previous run's signal wasn't cleaned up
        clear_task_context(session)

        # Write task context for hook coordination
        # Hook will: first idle → send summary prompt (ensure polls for summary file directly)
        # Loop mode: hook iterates (review → re-prompt) until complete or max_iterations
        write_task_context(
            session=session,
            task_name=task.name,
            summary_file=summary_filename,
            attempt=attempt,
            exit_on_complete=task.exit_on_complete,
            mode=task.mode,
            max_iterations=task.max_iterations,
            iteration=1,
            loop_review=task.loop_review,
            loop_delay=task.loop_delay,
            original_prompt=prompt,
        )

        # Find previous summaries for this task to give the agent context
        summary_glob = f".agentwire/task-summary-{session}-{task.name}-*.md"
        prev_summaries = sorted(
            project_path.glob(summary_glob),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        if prev_summaries:
            prompt += "\n\nPrevious task summaries (consider them when generating your output):"
            for p in prev_summaries:
                prompt += f"\n- {p}"

        if not json_mode:
            print("Sending task prompt...")

        # Send task prompt using pane_manager for proper multi-line handling
        pane_manager.send_to_pane(session, 0, prompt, enter=True)

        # Wait for completion signal from hook
        if not json_mode:
            print("Waiting for task completion...")

        try:
            signal = wait_for_completion_signal(
                session, summary_path=summary_path
            )
            last_status = signal.get("status", "incomplete")
            last_summary = signal.get("summary", "")
            ctx.status = last_status
            ctx.summary = last_summary
        except CompletionTimeout:
            # Don't clear task context here — the hook may still need it.
            # Hook cleans up after itself (exit_on_complete kills session).
            # Task context files are cleared at the START of next run.
            last_status = "incomplete"
            last_summary = "Timeout waiting for task completion"
            if attempt < max_attempts:
                if not json_mode:
                    print(f"Timeout, retrying in {task.retry_delay}s...")
                time.sleep(task.retry_delay)
                continue
            break

        # Don't clear task context here — hook owns context file lifecycle.
        # ensure waits for hook to delete it (signals cleanup complete).

        if not json_mode:
            print(f"Task status: {last_status}")
            if last_summary:
                print(f"Summary: {last_summary}")

        # on_task_end: send additional prompt after summary is written
        # Note: we don't wait for this to complete - it's fire-and-forget
        if task.on_task_end:
            try:
                end_prompt = expand_all(task.on_task_end, ctx)
                pane_manager.send_to_pane(session, 0, end_prompt, enter=True)
                if not json_mode:
                    print("Sent on_task_end prompt (not waiting for completion)")
            except TemplateError as e:
                if not json_mode:
                    print(f"Warning: template error in on_task_end: {e}")

        # Capture output
        output_result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{task.output.capture}"],
            capture_output=True,
            text=True,
        )
        ctx.output = output_result.stdout if output_result.returncode == 0 else ""

        # Run post-commands
        if task.post:
            if not json_mode:
                print("Running post-commands...")

            for cmd in task.post:
                try:
                    expanded_cmd = expand_all(cmd, ctx)
                    rc, stdout, stderr = run_post_command(expanded_cmd, shell, project_path)
                    if rc != 0 and not json_mode:
                        print(f"  Warning: post-command failed: {stderr}")
                except TemplateError as e:
                    if not json_mode:
                        print(f"  Warning: template error in post-command: {e}")

        # Handle notifications
        if task.output.notify:
            _handle_task_notification(task.output.notify, ctx, session, json_mode)

        # Save output if configured
        if task.output.save:
            try:
                save_path = Path(expand_all(task.output.save, ctx)).expanduser()
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(ctx.output)
                if not json_mode:
                    print(f"Output saved to: {save_path}")
            except Exception as e:
                if not json_mode:
                    print(f"Warning: Failed to save output: {e}")

        # Create PR if branch management is configured
        if work_branch and task.starting_ref:
            pr_url = _create_task_pr(project_path, task, work_branch, last_summary, json_mode)
            ctx.work_branch = work_branch
            if pr_url:
                ctx.pr_url = pr_url

        # Check if we should retry
        if last_status == "failed" and attempt < max_attempts:
            if not json_mode:
                print(f"Task failed, retrying in {task.retry_delay}s...")
            time.sleep(task.retry_delay)
            continue

        # Done (success or no more retries)
        break

    # Final result
    exit_code = status_to_exit_code(last_status)

    if json_mode:
        result_data = {
            "success": last_status == "complete",
            "status": last_status,
            "summary": last_summary,
            "attempt": ctx.attempt,
            "summary_file": ctx.summary_file,
        }
        if ctx.work_branch:
            result_data["work_branch"] = ctx.work_branch
        if ctx.pr_url:
            result_data["pr_url"] = ctx.pr_url
        _output_json(result_data)
    else:
        print(f"\nTask {task.name}: {last_status}")

    return exit_code


def _handle_task_notification(notify_config: str, ctx, session: str, json_mode: bool) -> None:
    """Handle task notification based on config."""
    from .templating import expand_all, expand_env_vars

    if notify_config == "voice":
        # Speak result
        message = f"Task {ctx.task} {ctx.status}"
        if ctx.summary:
            message += f": {ctx.summary}"
        subprocess.run(["agentwire", "say", "-s", session, message], capture_output=True)

    elif notify_config == "alert":
        # Send text notification to parent
        message = f"Task {ctx.task} {ctx.status}"
        if ctx.summary:
            message += f": {ctx.summary}"
        subprocess.run(["agentwire", "notify-parent", "--to", session, message], capture_output=True)

    elif notify_config.startswith("webhook "):
        # POST to webhook URL
        url = expand_env_vars(notify_config[8:].strip())
        payload = {
            "task": ctx.task,
            "session": ctx.session,
            "status": ctx.status,
            "summary": ctx.summary,
            "timestamp": datetime.datetime.now().isoformat(),
            "attempt": ctx.attempt,
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            if not json_mode:
                print(f"Warning: Webhook notification failed: {e}")

    elif notify_config == "email":
        # Send email with task result
        subject = f"Task {ctx.task}: {ctx.status}"
        body_lines = [f"**{ctx.task}** finished with status: **{ctx.status}**"]
        if ctx.summary:
            body_lines.append(f"\n{ctx.summary}")
        body_lines.append(f"\nSession: {ctx.session}")
        subprocess.run(
            ["agentwire", "email", "--subject", subject, "--body", "\n".join(body_lines)],
            capture_output=True,
        )

    elif notify_config.startswith("command "):
        # Run custom command
        cmd = notify_config[8:].strip()
        try:
            expanded = expand_all(cmd, ctx)
            subprocess.run(expanded, shell=True, capture_output=True, timeout=30)
        except Exception as e:
            if not json_mode:
                print(f"Warning: Notification command failed: {e}")


def cmd_task_list(args) -> int:
    """List tasks for a session/project."""
    from .tasks import list_tasks

    session = getattr(args, 'session', None)
    json_mode = getattr(args, 'json', False)

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    if not project_path or not project_path.exists():
        return _output_result(False, json_mode, f"Project path not found: {project_path}")

    tasks = list_tasks(project_path)

    if json_mode:
        _output_json({"tasks": tasks, "project": str(project_path)})
        return 0

    if not tasks:
        print(f"No tasks defined in {project_path / '.agentwire.yml'}")
        return 0

    print(f"Tasks in {project_path.name}:\n")
    print(f"{'Name':<25} {'Mode':<10} {'Pre':<5} {'Post':<5} {'Retries':<8}")
    print("-" * 60)
    for t in tasks:
        pre = "Yes" if t["has_pre"] else "-"
        post = "Yes" if t["has_post"] else "-"
        mode = t.get("mode", "standard")
        print(f"{t['name']:<25} {mode:<10} {pre:<5} {post:<5} {t['retries']:<8}")

    return 0


def cmd_task_show(args) -> int:
    """Show task definition details."""
    from .tasks import TaskNotFound, TaskValidationError, load_task, validate_task

    task_arg = args.task  # format: session/task or just task
    json_mode = getattr(args, 'json', False)

    # Parse task argument
    if "/" in task_arg:
        session, task_name = task_arg.split("/", 1)
    else:
        session = None
        task_name = task_arg

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    try:
        task = load_task(project_path, task_name)
    except (TaskNotFound, TaskValidationError) as e:
        return _output_result(False, json_mode, str(e))

    issues = validate_task(task)

    if json_mode:
        _output_json({
            "name": task.name,
            "prompt": task.prompt,
            "shell": task.shell,
            "retries": task.retries,
            "retry_delay": task.retry_delay,
            "idle_timeout": task.idle_timeout,
            "mode": task.mode,
            "max_iterations": task.max_iterations,
            "loop_review": task.loop_review,
            "loop_delay": task.loop_delay,
            "pre": [{"name": p.name, "cmd": p.cmd, "required": p.required, "validate": p.validate, "timeout": p.timeout} for p in task.pre],
            "on_task_end": task.on_task_end,
            "post": task.post,
            "output": {"capture": task.output.capture, "save": task.output.save, "notify": task.output.notify},
            "validation_issues": issues,
        })
        return 0

    print(f"Task: {task.name}\n")
    print(f"Shell: {task.shell or '/bin/sh'}")
    print(f"Mode: {task.mode}")
    if task.mode == "loop":
        print(f"Max iterations: {task.max_iterations}")
        print(f"Loop review: {task.loop_review}")
        if task.loop_delay > 0:
            print(f"Loop delay: {task.loop_delay}s")
    print(f"Retries: {task.retries} (delay: {task.retry_delay}s)")
    print(f"Idle timeout: {task.idle_timeout}s")
    print()

    if task.pre:
        print("Pre-commands:")
        for p in task.pre:
            req = " (required)" if p.required else ""
            print(f"  {p.name}: {p.cmd}{req}")
        print()

    print("Prompt:")
    print(task.prompt[:200] + "..." if len(task.prompt) > 200 else task.prompt)
    print()

    if task.on_task_end:
        print("On task end:")
        print(task.on_task_end[:100] + "..." if len(task.on_task_end) > 100 else task.on_task_end)
        print()

    if task.post:
        print("Post-commands:")
        for cmd in task.post:
            print(f"  {cmd}")
        print()

    if task.output.notify or task.output.save:
        print("Output:")
        if task.output.save:
            print(f"  Save to: {task.output.save}")
        if task.output.notify:
            print(f"  Notify: {task.output.notify}")

    if issues:
        print(f"\nValidation issues: {', '.join(issues)}")

    return 0


def cmd_task_validate(args) -> int:
    """Validate task configuration."""
    from .tasks import TaskNotFound, TaskValidationError, load_task, validate_task

    task_arg = args.task
    json_mode = getattr(args, 'json', False)

    # Parse task argument
    if "/" in task_arg:
        session, task_name = task_arg.split("/", 1)
    else:
        session = None
        task_name = task_arg

    # Find project path from session's working directory or cwd
    if session:
        project_path = _get_session_project_path(session)
    else:
        project_path = Path.cwd()

    try:
        task = load_task(project_path, task_name)
    except (TaskNotFound, TaskValidationError) as e:
        return _output_result(False, json_mode, str(e))

    issues = validate_task(task)

    if json_mode:
        _output_json({
            "valid": len(issues) == 0,
            "issues": issues,
            "task": task_name,
        })
        return 0 if not issues else 1

    if issues:
        print(f"Task '{task_name}' has issues:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    else:
        print(f"Task '{task_name}' is valid.")
        return 0


# =============================================================================
# Lock Management Commands
# =============================================================================


def cmd_lock_list(args) -> int:
    """List all locks with metadata."""
    from .locking import list_locks

    json_mode = getattr(args, 'json', False)
    locks = list_locks()

    if json_mode:
        _output_json({"locks": locks})
        return 0

    if not locks:
        print("No locks found.")
        return 0

    # Format output
    print(f"{'SESSION':<25} {'PID':<10} {'AGE':<12} {'STATUS'}")
    print("-" * 60)

    for lock in locks:
        session = lock["session"][:24]
        pid = str(lock["pid"]) if lock["pid"] else "-"
        age_seconds = lock["age_seconds"]

        # Format age
        if age_seconds < 60:
            age = f"{age_seconds}s"
        elif age_seconds < 3600:
            age = f"{age_seconds // 60}m {age_seconds % 60}s"
        elif age_seconds < 86400:
            hours = age_seconds // 3600
            mins = (age_seconds % 3600) // 60
            age = f"{hours}h {mins}m"
        else:
            days = age_seconds // 86400
            hours = (age_seconds % 86400) // 3600
            age = f"{days}d {hours}h"

        status = lock["status"]
        print(f"{session:<25} {pid:<10} {age:<12} {status}")

    return 0


def cmd_lock_clean(args) -> int:
    """Remove all stale locks."""
    from .locking import clean_stale_locks

    json_mode = getattr(args, 'json', False)
    dry_run = getattr(args, 'dry_run', False)

    removed = clean_stale_locks(dry_run=dry_run)

    if json_mode:
        _output_json({
            "removed": removed,
            "count": len(removed),
            "dry_run": dry_run,
        })
        return 0

    if not removed:
        print("No stale locks found.")
    elif dry_run:
        print(f"Would remove {len(removed)} stale lock(s): {', '.join(removed)}")
    else:
        print(f"Removed {len(removed)} stale lock(s): {', '.join(removed)}")

    return 0


def cmd_lock_remove(args) -> int:
    """Force-remove a specific lock."""
    from .locking import remove_lock

    session = args.session
    json_mode = getattr(args, 'json', False)

    removed = remove_lock(session)

    if json_mode:
        _output_json({
            "session": session,
            "removed": removed,
        })
        return 0 if removed else 1

    if removed:
        print(f"Removed lock: {session}")
        return 0
    else:
        print(f"No lock found for: {session}")
        return 1


def cmd_lock(args) -> int:
    """Lock command dispatcher - shows help if no subcommand."""
    # This will be called if no subcommand is provided
    # The help is printed in main() based on lock_command being None
    return 0


# =============================================================================
# Scheduler Commands
# =============================================================================


SCHEDULER_SESSION = "agentwire-scheduler"


def cmd_scheduler_start(args) -> int:
    """Start the scheduler daemon in a tmux session."""
    if not _check_tmux_installed():
        return 1

    if tmux_session_exists(SCHEDULER_SESSION):
        print(f"Scheduler already running in tmux session '{SCHEDULER_SESSION}'")
        print("Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", SCHEDULER_SESSION])
        return 0

    print(f"Starting scheduler daemon in tmux session '{SCHEDULER_SESSION}'...")
    subprocess.run([
        "tmux", "new-session", "-d", "-s", SCHEDULER_SESSION,
    ])
    subprocess.run([
        "tmux", "send-keys", "-t", SCHEDULER_SESSION,
        "agentwire scheduler serve", "Enter",
    ])

    print("Attaching... (Ctrl+B D to detach)")
    subprocess.run(["tmux", "attach-session", "-t", SCHEDULER_SESSION])
    return 0


def cmd_scheduler_serve(args) -> int:
    """Run the scheduler loop in the foreground (for tmux)."""
    from .scheduler import run_scheduler_loop

    run_scheduler_loop()
    return 0


def cmd_scheduler_stop(args) -> int:
    """Stop the scheduler daemon."""
    if not tmux_session_exists(SCHEDULER_SESSION):
        print("Scheduler is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", SCHEDULER_SESSION])
    print("Scheduler stopped.")
    return 0


def cmd_scheduler_status(args) -> int:
    """Show scheduler status and next task due."""
    from .config import get_config
    from .scheduler import (
        format_interval,
        load_board,
        pick_next_task,
    )

    json_mode = getattr(args, 'json', False)
    running = tmux_session_exists(SCHEDULER_SESSION)
    board_path = get_config().scheduler.board_file

    if not board_path.exists():
        return _output_result(
            False, json_mode,
            f"Board file not found: {board_path}",
            running=running,
        )

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        return _output_result(False, json_mode, str(e), running=running)

    task_count = len(board.tasks)
    enabled_count = sum(1 for t in board.tasks.values() if t.enabled)
    next_task, wait_seconds = pick_next_task(board)

    result = {
        "running": running,
        "board_path": str(board_path),
        "task_count": task_count,
        "enabled_count": enabled_count,
        "next_task": next_task,
        "next_in_seconds": round(wait_seconds, 1),
    }

    if json_mode:
        _output_json({"success": True, **result})
        return 0

    status_str = "running" if running else "stopped"
    print(f"Scheduler: {status_str}")
    print(f"Board: {board_path}")
    print(f"Tasks: {enabled_count}/{task_count} enabled")

    if next_task:
        if wait_seconds <= 0:
            print(f"Next: {next_task} (due now)")
        else:
            print(f"Next: {next_task} (in {format_interval(int(wait_seconds))})")
    else:
        print("Next: nothing due")

    return 0


def cmd_scheduler_board(args) -> int:
    """Show full task board with overdue scores."""
    from .scheduler import get_board_display, load_board

    json_mode = getattr(args, 'json', False)

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        return _output_result(False, json_mode, str(e))

    rows = get_board_display(board)

    if json_mode:
        _output_json({"success": True, "tasks": rows})
        return 0

    if not rows:
        print("No tasks in board.")
        return 0

    # Group by project
    groups: dict[str, list[dict]] = {}
    for r in rows:
        proj = r["project"].rstrip("/").split("/")[-1]
        groups.setdefault(proj, [])
        groups[proj].append(r)

    # Summary line
    total = len(rows)
    regular = sum(1 for r in rows if not r["filler"])
    fillers = total - regular
    enabled = sum(1 for r in rows if r["enabled"])
    print(f"Scheduler board: {total} tasks ({regular} regular + {fillers} filler), {enabled} enabled\n")

    for proj, items in groups.items():
        # Sort: regular first (by priority), then filler (by priority)
        reg = sorted([r for r in items if not r["filler"]], key=lambda r: r["priority"])
        fil = sorted([r for r in items if r["filler"]], key=lambda r: r["priority"])

        session = items[0]["session"]
        print(f"  {proj} ({len(items)} tasks) → {session}")
        print(f"  {'Task':<30} {'Type':<16} {'Schedule':<24} {'Last Run':<16} {'Status':<12} {'Overdue'}")
        print(f"  {'-' * 114}")

        for r in reg + fil:
            task_name = r["task"]
            if not r["enabled"]:
                task_name = f"{task_name} [off]"

            if r["filler"]:
                type_str = f"filler (p{r['priority']})"
            elif r["priority"] != 99:
                type_str = f"regular (p{r['priority']})"
            else:
                type_str = "regular"

            status_str = r["last_status"]
            if r.get("in_flight"):
                status_str = "[in-flight]"

            schedule_display = r.get("schedule_str", "?")
            if len(schedule_display) > 22:
                schedule_display = schedule_display[:21] + "…"

            print(
                f"  {task_name:<30} "
                f"{type_str:<16} "
                f"{schedule_display:<24} "
                f"{r['last_run']:<16} "
                f"{status_str:<12} "
                f"{r['overdue_str']}"
            )

        print()

    return 0


def cmd_scheduler_run(args) -> int:
    """Force-run a specific task now."""
    from .scheduler import _render_workflow_inputs, dispatch_task, load_board, save_board

    json_mode = getattr(args, 'json', False)
    dry_run = getattr(args, 'dry_run', False)
    name = args.name

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        return _output_result(False, json_mode, str(e))

    if name not in board.tasks:
        return _output_result(
            False, json_mode,
            f"Task '{name}' not found in board. Available: {', '.join(board.tasks.keys())}",
        )

    task = board.tasks[name]

    if dry_run:
        if not task.workflow:
            return _output_result(
                False, json_mode,
                f"--dry-run only applies to workflow tasks; '{name}' runs via ensure.",
            )
        from agentwire.workflows.definitions import resolve_workflow
        from agentwire.workflows.runner import run_workflow
        try:
            wf = resolve_workflow(task.workflow)
        except Exception as e:
            return _output_result(False, json_mode, f"Could not load workflow '{task.workflow}': {e}")
        rendered_inputs = _render_workflow_inputs(task.inputs, task)
        run = run_workflow(wf, runs_dir=None, inputs=rendered_inputs, dry_run=True)
        if json_mode:
            _output_json({
                "success": True,
                "task": name,
                "dry_run": True,
                "workflow": wf.name,
                "inputs": rendered_inputs,
                "nodes": [r.node_id for r in run.node_results],
                "status": run.status,
            })
            return 0
        print(f"Dry-run: {name} → workflow '{wf.name}'")
        if rendered_inputs:
            print(f"  inputs: {rendered_inputs}")
        for r in run.node_results:
            print(f"  - {r.node_id}: {r.final_text}")
        return 0

    if not json_mode:
        print(f"Running: {name}")

    state = dispatch_task(board, name)
    board.state[name] = state
    save_board(board)

    if json_mode:
        _output_json({
            "success": state.last_status == "complete",
            "task": name,
            "status": state.last_status,
            "duration": state.last_duration,
            "run_count": state.run_count,
        })
        return 0 if state.last_status == "complete" else 1

    print(f"Done: {name} → {state.last_status} ({state.last_duration}s)")
    return 0 if state.last_status == "complete" else 1


def cmd_scheduler_enable(args) -> int:
    """Enable a task in the board."""
    return _set_task_enabled(args.name, True)


def cmd_scheduler_disable(args) -> int:
    """Disable a task in the board."""
    return _set_task_enabled(args.name, False)


def _set_task_enabled(name: str, enabled: bool) -> int:
    """Toggle a task's enabled field in the board YAML."""
    import yaml

    from .config import get_config

    board_path = get_config().scheduler.board_file

    if not board_path.exists():
        print(f"Board file not found: {board_path}", file=sys.stderr)
        return 1

    with open(board_path) as f:
        raw = yaml.safe_load(f) or {}

    tasks = raw.get("tasks", {})
    if name not in tasks:
        print(f"Task '{name}' not found in board.", file=sys.stderr)
        return 1

    tasks[name]["enabled"] = enabled

    with open(board_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

    action = "Enabled" if enabled else "Disabled"
    print(f"{action}: {name}")
    return 0


def cmd_scheduler_history(args) -> int:
    """Show recent run history from board state."""
    from .scheduler import format_interval, load_board

    json_mode = getattr(args, 'json', False)

    try:
        board = load_board()
    except (FileNotFoundError, ValueError) as e:
        return _output_result(False, json_mode, str(e))

    if json_mode:
        history = []
        for name, state in board.state.items():
            task = board.tasks.get(name)
            history.append({
                "task": name,
                "last_run": state.last_run.isoformat() if state.last_run else None,
                "last_status": state.last_status,
                "last_duration": state.last_duration,
                "run_count": state.run_count,
                "workflow": (task.workflow if task else "") or None,
            })
        _output_json({"success": True, "history": history})
        return 0

    if not board.state:
        print("No run history.")
        return 0

    print(f"{'Task':<30} {'Last Run':<20} {'Status':<14} {'Duration':<10} {'Runs'}")
    print("-" * 85)

    for name, state in sorted(board.state.items()):
        if state.last_run:
            lr = state.last_run.strftime("%Y-%m-%d %H:%M")
        else:
            lr = "never"

        dur = format_interval(state.last_duration) if state.last_duration else "-"
        print(f"{name:<30} {lr:<20} {state.last_status:<14} {dur:<10} {state.run_count}")

    return 0


def cmd_scheduler_report(args) -> int:
    """Generate a morning report HTML artifact of recent task runs."""
    import re as _re
    from html import escape as html_escape
    from .scheduler import _parse_duration, format_interval, load_board, read_events

    json_mode = getattr(args, 'json', False)
    since_str = getattr(args, 'since', '8h') or '8h'
    open_artifact = getattr(args, 'artifact', False)

    # Parse duration
    since_seconds = _parse_duration(since_str) or 28800  # default 8h
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=since_seconds)

    # Load board state
    try:
        board = load_board()
    except Exception as e:
        print(f"Error loading board: {e}", file=sys.stderr)
        return 1

    # Load events in the window
    try:
        events = read_events(tail=500)
    except Exception:
        events = []

    # Collect completed task events within window
    runs: list[dict] = []
    for ev in events:
        if ev.get("event") != "task_completed":
            continue
        ts_str = ev.get("ts") or ev.get("timestamp", "")
        try:
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if ts < cutoff:
            continue
        task_name = ev.get("task", "")
        # Collect run data
        run = {
            "task": task_name,
            "status": ev.get("status", "unknown"),
            "duration": ev.get("duration", 0),
            "summary": ev.get("summary", ""),
            "timestamp": ts.strftime("%Y-%m-%d %H:%M"),
            "work_branch": "",
            "pr_url": "",
            "workflow": ev.get("workflow", ""),
            "run_id": ev.get("run_id", ""),
            "nodes": ev.get("nodes") or [],
        }
        runs.append(run)

    # Count totals
    total = len(runs)
    complete = sum(1 for r in runs if r["status"] == "complete")
    failed = sum(1 for r in runs if r["status"] in ("failed", "timeout"))
    incomplete = total - complete - failed

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    report_date = datetime.datetime.now().strftime("%Y-%m-%d")

    def status_badge(status: str) -> str:
        colors = {
            "complete": "#00c853",
            "failed": "#ff5252",
            "timeout": "#ff7043",
            "incomplete": "#ffa726",
            "unknown": "#78909c",
        }
        color = colors.get(status, "#78909c")
        return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:12px;font-size:0.85em">{status}</span>'

    def node_badges(nodes: list) -> str:
        if not nodes:
            return ""
        node_colors = {
            "success": "#00c853",
            "failure": "#ff5252",
            "timeout": "#ff7043",
            "skipped": "#78909c",
        }
        pieces = []
        for n in nodes:
            nid = html_escape(str(n.get("id", "?")))
            nstatus = str(n.get("status", "?"))
            color = node_colors.get(nstatus, "#556")
            pieces.append(
                f'<span style="background:{color};color:#fff;padding:1px 6px;border-radius:8px;font-size:0.75em;margin-right:3px">{nid}</span>'
            )
        return "".join(pieces)

    rows_html = ""
    for r in runs:
        duration_str = format_interval(r["duration"]) if r["duration"] else "-"
        pr_link = f'<a href="{r["pr_url"]}" target="_blank" style="color:#00d4ff">{r["pr_url"][:40]}...</a>' if r.get("pr_url") else "-"
        branch_col = ""
        if r.get("workflow"):
            wf_label = html_escape(r["workflow"])
            run_id = html_escape(r.get("run_id", ""))
            badges = node_badges(r["nodes"])
            run_hint = f' <span style="color:#556;font-size:0.75em">({run_id[:20]}…)</span>' if run_id else ""
            branch_col = f'<code style="font-size:0.85em">workflow:{wf_label}</code>{run_hint}<div style="margin-top:4px">{badges}</div>'
        else:
            branch_col = f'<code style="font-size:0.85em">{r.get("work_branch") or "-"}</code>'
        summary_text = r["summary"][:120] if r["summary"] else "-"
        rows_html += f"""
        <tr>
          <td style="font-weight:600">{r["task"]}</td>
          <td>{status_badge(r["status"])}</td>
          <td>{r["timestamp"]}</td>
          <td>{duration_str}</td>
          <td>{branch_col}</td>
          <td>{pr_link}</td>
          <td style="color:#aaa;font-size:0.85em">{summary_text}</td>
        </tr>"""

    if not rows_html:
        rows_html = f'<tr><td colspan="7" style="color:#556;text-align:center;padding:24px">No tasks ran in the last {since_str}</td></tr>'

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Morning Report — {report_date}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; background: #1a1a2e; color: #e0e0e0; }}
  h1 {{ color: #00d4ff; margin-bottom: 4px; }}
  .meta {{ color: #556; font-size: 0.85em; margin-bottom: 20px; }}
  .summary-bar {{ display: flex; gap: 24px; padding: 14px 20px; background: #16213e; border-radius: 8px; margin-bottom: 24px; }}
  .summary-bar .item {{ display: flex; flex-direction: column; }}
  .summary-bar .label {{ color: #556; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.5px; }}
  .summary-bar .value {{ font-size: 1.4em; font-weight: 700; }}
  .complete {{ color: #00c853; }}
  .failed {{ color: #ff5252; }}
  .incomplete {{ color: #ffa726; }}
  .total {{ color: #e0e0e0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #2a2a4a; font-size: 0.9em; }}
  th {{ background: #16213e; color: #00d4ff; font-weight: 600; position: sticky; top: 0; }}
  tr:hover {{ background: #16213e; }}
  code {{ background: #0d1b2a; padding: 2px 6px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>Morning Report</h1>
<p class="meta">Generated {now_str} &nbsp;&middot;&nbsp; Last {since_str}</p>

<div class="summary-bar">
  <div class="item"><span class="label">Total</span><span class="value total">{total}</span></div>
  <div class="item"><span class="label">Complete</span><span class="value complete">{complete}</span></div>
  <div class="item"><span class="label">Failed</span><span class="value failed">{failed}</span></div>
  <div class="item"><span class="label">Incomplete</span><span class="value incomplete">{incomplete}</span></div>
</div>

<table>
  <thead>
    <tr>
      <th>Task</th>
      <th>Status</th>
      <th>Time</th>
      <th>Duration</th>
      <th>Branch</th>
      <th>PR</th>
      <th>Summary</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
</body>
</html>"""

    # Write artifact
    artifacts_dir = Path.home() / ".agentwire" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    filename = f"morning-report-{report_date}.html"
    report_path = artifacts_dir / filename
    report_path.write_text(html)

    if json_mode:
        _output_json({
            "success": True,
            "path": str(report_path),
            "filename": filename,
            "total": total,
            "complete": complete,
            "failed": failed,
            "incomplete": incomplete,
        })
    else:
        print(f"Report: {report_path}")
        print(f"Tasks: {total} total — {complete} complete, {failed} failed, {incomplete} incomplete")

    if open_artifact:
        subprocess.run(
            ["agentwire", "open", filename, "--title", f"Morning Report {report_date}"],
            capture_output=True,
        )

    return 0


def cmd_scheduler_events(args) -> int:
    """Show recent scheduler events from the JSONL log."""
    from .scheduler import read_events

    json_mode = getattr(args, 'json', False)
    tail = getattr(args, 'tail', 20)
    task_filter = getattr(args, 'task', None)

    events = read_events(tail=tail, task_filter=task_filter)

    if json_mode:
        _output_json({"success": True, "events": events})
        return 0

    if not events:
        print("No scheduler events.")
        return 0

    for evt in events:
        ts = evt.get("ts", "")
        # Format timestamp for display
        try:
            dt = datetime.datetime.fromisoformat(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            ts_str = ts[:16] if ts else "?"

        event_type = evt.get("event", "?")
        task_name = evt.get("task", "")
        session = evt.get("session", "")

        if event_type == "task_completed":
            status = evt.get("status", "?")
            duration = evt.get("duration", 0)
            summary = evt.get("summary", "")
            summary_str = f'  "{summary}"' if summary else ""
            print(f"{ts_str}  {event_type:<22} {task_name:<24} {status:<12} {duration}s{summary_str}")
        elif event_type == "task_started":
            print(f"{ts_str}  {event_type:<22} {task_name:<24} {session}")
        elif event_type == "task_skipped":
            reason = evt.get("reason", "?")
            print(f"{ts_str}  {event_type:<22} {task_name:<24} reason: {reason}")
        elif event_type == "scheduler_sleeping":
            next_task = evt.get("next_task", "?")
            sleep_s = evt.get("sleep_seconds", 0)
            print(f"{ts_str}  {event_type:<22} next: {next_task} in {int(sleep_s)}s")
        elif event_type == "scheduler_started":
            count = evt.get("task_count", 0)
            enabled = evt.get("enabled_count", 0)
            print(f"{ts_str}  {event_type:<22} {enabled}/{count} tasks enabled")
        else:
            print(f"{ts_str}  {event_type:<22} {task_name}")

    return 0


def cmd_scheduler_live(args) -> int:
    """Show live scheduler state."""
    from .scheduler import format_interval, read_live_state

    json_mode = getattr(args, 'json', False)
    watch_mode = getattr(args, 'watch', False)

    def _display_once():
        state = read_live_state()
        if not state:
            if json_mode:
                _output_json({"success": False, "error": "No live state file. Is the scheduler running?"})
            else:
                print("No live state available. Is the scheduler running?")
            return False

        if json_mode:
            _output_json({"success": True, **state})
            return True

        status = state.get("status", "unknown")
        uptime = state.get("uptime_seconds", 0)
        current = state.get("current_task")
        current_started = state.get("current_task_started")
        completed = state.get("tasks_completed", 0)
        failed = state.get("tasks_failed", 0)
        next_task = state.get("next_task")
        next_in = state.get("next_in_seconds", 0)

        print(f"Scheduler: {status} (uptime {format_interval(int(uptime))})")

        if current:
            # Calculate running time
            running_str = ""
            if current_started:
                try:
                    started_dt = datetime.datetime.fromisoformat(current_started)
                    running = int((datetime.datetime.now(datetime.timezone.utc) - started_dt).total_seconds())
                    running_str = f" (running {format_interval(running)})"
                except (ValueError, TypeError):
                    pass
            print(f"Current:   {current}{running_str}")
        else:
            print("Current:   idle")

        print(f"Completed: {completed} tasks | Failed: {failed}")

        if next_task:
            print(f"Next:      {next_task} (in {format_interval(int(next_in))})")
        elif not current:
            print("Next:      nothing due")

        return True

    if watch_mode:
        import os
        try:
            while True:
                os.system("clear")
                _display_once()
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
    else:
        _display_once()
        return 0


def cmd_scheduler_dashboard(args) -> int:
    """Generate and open a live HTML dashboard as a portal artifact."""
    no_open = getattr(args, 'no_open', False)

    html = _generate_dashboard_html()

    # Write to artifacts
    artifacts_dir = Path.home() / ".agentwire" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    dashboard_path = artifacts_dir / "scheduler-dashboard.html"
    dashboard_path.write_text(html)
    print(f"Dashboard written to {dashboard_path}")

    if not no_open:
        subprocess.run(
            ["agentwire", "open", "scheduler-dashboard.html", "--title", "Scheduler Dashboard"],
            capture_output=True,
        )

    return 0


def _generate_dashboard_html() -> str:
    """Generate a live scheduler dashboard that fetches data from REST APIs."""

    return '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Scheduler Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #1a1a2e; color: #e0e0e0; }
  h1 { color: #00d4ff; margin-bottom: 4px; }
  h2 { color: #8892b0; margin-top: 24px; margin-bottom: 8px; font-size: 1.1em; }
  .meta { color: #555; font-size: 0.82em; margin-bottom: 16px; }
  .status-bar { display: flex; gap: 24px; padding: 12px 16px; background: #16213e; border-radius: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .status-bar .item { display: flex; flex-direction: column; }
  .status-bar .label { color: #556; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.5px; }
  .status-bar .value { color: #e0e0e0; font-size: 1.1em; font-weight: 600; }
  .status-bar .value.running { color: #00c853; }
  .status-bar .value.idle { color: #8892b0; }
  .status-bar .value.stopped { color: #ff5252; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #2a2a4a; font-size: 0.9em; }
  th { background: #16213e; color: #00d4ff; font-weight: 600; position: sticky; top: 0; }
  tr:hover { background: #16213e; }
  .complete { color: #00c853; }
  .failed, .timeout { color: #ff5252; }
  .never { color: #555; }
  .lock_conflict, .incomplete { color: #ffa726; }
  .disabled { opacity: 0.4; }
  .evt-task_completed { color: #00c853; }
  .evt-task_started { color: #42a5f5; }
  .evt-task_skipped { color: #ffa726; }
  .evt-scheduler_sleeping { color: #555; }
  .evt-scheduler_started { color: #00d4ff; }
  .pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #00c853; margin-right: 6px; animation: pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
</style>
</head>
<body>
<h1>Scheduler Dashboard</h1>
<p class="meta">Live &mdash; polls every 10s, instant updates via WebSocket</p>

<div class="status-bar" id="status-bar">
  <div class="item"><span class="label">Status</span><span class="value" id="sb-status">&mdash;</span></div>
  <div class="item"><span class="label">Uptime</span><span class="value" id="sb-uptime">&mdash;</span></div>
  <div class="item"><span class="label">Current</span><span class="value" id="sb-current">&mdash;</span></div>
  <div class="item"><span class="label">Completed</span><span class="value" id="sb-completed">&mdash;</span></div>
  <div class="item"><span class="label">Failed</span><span class="value" id="sb-failed">&mdash;</span></div>
  <div class="item"><span class="label">Next</span><span class="value" id="sb-next">&mdash;</span></div>
</div>

<h2>Task Board</h2>
<table>
  <thead><tr><th>Task</th><th>Schedule</th><th>Last Run</th><th>Status</th><th>Duration</th><th>Overdue</th><th>Runs</th></tr></thead>
  <tbody id="board-body"></tbody>
</table>

<h2>Recent Events</h2>
<table>
  <thead><tr><th style="width:70px">Time</th><th style="width:160px">Event</th><th style="width:180px">Task</th><th>Details</th></tr></thead>
  <tbody id="events-body"></tbody>
</table>

<script>
const BASE = location.origin;
const WS_URL = BASE.replace(/^http/, "ws") + "/ws";

function fmtInterval(s) {
  s = Math.round(s);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return m ? h + "h" + m + "m" : h + "h";
}

function fmtTime(iso) {
  try { return new Date(iso).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}); }
  catch(e) { return iso ? iso.slice(11, 19) : "?"; }
}

function esc(s) {
  var d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

async function refreshLive() {
  try {
    var r = await fetch(BASE + "/api/scheduler/live");
    if (!r.ok) throw new Error(r.status);
    var d = await r.json();
    var el = document.getElementById("sb-status");
    if (d.current_task) { el.textContent = "running"; el.className = "value running"; }
    else if (d.status === "running") { el.innerHTML = "<span class=\"pulse\"></span>idle"; el.className = "value idle"; }
    else { el.textContent = d.status || "stopped"; el.className = "value stopped"; }
    document.getElementById("sb-uptime").textContent = fmtInterval(d.uptime_seconds || 0);
    if (d.current_task) {
      var run = "";
      if (d.current_task_started) {
        var elapsed = (Date.now() - new Date(d.current_task_started).getTime()) / 1000;
        run = " (" + fmtInterval(elapsed) + ")";
      }
      document.getElementById("sb-current").textContent = d.current_task + run;
    } else { document.getElementById("sb-current").textContent = "\u2014"; }
    document.getElementById("sb-completed").textContent = d.tasks_completed != null ? d.tasks_completed : "\u2014";
    document.getElementById("sb-failed").textContent = d.tasks_failed != null ? d.tasks_failed : "\u2014";
    if (d.next_task) {
      document.getElementById("sb-next").textContent = d.next_task + " (in " + fmtInterval(d.next_in_seconds || 0) + ")";
    } else { document.getElementById("sb-next").textContent = "\u2014"; }
  } catch(e) {
    document.getElementById("sb-status").textContent = "offline";
    document.getElementById("sb-status").className = "value stopped";
  }
}

async function refreshBoard() {
  try {
    var r = await fetch(BASE + "/api/scheduler/board");
    if (!r.ok) return;
    var d = await r.json();
    var tbody = document.getElementById("board-body");
    tbody.innerHTML = (d.tasks || []).map(function(t) {
      var cls = t.enabled ? t.last_status : "disabled";
      var label = esc(t.label) + (t.enabled ? "" : " <span style=\"color:#555\">[off]</span>");
      return "<tr class=\"" + cls + "\">" +
        "<td>" + label + "</td>" +
        "<td>" + esc(t.schedule_str || "?") + "</td>" +
        "<td>" + esc(t.last_run) + "</td>" +
        "<td class=\"" + esc(t.last_status) + "\">" + esc(t.last_status) + "</td>" +
        "<td>" + t.last_duration + "s</td>" +
        "<td>" + esc(t.overdue_str) + "</td>" +
        "<td>" + t.run_count + "</td></tr>";
    }).join("");
  } catch(e) {}
}

async function refreshEvents() {
  try {
    var r = await fetch(BASE + "/api/scheduler/events?tail=30");
    if (!r.ok) return;
    var d = await r.json();
    var evts = (d.events || []).slice().reverse();
    var tbody = document.getElementById("events-body");
    tbody.innerHTML = evts.map(function(evt) {
      var ts = fmtTime(evt.ts);
      var etype = evt.event || "?";
      var task = evt.task || "";
      var detail = "";
      if (etype === "task_completed") detail = esc(evt.status) + " \u2014 " + esc(evt.summary);
      else if (etype === "task_skipped") detail = esc(evt.reason);
      else if (etype === "scheduler_sleeping") detail = "next: " + esc(evt.next_task) + " in " + Math.round(evt.sleep_seconds || 0) + "s";
      else if (etype === "task_started") detail = esc(evt.session);
      else if (etype === "scheduler_started") detail = (evt.enabled_count || 0) + "/" + (evt.task_count || 0) + " tasks enabled";
      return "<tr><td>" + ts + "</td><td class=\"evt-" + esc(etype) + "\">" + esc(etype) + "</td><td>" + esc(task) + "</td><td>" + detail + "</td></tr>";
    }).join("");
  } catch(e) {}
}

function connectWS() {
  try {
    var ws = new WebSocket(WS_URL);
    ws.onmessage = function(e) {
      try {
        var msg = JSON.parse(e.data);
        if (msg.type === "scheduler_update") { refreshLive(); refreshBoard(); refreshEvents(); }
      } catch(ex) {}
    };
    ws.onclose = function() { setTimeout(connectWS, 5000); };
    ws.onerror = function() { ws.close(); };
  } catch(e) {}
}

refreshLive(); refreshBoard(); refreshEvents();
setInterval(refreshLive, 10000);
setInterval(refreshBoard, 10000);
setInterval(refreshEvents, 10000);
connectWS();
</script>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Overnight session queue commands
# ---------------------------------------------------------------------------

OVERNIGHT_SESSION = "agentwire-overnight"


def cmd_overnight_prepare(args) -> int:
    """Queue an active session for overnight autonomous execution."""
    from .overnight import prepare_item

    source = args.source
    task_desc = args.task
    priority = getattr(args, "priority", 50)
    session_type = getattr(args, "type", None)
    json_mode = getattr(args, "json", False)

    try:
        item = prepare_item(source, task_desc, priority=priority, session_type=session_type)
    except RuntimeError as e:
        return _output_result(False, json_mode, str(e))

    if json_mode:
        _output_json({
            "success": True,
            "id": item.id,
            "session": item.session,
            "work_branch": item.work_branch,
            "description": item.description,
            "status": item.status,
        })
    else:
        print(f"Queued for overnight execution:")
        print(f"  ID: {item.id}")
        print(f"  Session: {item.session}")
        print(f"  Branch: {item.work_branch}")
        print(f"  Source: {item.source_session} ({item.parent_branch} @ {item.parent_commit[:8]})")
        print(f"  Priority: {item.priority}")

    return 0


def cmd_overnight_list(args) -> int:
    """List overnight queue items."""
    from .overnight import load_queue, load_done

    json_mode = getattr(args, "json", False)
    show_all = getattr(args, "all", False)

    items = load_queue()
    if show_all:
        items.extend(load_done())

    items.sort(key=lambda i: (
        0 if i.status == "running" else 1 if i.status == "queued" else 2,
        i.priority,
        i.prepared_at,
    ))

    if json_mode:
        _output_json({"success": True, "items": [i.to_dict() for i in items]})
        return 0

    if not items:
        print("No overnight items in queue.")
        return 0

    for item in items:
        status_icon = {
            "queued": "  ",
            "running": ">>",
            "complete": "OK",
            "failed": "XX",
            "cancelled": "--",
        }.get(item.status, "??")
        pr = f" -> {item.pr_url}" if item.pr_url else ""
        print(f"  [{status_icon}] {item.id}  p={item.priority}  {item.description}{pr}")

    return 0


def cmd_overnight_status(args) -> int:
    """Show overnight orchestrator state and queue summary."""
    from .overnight import load_queue, read_live_state, in_overnight_window

    json_mode = getattr(args, "json", False)

    items = load_queue()
    queued = [i for i in items if i.status == "queued"]
    running_items = [i for i in items if i.status == "running"]

    daemon_running = tmux_session_exists(OVERNIGHT_SESSION)
    live = read_live_state()

    from .config import get_config
    config = get_config().overnight
    in_window = in_overnight_window(config)

    if json_mode:
        _output_json({
            "success": True,
            "running": daemon_running,
            "in_window": in_window,
            "window": f"{config.window_start}-{config.window_end}",
            "queued": len(queued),
            "active": len(running_items),
            "active_items": [i.to_dict() for i in running_items],
            "queued_items": [i.to_dict() for i in queued],
        })
        return 0

    status = "running" if daemon_running else "stopped"
    window_status = "ACTIVE" if in_window else "outside"
    print(f"Overnight orchestrator: {status}")
    print(f"Window: {config.window_start}-{config.window_end} ({window_status})")
    print(f"Queue: {len(queued)} queued, {len(running_items)} running")

    if running_items:
        for item in running_items:
            print(f"  >> {item.id}: {item.description}")
    if queued:
        for item in queued:
            print(f"     {item.id}: {item.description} (p={item.priority})")

    return 0


def cmd_overnight_cancel(args) -> int:
    """Cancel a queued overnight item."""
    from .overnight import load_item, save_item, delete_item

    item_id = args.id
    json_mode = getattr(args, "json", False)

    item = load_item(item_id)
    if not item:
        return _output_result(False, json_mode, f"Item '{item_id}' not found")

    if item.status == "running":
        # Kill the running session
        if tmux_session_exists(item.session):
            subprocess.run(["tmux", "kill-session", "-t", item.session], capture_output=True)
        # Checkout parent branch
        subprocess.run(
            ["git", "-C", item.project_path, "checkout", item.parent_branch],
            capture_output=True, timeout=15,
        )

    item.status = "cancelled"
    delete_item(item_id)

    if json_mode:
        _output_json({"success": True, "id": item_id, "status": "cancelled"})
    else:
        print(f"Cancelled: {item_id}")

    return 0


def cmd_overnight_priority(args) -> int:
    """Update priority of a queued overnight item."""
    from .overnight import load_item, save_item

    item_id = args.id
    priority = args.priority
    json_mode = getattr(args, "json", False)

    item = load_item(item_id)
    if not item:
        return _output_result(False, json_mode, f"Item '{item_id}' not found")

    if item.status != "queued":
        return _output_result(False, json_mode, f"Can only change priority of queued items (status: {item.status})")

    item.priority = priority
    save_item(item)

    if json_mode:
        _output_json({"success": True, "id": item_id, "priority": priority})
    else:
        print(f"Updated priority: {item_id} -> {priority}")

    return 0


def cmd_overnight_start(args) -> int:
    """Start the overnight orchestrator daemon in a tmux session."""
    if not _check_tmux_installed():
        return 1

    if tmux_session_exists(OVERNIGHT_SESSION):
        print(f"Overnight orchestrator already running in '{OVERNIGHT_SESSION}'")
        print("Attaching... (Ctrl+B D to detach)")
        subprocess.run(["tmux", "attach-session", "-t", OVERNIGHT_SESSION])
        return 0

    print(f"Starting overnight orchestrator in '{OVERNIGHT_SESSION}'...")
    subprocess.run(["tmux", "new-session", "-d", "-s", OVERNIGHT_SESSION])
    subprocess.run([
        "tmux", "send-keys", "-t", OVERNIGHT_SESSION,
        "agentwire overnight serve", "Enter",
    ])

    print("Attaching... (Ctrl+B D to detach)")
    subprocess.run(["tmux", "attach-session", "-t", OVERNIGHT_SESSION])
    return 0


def cmd_overnight_serve(args) -> int:
    """Run the overnight orchestrator loop in foreground."""
    from .overnight import run_overnight_loop

    run_overnight_loop()
    return 0


def cmd_overnight_stop(args) -> int:
    """Stop the overnight orchestrator daemon."""
    if not tmux_session_exists(OVERNIGHT_SESSION):
        print("Overnight orchestrator is not running.")
        return 1

    subprocess.run(["tmux", "kill-session", "-t", OVERNIGHT_SESSION])
    print("Overnight orchestrator stopped.")
    return 0


def cmd_overnight_report(args) -> int:
    """Generate a morning report from completed overnight items."""
    from .overnight import load_done

    json_mode = getattr(args, "json", False)
    items = load_done()

    if not items:
        if json_mode:
            _output_json({"success": True, "items": [], "message": "No completed items"})
        else:
            print("No completed overnight items.")
        return 0

    # Sort by completed_at descending
    items.sort(key=lambda i: i.completed_at or "", reverse=True)

    if json_mode:
        _output_json({"success": True, "items": [i.to_dict() for i in items]})
        return 0

    print("Overnight Report")
    print("=" * 60)
    for item in items:
        status_icon = "OK" if item.status == "complete" else "XX"
        pr = item.pr_url or "no PR"
        print(f"  [{status_icon}] {item.id}: {item.description}")
        print(f"       Branch: {item.work_branch}")
        print(f"       PR: {pr}")
        if item.summary:
            print(f"       Summary: {item.summary}")
        print()

    return 0


class VersionAction(argparse.Action):
    """Custom version action that checks Python version and pip environment."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        # Print version
        print(f"agentwire {__version__}")
        print(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

        # Check version compatibility
        version_ok = check_python_version()
        env_ok = check_pip_environment()

        if version_ok and env_ok:
            print("\n✓ System is ready for AgentWire")
        else:
            print("\n⚠️  Please resolve the issues above before installing/running AgentWire")

        parser.exit()


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="agentwire",
        description="Multi-session voice web interface for AI coding agents.",
    )
    parser.add_argument(
        "--version",
        action=VersionAction,
        help="Show version and check system compatibility",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # === init command ===
    init_parser = subparsers.add_parser("init", help="Interactive setup wizard")
    init_parser.add_argument(
        "--quick", action="store_true",
        help="Quick mode: skip agentwire setup at end"
    )
    init_parser.set_defaults(func=cmd_init)

    # === portal command group ===
    portal_parser = subparsers.add_parser("portal", help="Manage the web portal")
    portal_subparsers = portal_parser.add_subparsers(dest="portal_command")

    # portal start
    portal_start = portal_subparsers.add_parser(
        "start", help="Start portal in tmux session"
    )
    portal_start.add_argument("--config", type=Path, help="Config file path")
    portal_start.add_argument("--port", type=int, help="Override port")
    portal_start.add_argument("--host", type=str, help="Override host")
    portal_start.add_argument("--no-tts", action="store_true", help="Disable TTS")
    portal_start.add_argument("--no-stt", action="store_true", help="Disable STT")
    portal_start.add_argument("--dev", action="store_true",
                              help="Run from source (uv run) - picks up code changes")
    portal_start.set_defaults(func=cmd_portal_start)

    # portal serve (run in foreground)
    portal_serve = portal_subparsers.add_parser(
        "serve", help="Run portal in foreground"
    )
    portal_serve.add_argument("--config", type=Path, help="Config file path")
    portal_serve.add_argument("--port", type=int, help="Override port")
    portal_serve.add_argument("--host", type=str, help="Override host")
    portal_serve.add_argument("--no-tts", action="store_true", help="Disable TTS")
    portal_serve.add_argument("--no-stt", action="store_true", help="Disable STT")
    portal_serve.set_defaults(func=cmd_portal_serve)

    # portal stop
    portal_stop = portal_subparsers.add_parser("stop", help="Stop the portal")
    portal_stop.set_defaults(func=cmd_portal_stop)

    # portal status
    portal_status = portal_subparsers.add_parser("status", help="Check portal status")
    portal_status.add_argument("--json", action="store_true", help="Output JSON")
    portal_status.set_defaults(func=cmd_portal_status)

    # portal restart
    portal_restart = portal_subparsers.add_parser("restart", help="Restart the portal (stop + start)")
    portal_restart.add_argument("--config", type=Path, help="Config file path")
    portal_restart.add_argument("--port", type=int, help="Override port")
    portal_restart.add_argument("--host", type=str, help="Override host")
    portal_restart.add_argument("--no-tts", action="store_true", help="Disable TTS")
    portal_restart.add_argument("--no-stt", action="store_true", help="Disable STT")
    portal_restart.add_argument("--dev", action="store_true",
                                help="Run from source (uv run) - picks up code changes")
    portal_restart.set_defaults(func=cmd_portal_restart)

    # portal generate-certs
    portal_certs = portal_subparsers.add_parser(
        "generate-certs", help="Generate SSL certificates"
    )
    portal_certs.set_defaults(func=cmd_generate_certs)

    # === tts command group ===
    tts_parser = subparsers.add_parser("tts", help="Manage TTS server")
    tts_subparsers = tts_parser.add_subparsers(dest="tts_command")

    # tts start
    tts_start = tts_subparsers.add_parser("start", help="Start TTS server in tmux")
    tts_start.add_argument("--port", type=int, help="Server port (default: 8100)")
    tts_start.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    tts_start.add_argument("--backend", type=str,
                           choices=["kokoro", "chatterbox", "chatterbox-streaming", "qwen-base-0.6b", "qwen-base-1.7b", "qwen-design", "qwen-custom", "zonos-transformer", "zonos-hybrid"],
                           help="TTS backend (default: chatterbox)")
    tts_start.set_defaults(func=cmd_tts_start)

    # tts serve (run in foreground)
    tts_serve = tts_subparsers.add_parser("serve", help="Run TTS server in foreground")
    tts_serve.add_argument("--port", type=int, help="Server port (default: 8100)")
    tts_serve.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    tts_serve.add_argument("--backend", type=str,
                           choices=["kokoro", "chatterbox", "chatterbox-streaming", "qwen-base-0.6b", "qwen-base-1.7b", "qwen-design", "qwen-custom", "zonos-transformer", "zonos-hybrid"],
                           help="TTS backend (default: chatterbox)")
    tts_serve.add_argument("--venv", type=str,
                           choices=["kokoro", "chatterbox", "qwen", "zonos"],
                           help="Which venv family is running (for hot-swap detection)")
    tts_serve.set_defaults(func=cmd_tts_serve)

    # tts stop
    tts_stop = tts_subparsers.add_parser("stop", help="Stop TTS server")
    tts_stop.set_defaults(func=cmd_tts_stop)

    # tts restart
    tts_restart = tts_subparsers.add_parser("restart", help="Restart TTS server (with optional venv switch)")
    tts_restart.add_argument("--port", type=int, help="Server port (default: 8100)")
    tts_restart.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    tts_restart.add_argument("--backend", type=str,
                             choices=["kokoro", "chatterbox", "chatterbox-streaming", "qwen-base-0.6b", "qwen-base-1.7b", "qwen-design", "qwen-custom", "zonos-transformer", "zonos-hybrid"],
                             help="TTS backend")
    tts_restart.add_argument("--venv", type=str,
                             choices=["kokoro", "chatterbox", "qwen", "zonos"],
                             help="Force specific venv family")
    tts_restart.set_defaults(func=cmd_tts_restart)

    # tts status
    tts_status = tts_subparsers.add_parser("status", help="Check TTS status")
    tts_status.add_argument("--json", action="store_true", help="Output JSON")
    tts_status.set_defaults(func=cmd_tts_status)

    # === stt command group ===
    stt_parser = subparsers.add_parser("stt", help="Manage STT server (native Whisper)")
    stt_subparsers = stt_parser.add_subparsers(dest="stt_command")

    # stt start
    stt_start = stt_subparsers.add_parser("start", help="Start STT server in tmux")
    stt_start.add_argument("--port", type=int, help="Server port (default: 8101)")
    stt_start.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    stt_start.add_argument("--model", type=str, help="Whisper model (tiny/base/small/medium/large-v3)")
    stt_start.add_argument("--backend", type=str, help="STT backend: auto (default), moonshine, whisper")
    stt_start.set_defaults(func=cmd_stt_start)

    # stt serve
    stt_serve = stt_subparsers.add_parser("serve", help="Run STT server in foreground")
    stt_serve.add_argument("--port", type=int, help="Server port (default: 8101)")
    stt_serve.add_argument("--host", type=str, help="Server host (default: 0.0.0.0)")
    stt_serve.add_argument("--model", type=str, help="Whisper model (tiny/base/small/medium/large-v3)")
    stt_serve.add_argument("--backend", type=str, help="STT backend: auto (default), moonshine, whisper")
    stt_serve.set_defaults(func=cmd_stt_serve)

    # stt stop
    stt_stop = stt_subparsers.add_parser("stop", help="Stop STT server")
    stt_stop.set_defaults(func=cmd_stt_stop)

    # stt status
    stt_status = stt_subparsers.add_parser("status", help="Check STT status")
    stt_status.add_argument("--json", action="store_true", help="Output JSON")
    stt_status.set_defaults(func=cmd_stt_status)

    # === telegram command group ===
    telegram_parser = subparsers.add_parser("telegram", help="Manage Telegram bridge")
    telegram_subparsers = telegram_parser.add_subparsers(dest="telegram_command")

    # telegram start
    telegram_start = telegram_subparsers.add_parser("start", help="Start Telegram bridge in tmux")
    telegram_start.set_defaults(func=cmd_telegram_start)

    # telegram serve (foreground)
    telegram_serve = telegram_subparsers.add_parser("serve", help="Run Telegram bridge in foreground")
    telegram_serve.set_defaults(func=cmd_telegram_serve)

    # telegram stop
    telegram_stop = telegram_subparsers.add_parser("stop", help="Stop Telegram bridge")
    telegram_stop.set_defaults(func=cmd_telegram_stop)

    # telegram status
    telegram_status = telegram_subparsers.add_parser("status", help="Check Telegram bridge status")
    telegram_status.add_argument("--json", action="store_true", help="Output JSON")
    telegram_status.set_defaults(func=cmd_telegram_status)

    # telegram notify
    telegram_notify = telegram_subparsers.add_parser("notify", help="Send a Telegram notification")
    telegram_notify.add_argument("--body", "-b", type=str, help="Message body (or pipe via stdin)")
    telegram_notify.add_argument("--chat-id", type=int, help="Target chat ID (default: first allowed user)")
    telegram_notify.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    telegram_notify.set_defaults(func=cmd_telegram_notify)

    # === discord command group ===
    discord_parser = subparsers.add_parser("discord", help="Manage Discord bridge")
    discord_subparsers = discord_parser.add_subparsers(dest="discord_command")

    discord_start = discord_subparsers.add_parser("start", help="Start Discord bridge in tmux")
    discord_start.set_defaults(func=cmd_discord_start)

    discord_serve = discord_subparsers.add_parser("serve", help="Run Discord bridge in foreground")
    discord_serve.set_defaults(func=cmd_discord_serve)

    discord_stop = discord_subparsers.add_parser("stop", help="Stop Discord bridge")
    discord_stop.set_defaults(func=cmd_discord_stop)

    discord_status_cmd = discord_subparsers.add_parser("status", help="Check Discord bridge status")
    discord_status_cmd.add_argument("--json", action="store_true", help="Output JSON")
    discord_status_cmd.set_defaults(func=cmd_discord_status)

    # === slack command group ===
    slack_parser = subparsers.add_parser("slack", help="Manage Slack bridge")
    slack_subparsers = slack_parser.add_subparsers(dest="slack_command")

    slack_start = slack_subparsers.add_parser("start", help="Start Slack bridge in tmux")
    slack_start.set_defaults(func=cmd_slack_start)

    slack_serve = slack_subparsers.add_parser("serve", help="Run Slack bridge in foreground")
    slack_serve.set_defaults(func=cmd_slack_serve)

    slack_stop = slack_subparsers.add_parser("stop", help="Stop Slack bridge")
    slack_stop.set_defaults(func=cmd_slack_stop)

    slack_status_cmd = slack_subparsers.add_parser("status", help="Check Slack bridge status")
    slack_status_cmd.add_argument("--json", action="store_true", help="Output JSON")
    slack_status_cmd.set_defaults(func=cmd_slack_status)

    # === quo command ===
    from agentwire.channels.quo import cmd_quo
    quo_parser = subparsers.add_parser("quo", help="Send SMS via Quo (OpenPhone)")
    quo_parser.add_argument("--body", "-b", type=str, help="Message body (or pipe via stdin)")
    quo_parser.add_argument("--to", type=str, help="Recipient phone number (+E.164 format)")
    quo_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    quo_parser.set_defaults(func=cmd_quo)

    # === sms command ===
    from agentwire.channels.sms import cmd_sms
    sms_parser = subparsers.add_parser("sms", help="Send SMS via Twilio")
    sms_parser.add_argument("--body", "-b", type=str, help="Message body (or pipe via stdin)")
    sms_parser.add_argument("--to", type=str, help="Recipient phone number (+E.164 format)")
    sms_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    sms_parser.set_defaults(func=cmd_sms)

    # === webhook command ===
    from agentwire.channels.webhook import cmd_webhook
    webhook_parser = subparsers.add_parser("webhook", help="Send message via HTTP webhook")
    webhook_parser.add_argument("--body", "-b", type=str, help="Message body (or pipe via stdin)")
    webhook_parser.add_argument("--url", type=str, help="Target URL (overrides config)")
    webhook_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    webhook_parser.set_defaults(func=cmd_webhook)

    # === tunnels command group ===
    tunnels_parser = subparsers.add_parser("tunnels", help="Manage SSH tunnels for service routing")
    tunnels_subparsers = tunnels_parser.add_subparsers(dest="tunnels_command")

    # tunnels up
    tunnels_up = tunnels_subparsers.add_parser("up", help="Create all required tunnels")
    tunnels_up.set_defaults(func=cmd_tunnels_up)

    # tunnels down
    tunnels_down = tunnels_subparsers.add_parser("down", help="Tear down all tunnels")
    tunnels_down.set_defaults(func=cmd_tunnels_down)

    # tunnels status
    tunnels_status = tunnels_subparsers.add_parser("status", help="Show tunnel health")
    tunnels_status.set_defaults(func=cmd_tunnels_status)

    # tunnels check
    tunnels_check = tunnels_subparsers.add_parser("check", help="Verify tunnels are working")
    tunnels_check.set_defaults(func=cmd_tunnels_check)

    # === say command ===
    say_parser = subparsers.add_parser("say", help="Speak text via TTS")
    say_parser.add_argument("text", nargs="*", help="Text to speak")
    say_parser.add_argument("-v", "--voice", type=str, help="Voice name")
    say_parser.add_argument("-s", "--session", type=str, help="Session name (auto-detected from .agentwire.yml or tmux)")
    say_parser.add_argument("--exaggeration", type=float, help="Voice exaggeration (0-1, Chatterbox)")
    say_parser.add_argument("--cfg", type=float, help="CFG weight (0-1, Chatterbox)")
    say_parser.add_argument("--backend", type=str, help="TTS backend (chatterbox, qwen-base-1.7b, qwen-design, qwen-custom)")
    say_parser.add_argument("--instruct", type=str, help="Emotion/style instruction (qwen-design, qwen-custom)")
    say_parser.add_argument("--language", type=str, default="English", help="Language (default: English)")
    say_parser.add_argument("--stream", action="store_true", help="Use streaming mode (if backend supports)")
    say_parser.add_argument("--notify", type=str, metavar="SESSION", help="Also notify this session (sends message as input)")
    say_parser.add_argument("--no-auto-notify", action="store_true", help="Disable auto-notify to pane 0 when in worker pane")
    say_parser.set_defaults(func=cmd_say)

    # === reply command ===
    reply_parser = subparsers.add_parser("reply", help="Reply to channel user (Discord, Slack, Telegram)")
    reply_parser.add_argument("text", nargs="*", help="Reply message")
    reply_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress output")
    reply_parser.set_defaults(func=cmd_reply)

    # === notify command (worker→parent) ===
    notify_cmd_parser = subparsers.add_parser("notify-parent", help="Notify parent session (worker→orchestrator)")
    notify_cmd_parser.add_argument("text", nargs="*", help="Notification message")
    notify_cmd_parser.add_argument("--to", type=str, metavar="SESSION", help="Target session (default: parent from .agentwire.yml)")
    notify_cmd_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress output")
    notify_cmd_parser.set_defaults(func=cmd_notify_parent)

    # === open command (artifact windows) ===
    open_parser = subparsers.add_parser("open", help="Open a URL or local file as an artifact window in the portal")
    open_parser.add_argument("url", help="URL or filename to open (filenames served from ~/.agentwire/artifacts/)")
    open_parser.add_argument("--title", "-t", type=str, default="Artifact", help="Window title")
    open_parser.add_argument("--artifact-id", type=str, help="Unique window ID (auto-generated if omitted)")
    open_parser.add_argument("--json", action="store_true", help="Output JSON")
    open_parser.set_defaults(func=cmd_open)

    # === channels command ===
    channels_parser = subparsers.add_parser("channels", help="Manage communication channels")
    channels_sub = channels_parser.add_subparsers(dest="channels_cmd")
    channels_list_parser = channels_sub.add_parser("list", help="List all registered channels")
    channels_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    channels_list_parser.set_defaults(func=cmd_channels_list)
    channels_parser.set_defaults(func=cmd_channels_list)

    # === email command ===
    from agentwire.channels.email import cmd_email
    email_parser = subparsers.add_parser("email", help="Send branded email notification via Resend")
    email_parser.add_argument(
        "--to", action="append", default=None,
        help="Recipient email. Repeat or pass comma-separated for multiple recipients (default: from config).",
    )
    email_parser.add_argument("--subject", "-s", type=str, help="Email subject")
    email_parser.add_argument("--body", "-b", type=str, help="Email body - markdown supported (or pipe via stdin)")
    email_parser.add_argument("--attach", "-a", type=str, action="append", help="Attach file (can use multiple times)")
    email_parser.add_argument("--plain", action="store_true", help="Send plain text only (no HTML template)")
    email_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress success output")
    email_parser.set_defaults(func=cmd_email)

    # === brave command ===
    from agentwire.search import cmd_brave
    brave_parser = subparsers.add_parser(
        "brave",
        help="Brave Search helper — run a web search and print results optimized for LLM consumption.",
    )
    brave_parser.add_argument("query", nargs="+", help="Search query (all remaining args joined with spaces)")
    brave_parser.add_argument("--count", "-n", type=int, default=10, help="Max results (1-20, default 10)")
    brave_parser.add_argument(
        "--freshness", "-f", type=str, default="pd",
        choices=["pd", "pw", "pm", "py"],
        help="Time window: pd=past day (default), pw=past week, pm=past month, py=past year",
    )
    brave_parser.add_argument("--json", action="store_true", help="Output raw JSON instead of compact text")
    brave_parser.set_defaults(func=cmd_brave)

    # === fetch command ===
    from agentwire.fetch import cmd_fetch
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch a URL via Jina Reader — handles JS-rendered pages, returns clean markdown.",
    )
    fetch_parser.add_argument("url", help="URL to fetch")
    fetch_parser.add_argument(
        "--limit", "-l", type=int, default=8000,
        help="Max characters to return (default: 8000, 0 = no limit)",
    )
    fetch_parser.set_defaults(func=cmd_fetch)

    # === notify command ===
    notify_parser = subparsers.add_parser("notify", help="Notify portal of session/pane state changes")
    notify_parser.add_argument(
        "event",
        help="Event type: session_closed, session_created, pane_died, pane_created, "
             "client_attached, client_detached, session_renamed, pane_focused, window_activity"
    )
    notify_parser.add_argument("-s", "--session", help="Session name")
    notify_parser.add_argument("--pane", type=int, help="Pane index (for pane events)")
    notify_parser.add_argument("--pane-id", help="Pane ID from tmux (for pane events via hooks)")
    notify_parser.add_argument("--old-name", help="Old session name (for session_renamed)")
    notify_parser.add_argument("--new-name", help="New session name (for session_renamed)")
    notify_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notify_parser.set_defaults(func=cmd_notify)

    # === send command ===
    send_parser = subparsers.add_parser("send", help="Send prompt to a session or pane (adds Enter)")
    send_parser.add_argument("-s", "--session", help="Target session (supports session@machine)")
    send_parser.add_argument("--pane", type=int, help="Target pane index (auto-detects session)")
    send_parser.add_argument("prompt", nargs="*", help="Prompt to send")
    send_parser.add_argument("--json", action="store_true", help="Output as JSON")
    send_parser.set_defaults(func=cmd_send)

    # === send-keys command ===
    send_keys_parser = subparsers.add_parser(
        "send-keys", help="Send raw keys to a session (with pause between groups)"
    )
    send_keys_parser.add_argument("-s", "--session", required=True, help="Target session (supports session@machine)")
    send_keys_parser.add_argument("keys", nargs="*", help="Key groups to send (e.g., 'hello world' Enter)")
    send_keys_parser.set_defaults(func=cmd_send_keys)

    # === list command (top-level) ===
    list_parser = subparsers.add_parser("list", help="List panes (in tmux) or sessions")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")
    list_parser.add_argument("--local", action="store_true", help="Only show local sessions")
    list_parser.add_argument("--remote", action="store_true", help="Only show remote sessions")
    list_parser.add_argument("--machine", help="Filter by specific machine ID")
    list_parser.add_argument("--sessions", action="store_true", help="Show sessions instead of panes")
    list_parser.set_defaults(func=cmd_list)

    # === new command (top-level) ===
    # === repl command (top-level, internal — invoked by build_agent_command) ===
    # Users spawn it via `agentwire new -s <name> --type sdk-bypass`; this is
    # the process that runs inside the tmux pane. See docs/missions/agentwire-repl.md.
    repl_parser = subparsers.add_parser(
        "repl",
        help="Agentwire REPL (internal — runs inside tmux pane for sdk-* session types)",
    )
    repl_parser.add_argument(
        "--mode", choices=["bypass", "prompted", "restricted"], default="bypass",
        help="Permission mode (bypass=run tools without asking, prompted=ask, restricted=read-only)",
    )
    repl_parser.add_argument("--model", default=None, help="Model override (default: claude-opus-4-7)")
    repl_parser.add_argument(
        "-p", "--print", dest="print", default=None, metavar="PROMPT",
        help="One-shot mode: run PROMPT, emit output, exit (no interactive loop)",
    )
    repl_parser.add_argument(
        "--append-system-prompt", dest="append_system_prompt", default=None, metavar="TEXT",
        help="Additional system prompt content (appended after base prompt)",
    )
    repl_parser.add_argument(
        "--session-name", dest="session_name", default=None, metavar="NAME",
        help="Transcript directory name under ~/.agentwire/sessions/repl/ (default: auto-generated timestamp+hash)",
    )
    repl_parser.add_argument(
        "--resume", dest="resume", default=None, metavar="NAME",
        help="Resume the saved session NAME (reuses its sdk_session_id to continue the prior conversation)",
    )
    repl_parser.add_argument(
        "--role", dest="roles", action="append", metavar="NAME",
        help="Role name to compose into the system prompt (repeatable). Overrides .agentwire.yml roles.",
    )
    repl_parser.add_argument(
        "--view", choices=["chat", "fanout"], default="chat",
        help="UI view: 'chat' (default, single conversation) or 'fanout' (N parallel columns).",
    )
    repl_parser.add_argument(
        "--cols", type=int, default=3, metavar="N",
        help="Column count for --view fanout (2-6, default 3).",
    )
    repl_parser.add_argument(
        "--col-model", dest="col_models", action="append", metavar="N=MODEL",
        help="Per-column model override for --view fanout (e.g. --col-model 0=claude-opus-4-7 --col-model 1=claude-sonnet-4-6). Repeatable.",
    )
    repl_parser.add_argument(
        "--col-effort", dest="col_efforts", action="append", metavar="N=EFFORT",
        help="Per-column effort override for --view fanout (e.g. --col-effort 0=max --col-effort 1=high). Repeatable.",
    )
    repl_parser.add_argument(
        "--col-role", dest="col_roles", action="append", metavar="N=ROLE[,ROLE...]",
        help="Per-column role override for --view fanout (e.g. --col-role 0=skeptic --col-role 1=optimist,explainer). Repeatable.",
    )
    repl_parser.set_defaults(func=cmd_repl)

    new_parser = subparsers.add_parser("new", help="Create new Claude Code session")
    new_parser.add_argument("-s", "--session", required=True, help="Session name (project, project/branch, or project/branch@machine)")
    new_parser.add_argument("-p", "--path", help="Working directory (default: ~/projects/<name>)")
    new_parser.add_argument("-f", "--force", action="store_true", help="Replace existing session")
    # Session type
    new_parser.add_argument("--type", help="Session type (bare, claude-bypass, claude-prompted, claude-restricted, pi-<provider>, pi-<provider>-restricted, pi-<provider>-readonly, standard, worker, voice) — e.g. pi-zai, pi-deepseek")
    # Roles
    new_parser.add_argument("--roles", help="Comma-separated list of roles (preserves existing config, defaults to agentwire for new projects)")
    new_parser.add_argument("--model", help="Model override (e.g., haiku, sonnet, opus)")
    new_parser.add_argument("--persist", action="store_true", help="Write --type/--roles to .agentwire.yml (default: session-level override only)")
    new_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var via `tmux set-environment` (repeatable, keeps secrets out of `ps`)")
    new_parser.add_argument("--json", action="store_true", help="Output as JSON")
    new_parser.set_defaults(func=cmd_new)

    # === output command (top-level) ===
    output_parser = subparsers.add_parser("output", help="Read session or pane output")
    output_parser.add_argument("-s", "--session", help="Session name (supports session@machine)")
    output_parser.add_argument("--pane", type=int, help="Target pane index (auto-detects session)")
    output_parser.add_argument("-n", "--lines", type=int, default=50, help="Lines to show (default: 50)")
    output_parser.add_argument("--json", action="store_true", help="Output as JSON")
    output_parser.set_defaults(func=cmd_output)

    # === info command (top-level) ===
    info_parser = subparsers.add_parser("info", help="Get session information (cwd, panes, etc.)")
    info_parser.add_argument("-s", "--session", required=True, help="Session name (supports session@machine)")
    info_parser.add_argument("--json", action="store_true", default=True, help="Output as JSON (default)")
    info_parser.add_argument("--no-json", dest="json", action="store_false", help="Human-readable output")
    info_parser.set_defaults(func=cmd_info)

    # === kill command (top-level) ===
    kill_parser = subparsers.add_parser("kill", help="Kill a session or pane (clean shutdown)")
    kill_parser.add_argument("-s", "--session", help="Session name (supports session@machine)")
    kill_parser.add_argument("--pane", type=int, help="Target pane index (auto-detects session)")
    kill_parser.add_argument("--json", action="store_true", help="Output as JSON")
    kill_parser.set_defaults(func=cmd_kill)

    # === spawn command (top-level) ===
    spawn_parser = subparsers.add_parser("spawn", help="Spawn a worker pane in current session")
    spawn_parser.add_argument("-s", "--session", help="Target session (default: auto-detect)")
    spawn_parser.add_argument("--cwd", help="Working directory (default: current)")
    spawn_parser.add_argument("--branch", "-b", help="Create worktree on this branch for isolated commits")
    spawn_parser.add_argument("--type", help="Session type (claude-bypass, claude-prompted, claude-restricted, pi-<provider>, pi-<provider>-restricted, pi-<provider>-readonly) — e.g. pi-zai, pi-deepseek")
    spawn_parser.add_argument("--roles", default="worker", help="Comma-separated roles (default: worker)")
    spawn_parser.add_argument("--no-wait", action="store_true", help="Don't wait for worker to be ready (default: wait up to 30s)")
    spawn_parser.add_argument("--timeout", type=int, default=30, help="Seconds to wait for worker ready (default: 30)")
    spawn_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var onto parent session (repeatable)")
    spawn_parser.add_argument("--json", action="store_true", help="Output as JSON")
    spawn_parser.set_defaults(func=cmd_spawn)

    # === split command (top-level) ===
    split_parser = subparsers.add_parser("split", help="Add terminal pane(s) with even vertical layout")
    split_parser.add_argument("-n", "--count", type=int, default=1, help="Number of panes to add (default: 1)")
    split_parser.add_argument("-s", "--session", help="Target session (default: auto-detect)")
    split_parser.add_argument("--cwd", help="Working directory (default: current)")
    split_parser.set_defaults(func=cmd_split)

    # === detach command (top-level) ===
    detach_parser = subparsers.add_parser("detach", help="Move a pane to its own session")
    detach_parser.add_argument("--pane", type=int, required=True, help="Pane index to detach")
    detach_parser.add_argument("-s", "--session", required=True, help="Target session name (created if doesn't exist)")
    detach_parser.add_argument("--source", help="Source session (default: auto-detect)")
    detach_parser.set_defaults(func=cmd_detach)

    # === jump command (top-level) ===
    jump_parser = subparsers.add_parser("jump", help="Jump to (focus) a specific pane")
    jump_parser.add_argument("-s", "--session", help="Target session (default: auto-detect)")
    jump_parser.add_argument("--pane", type=int, required=True, help="Pane index to focus")
    jump_parser.add_argument("--json", action="store_true", help="Output as JSON")
    jump_parser.set_defaults(func=cmd_jump)

    # === resize command (top-level) ===
    resize_parser = subparsers.add_parser("resize", help="Resize window to fit largest client")
    resize_parser.add_argument("-s", "--session", help="Target session (default: auto-detect)")
    resize_parser.add_argument("--json", action="store_true", help="Output as JSON")
    resize_parser.set_defaults(func=cmd_resize)

    # === recreate command (top-level) ===
    recreate_parser = subparsers.add_parser("recreate", help="Destroy and recreate session with fresh worktree")
    recreate_parser.add_argument("-s", "--session", required=True, help="Session name (project/branch or project/branch@machine)")
    # Session type
    recreate_parser.add_argument("--type", help="Session type (bare, claude-bypass, claude-prompted, claude-restricted, pi-<provider>, pi-<provider>-restricted, pi-<provider>-readonly, standard, worker, voice) — e.g. pi-zai, pi-deepseek")
    recreate_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var via `tmux set-environment` (repeatable)")
    recreate_parser.add_argument("--json", action="store_true", help="Output as JSON")
    recreate_parser.set_defaults(func=cmd_recreate)

    # === fork command (top-level) ===
    fork_parser = subparsers.add_parser("fork", help="Fork a session into a new worktree")
    fork_parser.add_argument("-s", "--source", required=True, help="Source session (project or project/branch)")
    fork_parser.add_argument("-t", "--target", required=True, help="Target session (must include branch: project/new-branch)")
    # Session type
    fork_parser.add_argument("--type", help="Session type (bare, claude-bypass, claude-prompted, claude-restricted, pi-<provider>, pi-<provider>-restricted, pi-<provider>-readonly, standard, worker, voice) — e.g. pi-zai, pi-deepseek")
    fork_parser.add_argument("--commit", metavar="REF", help="Fork from this commit/ref instead of HEAD (e.g. abc123, main~5)")
    fork_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var via `tmux set-environment` (repeatable)")
    fork_parser.add_argument("--json", action="store_true", help="Output as JSON")
    fork_parser.set_defaults(func=cmd_fork)

    # === worktree command ===
    wt_parser = subparsers.add_parser("worktree", help="Create a git worktree + session in one command")
    wt_parser.add_argument("name", help="Name (becomes branch name in default mode, session suffix always)")
    wt_parser.add_argument("--base", "-b", help="Base branch to fork from (default: from config or 'main')")
    wt_parser.add_argument("--current", "-c", action="store_true", help="Fork from the repo's current branch instead of --base")
    wt_parser.add_argument("--existing", "-e", action="store_true", help="Checkout an existing branch (no new branch created)")
    wt_parser.add_argument("--ref", help="Detach at a specific ref (tag, commit, branch)")
    wt_parser.add_argument("--project", "-p", help="Path to git repo (default: from config or cwd)")
    wt_parser.add_argument("--type", help="Session type override")
    wt_parser.add_argument("--roles", help="Comma-separated role names")
    wt_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var via `tmux set-environment` (repeatable)")
    wt_parser.add_argument("--json", action="store_true", help="Output as JSON")
    wt_parser.set_defaults(func=cmd_worktree)

    # === dev command ===
    dev_parser = subparsers.add_parser(
        "dev", help="Start/attach to dev agentwire session"
    )
    dev_parser.set_defaults(func=cmd_dev)

    # === workflow command group ===
    from agentwire.workflows.cli import (
        cmd_workflow_history,
        cmd_workflow_list,
        cmd_workflow_run,
        cmd_workflow_show,
        cmd_workflow_validate,
    )

    workflow_parser = subparsers.add_parser("workflow", help="Pi workflow engine")
    workflow_subparsers = workflow_parser.add_subparsers(dest="workflow_command")

    wf_list = workflow_subparsers.add_parser("list", help="List discoverable workflows")
    wf_list.add_argument("--json", action="store_true", help="Output as JSON")
    wf_list.set_defaults(func=cmd_workflow_list)

    wf_validate = workflow_subparsers.add_parser(
        "validate", help="Validate a workflow YAML without running it"
    )
    wf_validate.add_argument("workflow", help="Workflow name or path to YAML")
    wf_validate.set_defaults(func=cmd_workflow_validate)

    wf_run = workflow_subparsers.add_parser("run", help="Execute a workflow")
    wf_run.add_argument("workflow", help="Workflow name or path to YAML")
    wf_run.add_argument(
        "--input", action="append", metavar="KEY=VALUE",
        help="Workflow input (repeatable). Overrides --input-file."
    )
    wf_run.add_argument(
        "--input-file", metavar="PATH",
        help="JSON file with inputs (object mapping name → value)"
    )
    wf_run.add_argument(
        "--runner", choices=["pi", "anthropic"], default=None,
        help="Override runner for every node in this run (YAML declarations ignored)."
    )
    wf_run.add_argument("--dry-run", action="store_true", help="Print plan without running")
    wf_run.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    wf_run.add_argument("--json", action="store_true", help="Output as JSON")
    wf_run.set_defaults(func=cmd_workflow_run)

    wf_history = workflow_subparsers.add_parser(
        "history", help="List past workflow runs"
    )
    wf_history.add_argument(
        "--workflow", metavar="NAME", help="Filter by workflow name"
    )
    wf_history.add_argument(
        "--limit", type=int, default=20, help="Max runs to show (default: 20)"
    )
    wf_history.add_argument("--json", action="store_true", help="Output as JSON")
    wf_history.set_defaults(func=cmd_workflow_history)

    wf_show = workflow_subparsers.add_parser(
        "show", help="Inspect a past workflow run"
    )
    wf_show.add_argument("run_id", help="Run ID (from `workflow history`)")
    wf_show.add_argument(
        "--events", action="store_true",
        help="Dump raw event JSONL for all nodes",
    )
    wf_show.add_argument(
        "--node", metavar="ID",
        help="Filter events to one node id (implies --events)",
    )
    wf_show.add_argument("--json", action="store_true", help="Output as JSON")
    wf_show.set_defaults(func=cmd_workflow_show)

    # === listen command group ===
    listen_parser = subparsers.add_parser("listen", help="Voice input recording")
    listen_parser.add_argument(
        "--session", "-s", type=str, default="agentwire",
        help="Target session (default: agentwire)"
    )
    listen_parser.add_argument(
        "--no-prompt", action="store_true",
        help="Don't prepend voice prompt hint"
    )
    listen_subparsers = listen_parser.add_subparsers(dest="listen_command")

    # listen start
    listen_start = listen_subparsers.add_parser("start", help="Start recording")
    listen_start.set_defaults(func=cmd_listen_start)

    # listen stop
    listen_stop = listen_subparsers.add_parser("stop", help="Stop and send")
    listen_stop.add_argument("--session", "-s", type=str, help="Target session")
    listen_stop.add_argument("--no-prompt", action="store_true")
    listen_stop.add_argument("--type", action="store_true", help="Type at cursor instead of sending to session")
    listen_stop.set_defaults(func=cmd_listen_stop)

    # listen cancel
    listen_cancel = listen_subparsers.add_parser("cancel", help="Cancel recording")
    listen_cancel.set_defaults(func=cmd_listen_cancel)

    # Default listen (no subcommand) = toggle
    listen_parser.set_defaults(func=cmd_listen_toggle)

    # === voiceclone command group ===
    voiceclone_parser = subparsers.add_parser(
        "voiceclone", help="Record and upload voice clones"
    )
    voiceclone_subparsers = voiceclone_parser.add_subparsers(dest="voiceclone_command")

    # voiceclone start
    voiceclone_start = voiceclone_subparsers.add_parser(
        "start", help="Start recording for voice clone"
    )
    voiceclone_start.set_defaults(func=cmd_voiceclone_start)

    # voiceclone stop <name>
    voiceclone_stop = voiceclone_subparsers.add_parser(
        "stop", help="Stop recording and upload as voice clone"
    )
    voiceclone_stop.add_argument("name", help="Name for the voice clone")
    voiceclone_stop.set_defaults(func=cmd_voiceclone_stop)

    # voiceclone cancel
    voiceclone_cancel = voiceclone_subparsers.add_parser(
        "cancel", help="Cancel current recording"
    )
    voiceclone_cancel.set_defaults(func=cmd_voiceclone_cancel)

    # voiceclone list
    voiceclone_list = voiceclone_subparsers.add_parser(
        "list", help="List available voices"
    )
    voiceclone_list.add_argument("--json", action="store_true", help="Output JSON")
    voiceclone_list.set_defaults(func=cmd_voiceclone_list)

    # voiceclone delete <name>
    voiceclone_delete = voiceclone_subparsers.add_parser(
        "delete", help="Delete a voice clone"
    )
    voiceclone_delete.add_argument("name", help="Name of voice to delete")
    voiceclone_delete.set_defaults(func=cmd_voiceclone_delete)

    # === machine command group ===
    machine_parser = subparsers.add_parser("machine", help="Manage remote machines")
    machine_subparsers = machine_parser.add_subparsers(dest="machine_command")

    # machine list
    machine_list = machine_subparsers.add_parser("list", help="List registered machines")
    machine_list.add_argument("--json", action="store_true", help="Output JSON")
    machine_list.set_defaults(func=cmd_machine_list)

    # machine add <id>
    machine_add = machine_subparsers.add_parser(
        "add", help="Add a machine to the network"
    )
    machine_add.add_argument("machine_id", help="Machine ID (used in session names)")
    machine_add.add_argument("--host", help="SSH host (defaults to machine_id)")
    machine_add.add_argument("--user", help="SSH user")
    machine_add.add_argument("--projects-dir", dest="projects_dir", help="Projects directory on remote")
    machine_add.set_defaults(func=cmd_machine_add)

    # machine remove <id>
    machine_remove = machine_subparsers.add_parser(
        "remove", help="Remove a machine from the network"
    )
    machine_remove.add_argument("machine_id", help="Machine ID to remove")
    machine_remove.set_defaults(func=cmd_machine_remove)

    # === history command group ===
    history_parser = subparsers.add_parser("history", help="Claude Code session history")
    history_subparsers = history_parser.add_subparsers(dest="history_command")

    # history list
    history_list = history_subparsers.add_parser("list", help="List conversation history")
    history_list.add_argument("--project", "-p", help="Project path (defaults to cwd)")
    history_list.add_argument("--machine", "-m", default="local", help="Machine ID")
    history_list.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    history_list.add_argument("--json", action="store_true", help="JSON output")
    history_list.set_defaults(func=cmd_history_list)

    # history show <session_id>
    history_show = history_subparsers.add_parser("show", help="Show session details")
    history_show.add_argument("session_id", help="Session ID to show")
    history_show.add_argument("--machine", "-m", default="local", help="Machine ID")
    history_show.add_argument("--json", action="store_true", help="JSON output")
    history_show.set_defaults(func=cmd_history_show)

    # history resume <session_id>
    history_resume = history_subparsers.add_parser("resume", help="Resume a session (always forks)")
    history_resume.add_argument("session_id", help="Session ID to resume")
    history_resume.add_argument("--name", "-n", help="New tmux session name")
    history_resume.add_argument("--machine", "-m", default="local", help="Machine ID")
    history_resume.add_argument("--project", "-p", required=True, help="Project path")
    history_resume.add_argument("--json", action="store_true", help="JSON output")
    history_resume.set_defaults(func=cmd_history_resume)

    # === roles command group ===
    roles_parser = subparsers.add_parser(
        "roles", help="Manage composable roles"
    )
    roles_subparsers = roles_parser.add_subparsers(dest="roles_command")

    # roles list
    roles_list = roles_subparsers.add_parser("list", help="List available roles")
    roles_list.add_argument("--json", action="store_true", help="Output as JSON")
    roles_list.set_defaults(func=cmd_roles_list)

    # roles show <name>
    roles_show = roles_subparsers.add_parser("show", help="Show role details")
    roles_show.add_argument("name", help="Role name")
    roles_show.add_argument("--json", action="store_true", help="Output as JSON")
    roles_show.set_defaults(func=cmd_roles_show)

    # === projects command group ===
    projects_parser = subparsers.add_parser(
        "projects", help="Discover and list projects"
    )
    projects_subparsers = projects_parser.add_subparsers(dest="projects_command")

    # projects list
    projects_list = projects_subparsers.add_parser("list", help="List discovered projects")
    projects_list.add_argument("--machine", help="Filter by machine ID (e.g., 'local', 'mac-studio')")
    projects_list.add_argument("--json", action="store_true", help="Output as JSON")
    projects_list.set_defaults(func=cmd_projects_list)

    # === hooks command group ===
    hooks_parser = subparsers.add_parser(
        "hooks", help="Manage Claude Code permission hook"
    )
    hooks_subparsers = hooks_parser.add_subparsers(dest="hooks_command")

    # hooks install
    hooks_install = hooks_subparsers.add_parser(
        "install", help="Install Claude Code permission hook"
    )
    hooks_install.add_argument(
        "--force", "-f", action="store_true", help="Overwrite existing installation"
    )
    hooks_install.add_argument(
        "--copy", action="store_true", help="Copy files instead of symlinking"
    )
    hooks_install.set_defaults(func=cmd_hooks_install)

    # hooks uninstall
    hooks_uninstall = hooks_subparsers.add_parser(
        "uninstall", help="Remove Claude Code permission hook"
    )
    hooks_uninstall.set_defaults(func=cmd_hooks_uninstall)

    # hooks status
    hooks_status = hooks_subparsers.add_parser(
        "status", help="Check hook installation status"
    )
    hooks_status.set_defaults(func=cmd_hooks_status)

    # === network command group ===
    network_parser = subparsers.add_parser(
        "network", help="Network diagnostics and status"
    )
    network_subparsers = network_parser.add_subparsers(dest="network_command")

    # network status
    network_status = network_subparsers.add_parser(
        "status", help="Show complete network health at a glance"
    )
    network_status.set_defaults(func=cmd_network_status)

    # === safety command group ===
    safety_parser = subparsers.add_parser(
        "safety", help="Damage control security commands"
    )
    safety_subparsers = safety_parser.add_subparsers(dest="safety_command")

    # safety check <command>
    safety_check = safety_subparsers.add_parser(
        "check", help="Test if a command would be blocked/allowed"
    )
    safety_check.add_argument("command", help="Command to test")
    safety_check.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )
    safety_check.set_defaults(func=cmd_safety_check)

    # safety status
    safety_status = safety_subparsers.add_parser(
        "status", help="Show safety status and pattern counts"
    )
    safety_status.set_defaults(func=cmd_safety_status)

    # safety logs
    safety_logs = safety_subparsers.add_parser(
        "logs", help="Query audit logs"
    )
    safety_logs.add_argument(
        "--tail", "-n", type=int, help="Show last N entries"
    )
    safety_logs.add_argument(
        "--session", "-s", help="Filter by session ID"
    )
    safety_logs.add_argument(
        "--today", action="store_true", help="Show only today's logs"
    )
    safety_logs.add_argument(
        "--pattern", "-p", help="Filter by pattern (regex or substring)"
    )
    safety_logs.set_defaults(func=cmd_safety_logs)

    # safety install
    safety_install = safety_subparsers.add_parser(
        "install", help="Install damage control hooks (interactive)"
    )
    safety_install.set_defaults(func=cmd_safety_install)

    # safety tooldefs
    safety_tooldefs = safety_subparsers.add_parser(
        "tooldefs", help="Browse tool definitions"
    )
    tooldefs_subparsers = safety_tooldefs.add_subparsers(dest="tooldefs_command")

    tooldefs_list = tooldefs_subparsers.add_parser("list", help="List available tooldefs")
    tooldefs_list.set_defaults(func=cmd_safety_tooldefs_list)

    tooldefs_show = tooldefs_subparsers.add_parser("show", help="Show tooldef for a tool")
    tooldefs_show.add_argument("tool", help="Tool name (e.g. git, gh, docker)")
    tooldefs_show.set_defaults(func=cmd_safety_tooldefs_show)

    # === doctor command (top-level) ===
    doctor_parser = subparsers.add_parser(
        "doctor", help="Auto-diagnose and fix common issues"
    )
    doctor_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without making changes"
    )
    doctor_parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Auto-confirm all fixes without prompting"
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    # === generate-certs (top-level shortcut) ===
    certs_parser = subparsers.add_parser(
        "generate-certs", help="Generate SSL certificates"
    )
    certs_parser.set_defaults(func=cmd_generate_certs)

    # === rebuild command ===
    rebuild_parser = subparsers.add_parser(
        "rebuild", help="Clear uv cache and reinstall from source (for development)"
    )
    rebuild_parser.set_defaults(func=cmd_rebuild)

    # === uninstall command ===
    uninstall_parser = subparsers.add_parser(
        "uninstall", help="Clear uv cache and uninstall the tool"
    )
    uninstall_parser.set_defaults(func=cmd_uninstall)

    # === mcp command ===
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Run MCP server for external agent integration",
        description="Expose AgentWire as an MCP server for tools like MoltBot, Claude Desktop, etc.",
    )
    mcp_parser.set_defaults(func=cmd_mcp)

    # === ensure command (scheduled workloads) ===
    ensure_parser = subparsers.add_parser(
        "ensure",
        help="Run named task with reliable session management",
        description="Execute a task from .agentwire.yml with locking, retries, and completion detection.",
    )
    ensure_parser.add_argument("-s", "--session", required=True, help="Target session name")
    ensure_parser.add_argument("-p", "--project", help="Project path containing .agentwire.yml (defaults to ~/projects/{session})")
    ensure_parser.add_argument("--task", required=True, help="Task name from .agentwire.yml")
    ensure_parser.add_argument("--dry-run", action="store_true", help="Show what would execute without running")
    ensure_parser.add_argument("--wait-lock", action="store_true", help="Wait for lock instead of failing if locked")
    ensure_parser.add_argument("--lock-timeout", type=int, default=60, help="Max time to wait for lock (default: 60s)")
    ensure_parser.add_argument("--skip-if-locked", action="store_true", help="Exit 0 silently if session is locked (for cron use cases)")
    ensure_parser.add_argument("--json", action="store_true", help="Output JSON")
    ensure_parser.set_defaults(func=cmd_ensure)

    # === task command group ===
    task_parser = subparsers.add_parser(
        "task",
        help="Manage scheduled tasks",
        description="List, show, and validate tasks defined in .agentwire.yml.",
    )
    task_subparsers = task_parser.add_subparsers(dest="task_command")

    # task list
    task_list = task_subparsers.add_parser("list", help="List tasks for session/project")
    task_list.add_argument("session", nargs="?", help="Session name (default: current directory)")
    task_list.add_argument("--json", action="store_true", help="Output JSON")
    task_list.set_defaults(func=cmd_task_list)

    # task show
    task_show = task_subparsers.add_parser("show", help="Show task definition details")
    task_show.add_argument("task", help="Task name (session/task or just task)")
    task_show.add_argument("--json", action="store_true", help="Output JSON")
    task_show.set_defaults(func=cmd_task_show)

    # task validate
    task_validate = task_subparsers.add_parser("validate", help="Validate task configuration")
    task_validate.add_argument("task", help="Task name (session/task or just task)")
    task_validate.add_argument("--json", action="store_true", help="Output JSON")
    task_validate.set_defaults(func=cmd_task_validate)

    # === lock command group ===
    lock_parser = subparsers.add_parser(
        "lock",
        help="Manage session locks",
        description="List, clean, and remove session locks.",
    )
    lock_subparsers = lock_parser.add_subparsers(dest="lock_command")
    lock_parser.set_defaults(func=cmd_lock)

    # lock list
    lock_list_parser = lock_subparsers.add_parser("list", help="List all locks")
    lock_list_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_list_parser.set_defaults(func=cmd_lock_list)

    # lock clean
    lock_clean_parser = lock_subparsers.add_parser("clean", help="Remove stale locks")
    lock_clean_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    lock_clean_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_clean_parser.set_defaults(func=cmd_lock_clean)

    # lock remove
    lock_remove_parser = lock_subparsers.add_parser("remove", help="Force-remove a lock")
    lock_remove_parser.add_argument("session", help="Session name")
    lock_remove_parser.add_argument("--json", action="store_true", help="Output JSON")
    lock_remove_parser.set_defaults(func=cmd_lock_remove)

    # === scheduler command group ===
    scheduler_parser = subparsers.add_parser(
        "scheduler",
        help="Manage the task scheduler",
        description=(
            "Centralized daemon that dispatches tasks across projects on a shared cadence. "
            "Tasks in ~/.agentwire/scheduler.yaml are either ensure tasks (task: + session:) "
            "or workflow tasks (workflow: + inputs:) — the scheduler routes each automatically. "
            "See docs/wiki/scheduling/scheduled-workloads.md."
        ),
    )
    scheduler_subparsers = scheduler_parser.add_subparsers(dest="scheduler_command")

    # scheduler start
    sched_start = scheduler_subparsers.add_parser("start", help="Start scheduler daemon")
    sched_start.set_defaults(func=cmd_scheduler_start)

    # scheduler serve (foreground, for tmux)
    sched_serve = scheduler_subparsers.add_parser("serve", help="Run scheduler in foreground")
    sched_serve.set_defaults(func=cmd_scheduler_serve)

    # scheduler stop
    sched_stop = scheduler_subparsers.add_parser("stop", help="Stop scheduler")
    sched_stop.set_defaults(func=cmd_scheduler_stop)

    # scheduler status
    sched_status = scheduler_subparsers.add_parser("status", help="Check scheduler status")
    sched_status.add_argument("--json", action="store_true", help="Output JSON")
    sched_status.set_defaults(func=cmd_scheduler_status)

    # scheduler board
    sched_board = scheduler_subparsers.add_parser("board", help="Show task board with overdue scores")
    sched_board.add_argument("--json", action="store_true", help="Output JSON")
    sched_board.set_defaults(func=cmd_scheduler_board)

    # scheduler run <name>
    sched_run = scheduler_subparsers.add_parser("run", help="Force-run a task now")
    sched_run.add_argument("name", help="Task name from board")
    sched_run.add_argument("--json", action="store_true", help="Output JSON")
    sched_run.add_argument("--dry-run", action="store_true",
                           help="For workflow tasks: print the execution plan without running")
    sched_run.set_defaults(func=cmd_scheduler_run)

    # scheduler enable <name>
    sched_enable = scheduler_subparsers.add_parser("enable", help="Enable a task")
    sched_enable.add_argument("name", help="Task name")
    sched_enable.set_defaults(func=cmd_scheduler_enable)

    # scheduler disable <name>
    sched_disable = scheduler_subparsers.add_parser("disable", help="Disable a task")
    sched_disable.add_argument("name", help="Task name")
    sched_disable.set_defaults(func=cmd_scheduler_disable)

    # scheduler history
    sched_history = scheduler_subparsers.add_parser("history", help="Show recent run history")
    sched_history.add_argument("--json", action="store_true", help="Output JSON")
    sched_history.set_defaults(func=cmd_scheduler_history)

    # scheduler events
    sched_events = scheduler_subparsers.add_parser("events", help="Show recent scheduler events")
    sched_events.add_argument("--json", action="store_true", help="Output JSON")
    sched_events.add_argument("--tail", type=int, default=20, help="Number of events (default: 20)")
    sched_events.add_argument("--task", help="Filter by task name")
    sched_events.set_defaults(func=cmd_scheduler_events)

    # scheduler live
    sched_live = scheduler_subparsers.add_parser("live", help="Show live scheduler state")
    sched_live.add_argument("--json", action="store_true", help="Output JSON")
    sched_live.add_argument("--watch", action="store_true", help="Re-read every 2s")
    sched_live.set_defaults(func=cmd_scheduler_live)

    # scheduler dashboard
    sched_dashboard = scheduler_subparsers.add_parser("dashboard", help="Open scheduler dashboard")
    sched_dashboard.add_argument("--no-open", action="store_true", help="Generate HTML without opening")
    sched_dashboard.set_defaults(func=cmd_scheduler_dashboard)

    # scheduler report
    sched_report = scheduler_subparsers.add_parser("report", help="Generate morning report of recent task runs")
    sched_report.add_argument("--since", default="8h", metavar="DURATION", help="Time window (e.g. 8h, 12h, 1d) default: 8h")
    sched_report.add_argument("--artifact", action="store_true", help="Open report as portal artifact")
    sched_report.add_argument("--json", action="store_true", help="Output JSON")
    sched_report.set_defaults(func=cmd_scheduler_report)

    # === overnight command group ===
    overnight_parser = subparsers.add_parser(
        "overnight",
        help="Manage overnight session queue",
        description="Prepare sessions during the day, dispatch them autonomously overnight.",
    )
    overnight_subparsers = overnight_parser.add_subparsers(dest="overnight_command")

    # overnight prepare
    ov_prepare = overnight_subparsers.add_parser("prepare", help="Queue a session for overnight execution")
    ov_prepare.add_argument("--from", dest="source", required=True, help="Source session name")
    ov_prepare.add_argument("--task", required=True, help="Task description")
    ov_prepare.add_argument("--priority", type=int, default=50, help="Priority (lower = higher, default: 50)")
    ov_prepare.add_argument("--type", help="Session type override (default: from config)")
    ov_prepare.add_argument("--json", action="store_true", help="Output JSON")
    ov_prepare.set_defaults(func=cmd_overnight_prepare)

    # overnight list
    ov_list = overnight_subparsers.add_parser("list", help="List overnight queue items")
    ov_list.add_argument("--all", action="store_true", help="Include completed items")
    ov_list.add_argument("--json", action="store_true", help="Output JSON")
    ov_list.set_defaults(func=cmd_overnight_list)

    # overnight status
    ov_status = overnight_subparsers.add_parser("status", help="Check overnight orchestrator state")
    ov_status.add_argument("--json", action="store_true", help="Output JSON")
    ov_status.set_defaults(func=cmd_overnight_status)

    # overnight cancel
    ov_cancel = overnight_subparsers.add_parser("cancel", help="Cancel a queued item")
    ov_cancel.add_argument("id", help="Item ID to cancel")
    ov_cancel.add_argument("--json", action="store_true", help="Output JSON")
    ov_cancel.set_defaults(func=cmd_overnight_cancel)

    # overnight priority
    ov_prio = overnight_subparsers.add_parser("priority", help="Update item priority")
    ov_prio.add_argument("id", help="Item ID")
    ov_prio.add_argument("priority", type=int, help="New priority (lower = higher)")
    ov_prio.add_argument("--json", action="store_true", help="Output JSON")
    ov_prio.set_defaults(func=cmd_overnight_priority)

    # overnight start
    ov_start = overnight_subparsers.add_parser("start", help="Start overnight orchestrator daemon")
    ov_start.set_defaults(func=cmd_overnight_start)

    # overnight serve
    ov_serve = overnight_subparsers.add_parser("serve", help="Run orchestrator in foreground")
    ov_serve.set_defaults(func=cmd_overnight_serve)

    # overnight stop
    ov_stop = overnight_subparsers.add_parser("stop", help="Stop overnight orchestrator")
    ov_stop.set_defaults(func=cmd_overnight_stop)

    # overnight report
    ov_report = overnight_subparsers.add_parser("report", help="Morning report from completed items")
    ov_report.add_argument("--json", action="store_true", help="Output JSON")
    ov_report.set_defaults(func=cmd_overnight_report)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "portal" and getattr(args, "portal_command", None) is None:
        portal_parser.print_help()
        return 0

    if args.command == "tts" and getattr(args, "tts_command", None) is None:
        tts_parser.print_help()
        return 0

    if args.command == "stt" and getattr(args, "stt_command", None) is None:
        stt_parser.print_help()
        return 0

    if args.command == "tunnels" and getattr(args, "tunnels_command", None) is None:
        tunnels_parser.print_help()
        return 0

    if args.command == "machine" and getattr(args, "machine_command", None) is None:
        machine_parser.print_help()
        return 0

    if args.command == "history" and getattr(args, "history_command", None) is None:
        history_parser.print_help()
        return 0

    if args.command == "hooks" and getattr(args, "hooks_command", None) is None:
        hooks_parser.print_help()
        return 0

    if args.command == "projects" and getattr(args, "projects_command", None) is None:
        projects_parser.print_help()
        return 0

    if args.command == "safety" and getattr(args, "safety_command", None) is None:
        safety_parser.print_help()
        return 0

    if args.command == "safety" and getattr(args, "safety_command", None) == "tooldefs" and getattr(args, "tooldefs_command", None) is None:
        safety_tooldefs.print_help()
        return 0

    if args.command == "network" and getattr(args, "network_command", None) is None:
        network_parser.print_help()
        return 0

    if args.command == "listen" and getattr(args, "listen_command", None) is None:
        listen_parser.print_help()
        return 0

    if args.command == "voiceclone" and getattr(args, "voiceclone_command", None) is None:
        voiceclone_parser.print_help()
        return 0

    if args.command == "roles" and getattr(args, "roles_command", None) is None:
        roles_parser.print_help()
        return 0

    if args.command == "task" and getattr(args, "task_command", None) is None:
        task_parser.print_help()
        return 0

    if args.command == "lock" and getattr(args, "lock_command", None) is None:
        lock_parser.print_help()
        return 0

    if args.command == "scheduler" and getattr(args, "scheduler_command", None) is None:
        scheduler_parser.print_help()
        return 0

    if args.command == "overnight" and getattr(args, "overnight_command", None) is None:
        overnight_parser.print_help()
        return 0

    if hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
