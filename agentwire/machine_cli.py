"""CLI for the remote-machine registry — ``agentwire machine ...``.

Manages ``~/.agentwire/machines.json`` (add/remove/list). Remote session
management uses plain SSH; these commands only edit the registry and print
the manual SSH/deploy-key steps.
"""

from __future__ import annotations

import json
import subprocess
import sys

from .core import CONFIG_DIR, _output_json, write_owner_only


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

    # Save owner-only (#887): the registry names remote hosts, users and paths,
    # and a bare `open(..., "w")` inherits the umask — which is how the 0644
    # registry found in the wild got there.
    write_owner_only(machines_file, json.dumps({"machines": machines}, indent=2) + "\n")

    print(f"Added machine '{machine_id}'")
    print(f"  Host: {host}")
    if user:
        print(f"  User: {user}")
    if projects_dir:
        print(f"  Projects: {projects_dir}")
    print()
    print("Next steps:")
    print("  1. Ensure SSH access: ssh", f"{user}@{host}" if user else host)
    print("  2. Restart portal: agentwire portal stop && agentwire portal start")
    print()
    print("Remote session management uses plain SSH — no tunnel needed. To reach")
    print("the portal from another network, bring your own tunnel (cloudflared/")
    print("tailscale); see docs/wiki/deployment/remote-access.md.")
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

    # Step 3: Remove from machines.json
    print("Updating machines.json...")
    machines_data["machines"] = [m for m in machines if m.get("id") != machine_id]
    # Rewriting through write_owner_only also HEALS a registry that was minted
    # 0644 before #887 — os.replace swaps in an already-0600 inode.
    write_owner_only(machines_file, json.dumps(machines_data, indent=2) + "\n")
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
    print("2. Delete GitHub deploy keys:")
    print("   gh repo deploy-key list --repo <user>/<repo>")
    print(f"   # Find keys titled '{machine_id}' and delete them:")
    print("   gh repo deploy-key delete <key-id> --repo <user>/<repo>")
    print()
    print("3. Destroy remote machine:")
    print("   Option A: Delete user only")
    print("     ssh root@<ip> 'pkill -u agentwire; userdel -r agentwire'")
    print("   Option B: Destroy the VM entirely via provider console")
    print()
    print("4. Restart portal to pick up changes:")
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


def register_machine_parser(subparsers) -> None:
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
