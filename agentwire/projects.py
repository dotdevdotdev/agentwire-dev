"""
Project discovery for AgentWire.

Discovers projects by scanning for folders with .agentwire.yml in each machine's projects_dir.
"""

import json
from pathlib import Path

import yaml

from .config import get_config
from .ssh import ssh_base_opts

# Default config directory
CONFIG_DIR = Path.home() / ".agentwire"


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


def _run_ssh_command(machine: dict, command: str, timeout: int = 10) -> tuple[bool, str]:
    """Run command on remote machine via SSH.

    Args:
        machine: Machine config dict with host, user, port
        command: Shell command to run

    Returns:
        (success, output) tuple
    """
    import subprocess

    host = machine.get("host", machine.get("id", ""))
    user = machine.get("user")
    port = machine.get("port")

    # Build SSH target
    if user:
        ssh_target = f"{user}@{host}"
    else:
        ssh_target = host

    # Build SSH command with connection timeout
    ssh_cmd = ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([ssh_target, command])

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout
    except subprocess.TimeoutExpired:
        return False, ""
    except Exception:
        return False, ""


def _discover_local_projects(projects_dir: Path) -> list[dict]:
    """Discover projects in a local directory.

    Args:
        projects_dir: Path to scan for projects

    Returns:
        List of project dicts with name, path, type, roles, machine
    """
    projects = []
    projects_dir = projects_dir.expanduser().resolve()

    if not projects_dir.exists() or not projects_dir.is_dir():
        return projects

    for folder in projects_dir.iterdir():
        if not folder.is_dir():
            continue

        config_file = folder / ".agentwire.yml"
        if not config_file.exists():
            continue

        try:
            cfg = yaml.safe_load(config_file.read_text()) or {}
        except Exception:
            cfg = {}

        projects.append({
            "name": folder.name,
            "path": str(folder),
            "posture": cfg.get("posture", "bypass"),
            "roles": cfg.get("roles", []),
            "machine": "local",
        })

    return projects


def _discover_remote_projects(machine: dict) -> list[dict]:
    """Discover projects on a remote machine via SSH.

    Args:
        machine: Machine config dict with projects_dir

    Returns:
        List of project dicts with name, path, type, roles, machine
    """
    projects = []
    machine_id = machine.get("id", "")
    projects_dir = machine.get("projects_dir", "")

    if not projects_dir:
        return projects

    # SSH command to find folders with .agentwire.yml and cat their contents
    # Output format: one line per project: "folder_name|config_yaml_base64"
    # Using base64 to safely transfer YAML content
    cmd = f'''
cd {projects_dir} 2>/dev/null && for d in */; do
  d="${{d%/}}"
  if [ -f "$d/.agentwire.yml" ]; then
    cfg=$(cat "$d/.agentwire.yml" | base64 -w0 2>/dev/null || cat "$d/.agentwire.yml" | base64)
    echo "$d|$cfg"
  fi
done
'''

    success, output = _run_ssh_command(machine, cmd)
    if not success:
        return projects

    import base64

    for line in output.strip().split("\n"):
        if not line or "|" not in line:
            continue

        parts = line.split("|", 1)
        if len(parts) != 2:
            continue

        folder_name, config_b64 = parts

        try:
            config_yaml = base64.b64decode(config_b64).decode("utf-8")
            cfg = yaml.safe_load(config_yaml) or {}
        except Exception:
            cfg = {}

        projects.append({
            "name": folder_name,
            "path": f"{projects_dir}/{folder_name}",
            "posture": cfg.get("posture", "bypass"),
            "roles": cfg.get("roles", []),
            "machine": machine_id,
        })

    return projects


def _resolve_extra_projects(extra: list[dict], machine_filter: str | None = None) -> list[dict]:
    """Resolve explicitly configured extra project paths.

    Each entry in extra is a dict with 'path' and optional 'machine' (default: 'local').
    Reads .agentwire.yml from each path for type/roles.

    Args:
        extra: List of extra project entries from config.
        machine_filter: Only include projects matching this machine.

    Returns:
        List of project dicts: {name, path, type, roles, machine}
    """
    import base64

    projects = []
    for entry in extra:
        path = entry.get("path", "")
        if not path:
            continue
        entry_machine = entry.get("machine", "local")

        # Filter by machine if requested
        if machine_filter is not None:
            if machine_filter != entry_machine:
                continue

        if entry_machine == "local":
            # Local: read .agentwire.yml directly
            p = Path(path).expanduser().resolve()
            if not p.is_dir():
                continue
            config_file = p / ".agentwire.yml"
            try:
                cfg = yaml.safe_load(config_file.read_text()) or {} if config_file.exists() else {}
            except Exception:
                cfg = {}
            projects.append({
                "name": entry.get("name", p.name),
                "path": str(p),
                "posture": cfg.get("posture", "bypass"),
                "roles": cfg.get("roles", []),
                "machine": "local",
            })
        else:
            # Remote: read .agentwire.yml via SSH
            m = _get_machine_config(entry_machine)
            if not m:
                continue
            cmd = f'''
if [ -d "{path}" ]; then
  if [ -f "{path}/.agentwire.yml" ]; then
    cat "{path}/.agentwire.yml" | base64 -w0 2>/dev/null || cat "{path}/.agentwire.yml" | base64
  else
    echo ""
  fi
fi
'''
            success, output = _run_ssh_command(m, cmd)
            cfg = {}
            if success and output.strip():
                try:
                    config_yaml = base64.b64decode(output.strip()).decode("utf-8")
                    cfg = yaml.safe_load(config_yaml) or {}
                except Exception:
                    pass

            name = entry.get("name", Path(path).name)
            projects.append({
                "name": name,
                "path": path,
                "posture": cfg.get("posture", "bypass"),
                "roles": cfg.get("roles", []),
                "machine": entry_machine,
            })

    return projects


def get_projects(machine: str | None = None) -> list[dict]:
    """Discover projects from machine's projects_dir.

    Args:
        machine: Machine ID to filter by. None = all machines including local.
                 'local' = only local machine.

    Returns:
        List of project dicts: {name, path, type, roles, machine}
    """
    projects = []
    config = get_config()

    # Local machine discovery
    if machine is None or machine == "local":
        local_projects = _discover_local_projects(config.projects.dir)
        projects.extend(local_projects)

    # Remote machines discovery
    if machine is None:
        # Discover from all remote machines with projects_dir
        for m in _get_all_machines():
            if m.get("projects_dir"):
                remote_projects = _discover_remote_projects(m)
                projects.extend(remote_projects)
    elif machine != "local":
        # Discover from specific remote machine
        m = _get_machine_config(machine)
        if m and m.get("projects_dir"):
            remote_projects = _discover_remote_projects(m)
            projects.extend(remote_projects)

    # Extra projects from config (explicit paths outside projects_dir)
    extra_projects = _resolve_extra_projects(config.projects.extra, machine)
    # Deduplicate by (machine, path) — extras don't override discovered projects
    seen = {(p["machine"], p["path"]) for p in projects}
    for ep in extra_projects:
        if (ep["machine"], ep["path"]) not in seen:
            projects.append(ep)

    # Sort by machine then name
    projects.sort(key=lambda p: (p["machine"], p["name"]))

    return projects
