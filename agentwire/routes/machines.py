"""Portal routes — machines domain (local + configured remote machines).

Part of the #560 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend on stdlib helpers plus ``ssh_base_opts`` /
``parse_session_name`` and on core attributes (``self.agent``, ``self.config``,
``self.machine_status_checker``), which resolve through the MRO of the composed
server class. ``_get_machines_data`` stays on the base server — it is consumed
by the dashboard WS, not by these routes.
"""

import asyncio
import json
import logging
import re
import socket

from aiohttp import web

from ..ssh import ssh_base_opts
from ..worktree import parse_session_name

logger = logging.getLogger(__name__)


class MachinesRoutesMixin:
    async def api_machine_status(self, request: web.Request) -> web.Response:
        """Get status for a specific machine.

        Returns online/offline status and session count for a machine.

        URL params:
            machine_id: The machine ID to check

        Response:
            {
                "status": "online" | "offline",
                "session_count": <int>
            }
        """
        machine_id = request.match_info["machine_id"]

        try:
            # Load machines config
            machines_dict = {}
            if hasattr(self.agent, 'machines'):
                for m in self.agent.machines:
                    machines_dict[m.get('id')] = m

            machine_config = machines_dict.get(machine_id)
            if not machine_config:
                return web.json_response(
                    {"status": "offline", "session_count": 0},
                    status=404
                )

            # Check machine status
            status = await self._check_machine_status(machine_config)

            # Count sessions for this machine
            sessions = self.agent.list_sessions()
            session_count = 0
            for name in sessions:
                _, _, session_machine = parse_session_name(name)
                if session_machine == machine_id:
                    session_count += 1

            return web.json_response({
                "status": status,
                "session_count": session_count,
            })
        except Exception as e:
            logger.error(f"Failed to get machine status for {machine_id}: {e}")
            return web.json_response(
                {"status": "offline", "session_count": 0},
                status=500
            )

    async def api_machines(self, request: web.Request) -> web.Response:
        """Get list of all machines (local + configured remotes).

        Uses progressive loading pattern - returns immediately with status='checking',
        background checks populate cache for subsequent requests.
        """
        machines = []

        # Always include local machine first
        local_hostname = socket.gethostname()
        local_ip = await self._resolve_hostname(local_hostname)
        machines.append({
            "id": "local",
            "host": local_hostname,
            "ip": local_ip,
            "local": True,
            "status": "online",
        })

        # Add configured remote machines using progressive loading pattern
        machines_file = self.config.machines.file
        if machines_file.exists():
            try:
                with open(machines_file) as f:
                    data = json.load(f)
                    remote_machines = [
                        {**m, "local": False}
                        for m in data.get("machines", [])
                    ]

                    # Progressive loading: returns cached or "checking" status
                    checked_machines = await self.machine_status_checker.get_with_status(
                        remote_machines,
                        check_fn=self._check_machine_with_ip,
                        id_field='id'
                    )

                    machines.extend(checked_machines)

            except (json.JSONDecodeError, IOError):
                pass

        return web.json_response(machines)

    async def _check_machine_with_ip(self, machine: dict) -> dict:
        """Check machine status and resolve IP. Used by CachedStatusChecker."""
        status = await self._check_machine_status(machine, quick=True)
        ip = None
        if status == "online":
            ip = await self._resolve_hostname(machine.get("host", ""))
        return {"status": status, "ip": ip}

    async def _resolve_hostname(self, hostname: str) -> str | None:
        """Resolve hostname to IP address.

        Tries DNS first, then falls back to SSH config resolution,
        and finally queries the remote machine for its IP.
        """
        if not hostname:
            return None

        # Try DNS lookup first
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, socket.gethostbyname, hostname)
            return result
        except (socket.gaierror, socket.herror):
            pass

        # DNS failed, try SSH config to get the actual hostname/IP
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-G", hostname,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            # Parse output for "hostname <value>"
            ssh_hostname = None
            for line in stdout.decode().splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "hostname":
                    ssh_hostname = parts[1]
                    break

            if ssh_hostname:
                # Check if it's already an IP address
                if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ssh_hostname):
                    return ssh_hostname

                # Try DNS on the resolved hostname
                try:
                    result = await loop.run_in_executor(None, socket.gethostbyname, ssh_hostname)
                    return result
                except (socket.gaierror, socket.herror):
                    pass

                # DNS failed, try connecting via SSH to get the remote IP
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ssh", *ssh_base_opts(), "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
                        hostname, "hostname -I 2>/dev/null || ip addr show | grep 'inet ' | grep -v '127.0.0.1' | head -1 | awk '{print $2}' | cut -d/ -f1",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
                    remote_ip = stdout.decode().strip().split()[0] if stdout else None
                    if remote_ip and re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', remote_ip):
                        return remote_ip
                except (asyncio.TimeoutError, OSError, IndexError):
                    pass

        except (asyncio.TimeoutError, OSError):
            pass

        return None

    async def _check_machine_status(self, machine: dict, quick: bool = False) -> str:
        """Check if a remote machine is reachable via SSH.

        Args:
            machine: Machine dict with host/user info
            quick: If True, use very short timeout for fast initial check
        """
        host = machine.get("host", "")
        user = machine.get("user", "")
        ssh_target = f"{user}@{host}" if user else host

        # Use shorter timeout for quick checks
        connect_timeout = "1" if quick else "2"
        wait_timeout = 1.5 if quick else 3.0

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "ssh", *ssh_base_opts(), "-o", f"ConnectTimeout={connect_timeout}", "-o", "BatchMode=yes",
                    ssh_target, "echo ok",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
                timeout=wait_timeout
            )
            await proc.wait()
            return "online" if proc.returncode == 0 else "offline"
        except (asyncio.TimeoutError, Exception):
            return "offline"

    async def api_add_machine(self, request: web.Request) -> web.Response:
        """Add a new machine to the registry."""
        try:
            data = await request.json()
            machine_id = data.get("id", "").strip()
            host = data.get("host", "").strip()
            user = data.get("user", "").strip()
            projects_dir = data.get("projects_dir", "").strip()

            if not machine_id or not host:
                return web.json_response({"error": "ID and host are required"})

            machines_file = self.config.machines.file
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
                return web.json_response({"error": f"Machine '{machine_id}' already exists"})

            # Add new machine
            new_machine = {"id": machine_id, "host": host}
            if user:
                new_machine["user"] = user
            if projects_dir:
                new_machine["projects_dir"] = projects_dir

            machines.append(new_machine)

            # Save
            with open(machines_file, "w") as f:
                json.dump({"machines": machines}, f, indent=2)

            # Reload agent backend to pick up new machines
            if self.agent and hasattr(self.agent, '_load_machines'):
                self.agent._load_machines()

            return web.json_response({"success": True, "machine": new_machine})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_remove_machine(self, request: web.Request) -> web.Response:
        """Remove a machine from the registry."""
        machine_id = request.match_info["machine_id"]

        try:
            # Can't remove local machine
            if machine_id == "local":
                return web.json_response({"error": "Cannot remove local machine"})

            machines_file = self.config.machines.file
            if not machines_file.exists():
                return web.json_response({"error": "No machines configured"})

            # Load machines
            try:
                with open(machines_file) as f:
                    data = json.load(f)
                    machines = data.get("machines", [])
            except (json.JSONDecodeError, IOError) as e:
                return web.json_response({"error": f"Failed to read machines file: {e}"})

            # Check if machine exists
            machine = next((m for m in machines if m.get("id") == machine_id), None)
            if not machine:
                return web.json_response({"error": f"Machine '{machine_id}' not found"})

            # Remove from machines list
            machines = [m for m in machines if m.get("id") != machine_id]

            # Save updated machines file
            with open(machines_file, "w") as f:
                json.dump({"machines": machines}, f, indent=2)
                f.write("\n")

            # No sessions.json to clean up - config is now in .agentwire.yml per project

            # Reload agent backend to pick up changes
            if self.agent and hasattr(self.agent, '_load_machines'):
                self.agent._load_machines()

            return web.json_response({
                "success": True,
                "machine_id": machine_id,
            })

        except Exception as e:
            logger.error(f"Failed to remove machine: {e}")
            return web.json_response({"error": str(e)})


def register_machines_routes(server, app):
    """Wire the machines domain's routes onto ``app``."""
    app.router.add_get("/api/machine/{machine_id}/status", server.api_machine_status)
    app.router.add_get("/api/machines", server.api_machines)
    app.router.add_post("/api/machines", server.api_add_machine)
    app.router.add_delete("/api/machines/{machine_id}", server.api_remove_machine)
