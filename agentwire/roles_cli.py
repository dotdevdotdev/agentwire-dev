"""CLI for roles and projects — ``agentwire roles ...`` / ``agentwire projects ...``.

Roles are composable instruction/tool bundles discovered from user and bundled
sources. Projects are directories carrying a ``.agentwire.yml`` config.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

from .core import _output_json


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
    print(f"{'Name':<25} {'Posture':<15} {'Path':<40}")
    print("-" * 80)
    for p in projects:
        # Truncate long paths
        path = p["path"]
        if len(path) > 40:
            path = "..." + path[-37:]
        machine_suffix = f" @{p['machine']}" if p['machine'] != 'local' else ""
        print(f"{p['name']:<25} {p['posture']:<15} {path:<40}{machine_suffix}")

    print()
    return 0


_VALID_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def cmd_projects_create(args) -> int:
    """Create a new local project: make the directory, optionally git-init or clone, and write .agentwire.yml."""
    from .config import get_config
    from .project_config import ProjectConfig, ensure_gitignored, save_project_config

    name = (args.name or "").strip()
    clone_url = (getattr(args, "from_url", None) or "").strip() or None
    do_git_init = bool(getattr(args, "git_init", False))
    json_mode = getattr(args, "json", False)

    def _fail(msg: str) -> int:
        if json_mode:
            _output_json({"success": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    if not name:
        return _fail("Project name is required")
    if not _VALID_PROJECT_NAME.match(name) or "/" in name or ".." in name:
        return _fail("Invalid project name (allowed: letters, digits, '.', '_', '-')")

    projects_dir = get_config().projects.dir.expanduser().resolve()
    projects_dir.mkdir(parents=True, exist_ok=True)
    project_path = projects_dir / name

    if project_path.exists():
        return _fail(f"Project already exists at {project_path}")

    try:
        if clone_url:
            result = subprocess.run(
                ["git", "clone", clone_url, str(project_path)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                return _fail(f"git clone failed: {result.stderr.strip() or 'unknown error'}")
        else:
            project_path.mkdir(parents=True, exist_ok=False)
            if do_git_init:
                result = subprocess.run(
                    ["git", "init"],
                    cwd=str(project_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    return _fail(f"git init failed: {result.stderr.strip() or 'unknown error'}")
    except subprocess.TimeoutExpired:
        return _fail("Operation timed out")
    except OSError as e:
        return _fail(f"Failed to create directory: {e}")

    gitignore_updated = ensure_gitignored(project_path)
    config = ProjectConfig(posture="bypass", roles=[], voice=None)
    if not save_project_config(config, project_path):
        return _fail("Created project directory but failed to write .agentwire.yml")

    payload = {
        "success": True,
        "name": name,
        "path": str(project_path),
        "machine": "local",
        "cloned": bool(clone_url),
        "git_init": do_git_init and not clone_url,
    }
    if json_mode:
        _output_json(payload)
    else:
        print(f"Created project '{name}' at {project_path}")
        if clone_url:
            print(f"  Cloned from: {clone_url}")
        elif do_git_init:
            print("  Initialized empty git repository")
        if gitignore_updated:
            print("  Added .agentwire.yml to .gitignore (personal config — keep it untracked)")
    return 0


def cmd_projects_add(args) -> int:
    """Bind an existing folder as a project without hand-editing config.yaml (#814).

    Local: resolve+canonicalize the path, require it to exist, write a minimal
    .agentwire.yml unless one is already there (collision = "already bound",
    never overwritten), then register it in the out-of-tree registry unless
    the scan of projects.dir already covers it.

    Remote (--machine): validated against machines.json, existence checked
    over SSH (informational — mirrors repo-info's local/remote split), then
    registered. Writing .agentwire.yml remotely and the projects.dir-child
    skip-check are local-only concerns — the existing remote scan
    (`_discover_remote_projects`) already reads .agentwire.yml over SSH once
    a path is registered, so bind doesn't re-derive that logic per transport.

    --check runs the same resolution/validation/collision-detection with no
    writes (no .agentwire.yml, no registry entry) — the portal's bind modal
    uses it for the non-mutating preview step before the user confirms.
    """
    from .config import get_config
    from .core import _get_machine_config, _run_remote
    from .project_config import ProjectConfig, save_project_config
    from .projects import add_registry_entry, is_registered

    json_mode = getattr(args, "json", False)
    check_mode = bool(getattr(args, "check", False))
    machine = (getattr(args, "machine", None) or "local").strip() or "local"
    raw_path = (args.path or "").strip()

    def _fail(msg: str) -> int:
        if json_mode:
            _output_json({"success": False, "error": msg})
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    if not raw_path:
        return _fail("Path is required")

    if machine != "local":
        if not _get_machine_config(machine):
            return _fail(f"Unknown machine '{machine}' — run 'agentwire machine list' to see registered machines")

        path = raw_path.rstrip("/") or raw_path
        result = _run_remote(machine, f"test -d {shlex.quote(path)} && echo exists")
        if result.returncode != 0 or "exists" not in (result.stdout or ""):
            return _fail(f"'{path}' does not exist or is not a directory on '{machine}'")

        already_registered = is_registered(path, machine)
        newly_registered = False
        if not check_mode and not already_registered:
            newly_registered = add_registry_entry(path, machine)

        payload = {
            "success": True,
            "path": path,
            "machine": machine,
            "already_bound": already_registered,
            "mechanism": "registry",
            "dry_run": check_mode,
        }
        if json_mode:
            _output_json(payload)
        else:
            if check_mode:
                verb = "Already registered" if already_registered else "Would register"
            else:
                verb = "Already registered" if already_registered else "Bound"
            print(f"{verb} '{path}' on '{machine}' (registry)")
            if not check_mode and not already_registered and not newly_registered:
                print("Warning: registry write failed", file=sys.stderr)
        return 0

    # Local path: canonicalize symlinks/~ so what's stored is the real target.
    p = Path(raw_path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        return _fail(f"'{p}' does not exist or is not a directory")

    config_file = p / ".agentwire.yml"
    already_bound = config_file.exists()

    if not already_bound and not check_mode:
        cfg = ProjectConfig(posture="bypass", roles=[], voice=None)
        if not save_project_config(cfg, p):
            return _fail(f"Failed to write .agentwire.yml at {p}")

    # Git status is informational only — never gates the bind (reuses repo_cli's logic).
    from .repo_cli import _repo_info
    info = _repo_info(str(p), "local")

    projects_dir = get_config().projects.dir.expanduser().resolve()
    under_scan = p.parent == projects_dir
    already_registered = is_registered(str(p), "local")
    newly_registered = False
    if not under_scan and not already_registered and not check_mode:
        newly_registered = add_registry_entry(str(p), "local")

    mechanism = "scan" if under_scan else "registry"

    payload = {
        "success": True,
        "path": str(p),
        "machine": "local",
        "already_bound": already_bound,
        "wrote_config": (not already_bound) and not check_mode,
        "is_git": info["is_git"],
        "branch": info["current_branch"],
        "mechanism": mechanism,
        "dry_run": check_mode,
    }
    if json_mode:
        _output_json(payload)
    else:
        if already_bound:
            print(f"'{p}' already has .agentwire.yml — already bound")
        elif check_mode:
            print(f"'{p}' would be bound — .agentwire.yml would be written")
        else:
            print(f"Bound '{p}' — wrote .agentwire.yml")
        print(f"  git: {'branch ' + info['current_branch'] if info['is_git'] and info['current_branch'] else ('git repo' if info['is_git'] else 'not a git repository')}")
        if under_scan:
            print(f"  discovered via scan of {projects_dir}")
        else:
            verb = "would be registered" if check_mode else ("newly registered" if newly_registered else "already registered")
            print(f"  discovered via registry ({verb})")
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


def register_roles_parser(subparsers) -> None:
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


def register_projects_parser(subparsers) -> None:
    projects_parser = subparsers.add_parser(
        "projects", help="Discover and list projects"
    )
    projects_subparsers = projects_parser.add_subparsers(dest="projects_command")

    # projects list
    projects_list = projects_subparsers.add_parser("list", help="List discovered projects")
    projects_list.add_argument("--machine", help="Filter by machine ID (e.g., 'local', 'mac-studio')")
    projects_list.add_argument("--json", action="store_true", help="Output as JSON")
    projects_list.set_defaults(func=cmd_projects_list)

    # projects create
    projects_create = projects_subparsers.add_parser(
        "create",
        help="Create a new local project under projects.dir with a default .agentwire.yml",
    )
    projects_create.add_argument("name", help="Project name (directory name under projects.dir)")
    projects_create.add_argument(
        "--from", dest="from_url", help="Clone from this git URL instead of creating an empty directory"
    )
    projects_create.add_argument(
        "--git-init", action="store_true", help="Run 'git init' in the new project (ignored when --from is given)"
    )
    projects_create.add_argument("--json", action="store_true", help="Output as JSON")
    projects_create.set_defaults(func=cmd_projects_create)

    # projects add
    projects_add = projects_subparsers.add_parser(
        "add",
        help="Bind an existing folder as a project (writes .agentwire.yml + registers out-of-tree paths)",
    )
    projects_add.add_argument("path", help="Path to an existing folder")
    projects_add.add_argument("--machine", help="Machine ID the path lives on (default: local)")
    projects_add.add_argument(
        "--check", action="store_true",
        help="Resolve and validate only — don't write .agentwire.yml or register anything (dry run)",
    )
    projects_add.add_argument("--json", action="store_true", help="Output as JSON")
    projects_add.set_defaults(func=cmd_projects_add)
