"""Portal routes — projects domain (project discovery, creation, roles, defaults).

Handlers moved verbatim from ``AgentWireServer`` for the #560 server.py split.
Every ``self.`` reference to shared state / core helpers resolves through the
MRO of the composed server class. The ``_scan_machine_projects`` helper is
projects-private (only called by ``api_projects``) so it moves here too.
"""

import asyncio
import json
import logging
import re
import shlex
import subprocess
from pathlib import Path

from aiohttp import web

from ..ssh import ssh_base_opts

logger = logging.getLogger(__name__)


class ProjectsRoutesMixin:
    async def api_projects(self, request: web.Request) -> web.Response:
        """List discovered projects (progressive loading).

        Query params:
            machine: Optional machine ID to filter by (e.g., 'local', 'mac-studio')

        Response:
            {"projects": [{name, path, posture, roles, machine, status}, ...]}
        """
        try:
            # Get list of machines to scan
            machine_filter = request.query.get("machine")

            if machine_filter:
                # Single machine requested - use checker
                machines = [{"id": machine_filter}]
                scanned_machines = await self.projects_checker.get_with_status(
                    machines,
                    check_fn=self._scan_machine_projects,
                    id_field='id'
                )
                all_projects = []
                for machine_data in scanned_machines:
                    projects = machine_data.get("projects", [])
                    logger.debug(f"[api_projects] Machine {machine_data.get('id')} returned {len(projects)} projects (filtered request)")
                    all_projects.extend(projects)
            else:
                # All machines - get local first (fast), then remote (progressive)
                all_projects = []

                # Local projects (always fast, no caching needed)
                local_result = await self._scan_machine_projects({"id": "local"})
                local_projects = local_result.get("projects", [])
                logger.debug(f"[api_projects] Local scan returned {len(local_projects)} projects")
                all_projects.extend(local_projects)

                # Remote projects (progressive with caching)
                machines_file = self.config.machines.file
                if machines_file.exists():
                    with open(machines_file) as f:
                        data = json.load(f)
                        remote_machines = [
                            {"id": m.get("id")}
                            for m in data.get("machines", [])
                        ]
                        logger.debug(f"[api_projects] Found {len(remote_machines)} remote machines: {[m['id'] for m in remote_machines]}")

                        if remote_machines:
                            scanned_machines = await self.projects_checker.get_with_status(
                                remote_machines,
                                check_fn=self._scan_machine_projects,
                                id_field='id'
                            )
                            logger.debug(f"[api_projects] Checker returned {len(scanned_machines)} machine results")

                            # Track if any machines are still checking
                            has_checking = False

                            for machine_data in scanned_machines:
                                machine_id = machine_data.get("id", "unknown")
                                machine_status = machine_data.get("status", "unknown")
                                projects = machine_data.get("projects", [])
                                logger.debug(f"[api_projects] Machine {machine_id} (status: {machine_status}) has {len(projects)} projects: {[p.get('name', 'unnamed') for p in projects]}")

                                if machine_status == "checking":
                                    has_checking = True

                                # Add machine status to projects for frontend progressive loading
                                for project in projects:
                                    project["_machineStatus"] = machine_status

                                all_projects.extend(projects)

                logger.debug(f"[api_projects] Total projects before dedup: {len(all_projects)}")

                # Deduplicate by normalized path
                # Normalize paths to handle ~/projects vs /Users/user/projects
                def normalize_path(path: str) -> str:
                    """Normalize path for comparison (expand ~, resolve relative paths)."""
                    if not path:
                        return ""
                    # Expand ~ to home directory
                    if path.startswith("~/"):
                        # Use a consistent home path for comparison
                        import os
                        home = os.path.expanduser("~")
                        return path.replace("~", home, 1)
                    return path

                seen_normalized = set()
                deduped_projects = []
                duplicates = []
                for project in all_projects:
                    path = project.get("path")
                    if not path:
                        continue

                    machine = project.get("machine", "local")
                    dedup_key = f"{machine}:{normalize_path(path)}"
                    if dedup_key not in seen_normalized:
                        seen_normalized.add(dedup_key)
                        deduped_projects.append(project)
                    else:
                        # Prefer local version over remote for same project
                        duplicates.append(f"{project.get('name')} ({project.get('machine')})")

                if duplicates:
                    logger.debug(f"[api_projects] Removed {len(duplicates)} duplicates: {', '.join(duplicates)}")

                logger.debug(f"[api_projects] Total projects after dedup: {len(deduped_projects)}")
                all_projects = deduped_projects

            # Return projects with scanning status for auto-refresh
            response = {"projects": all_projects}
            if 'has_checking' in locals():
                response["_scanning"] = has_checking

            return web.json_response(response)
        except Exception as e:
            logger.error(f"Failed to list projects: {e}")
            return web.json_response({"projects": []})

    async def _scan_machine_projects(self, machine: dict) -> dict:
        """Scan projects on a specific machine. Used by CachedStatusChecker."""
        machine_id = machine.get("id")
        try:
            args = ["projects", "list", "--machine", machine_id]

            success, result = await self.run_agentwire_cmd(args)
            if not success:
                logger.warning(f"Failed to scan projects on {machine_id}: {result.get('error', 'unknown error')}")
                return {"status": "offline", "projects": []}

            projects = result.get("projects", [])
            logger.debug(f"Found {len(projects)} projects on {machine_id}")
            return {
                "status": "online",
                "projects": projects
            }
        except Exception as e:
            logger.error(f"Exception scanning projects on {machine_id}: {e}")
            return {"status": "offline", "projects": []}

    async def api_projects_create(self, request: web.Request) -> web.Response:
        """Create a new local project.

        Body:
            {
                "name": "myproject",          # required, alphanumerics + ._-
                "clone_url": "git@..."         # optional, clone from this URL
                "git_init": false              # optional, init empty git repo (ignored with clone_url)
            }

        Response:
            {"success": true, "name": "...", "path": "...", "machine": "local"}
            {"success": false, "error": "..."}
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON body"}, status=400)

        name = (data.get("name") or "").strip()
        clone_url = (data.get("clone_url") or "").strip() or None
        git_init = bool(data.get("git_init"))

        if not name:
            return web.json_response({"success": False, "error": "name is required"}, status=400)

        args = ["projects", "create", name]
        if clone_url:
            args.extend(["--from", clone_url])
        elif git_init:
            args.append("--git-init")

        success, result = await self.run_agentwire_cmd(args)
        if not success:
            return web.json_response({"success": False, "error": result.get("error", "Unknown error")}, status=400)
        return web.json_response(result)

    async def api_projects_delete(self, request: web.Request) -> web.Response:
        """Delete a project (remove .agentwire.yml or entire folder).

        Body:
            {
                "path": "/path/to/project",
                "machine": "machine-id" or null for local,
                "deleteType": "config" | "folder"
            }

        Response:
            {"success": true} or {"success": false, "error": "message"}
        """
        try:
            data = await request.json()
            path = data.get("path")
            machine = data.get("machine")
            delete_type = data.get("deleteType")

            if not path or not isinstance(path, str):
                return web.json_response({"success": False, "error": "Missing path"})
            if delete_type not in ("config", "folder"):
                return web.json_response({"success": False, "error": "Invalid deleteType"})

            # Path validation: absolute, no traversal, no shell metacharacters.
            # The endpoint has no auth (local-trust model — see SECURITY.md), so
            # treat the input as untrusted regardless and reject anything that
            # could escape argv quoting on either local or remote (SSH) execution.
            if not path.startswith("/"):
                return web.json_response({"success": False, "error": "path must be absolute"})
            if ".." in Path(path).parts:
                return web.json_response({"success": False, "error": "path may not contain '..'"})
            if re.search(r"[\s;&|`$<>(){}\[\]\\\"'*?#]", path):
                return web.json_response({"success": False, "error": "path contains disallowed characters"})
            if path.rstrip("/") in ("", "/root", "/home", "/Users", "/tmp", "/etc") or path.rstrip("/") in ("~", "$HOME"):
                return web.json_response({"success": False, "error": "Cannot delete protected paths"})

            # Build argv. For SSH we still need to cross a remote shell, so
            # quote with shlex; locally we use array form with shell=False.
            if delete_type == "config":
                target = f"{path.rstrip('/')}/.agentwire.yml"
                local_argv = ["rm", "-f", target]
            else:
                local_argv = ["rm", "-rf", path]

            if machine and machine != "local":
                # Remote shell — quote each argv element through shlex.
                remote_cmd = " ".join(shlex.quote(a) for a in local_argv)
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["ssh", *ssh_base_opts(), machine, remote_cmd],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            else:
                result = await asyncio.to_thread(
                    subprocess.run,
                    local_argv,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

            if result.returncode != 0:
                return web.json_response({
                    "success": False,
                    "error": result.stderr or "Delete command failed"
                })

            return web.json_response({"success": True})

        except asyncio.TimeoutError:
            return web.json_response({"success": False, "error": "Operation timed out"})
        except Exception as e:
            logger.error(f"Failed to delete project: {e}")
            return web.json_response({"success": False, "error": str(e)})

    async def api_session_defaults(self, request: web.Request) -> web.Response:
        """Resolve a new session's defaults via the CLI (the single resolver).

        Query params: kind (default orchestrator), posture.
        Response: {kind, posture, resolved_posture, roles, postures}.
        The new-session UI reads this instead of hardcoding posture or the
        intrinsic role chips.
        """
        kind = request.query.get("kind", "orchestrator")
        posture = request.query.get("posture")
        args = ["session-defaults", "--kind", kind]
        if posture:
            args += ["--posture", posture]
        success, result = await self.run_agentwire_cmd(args)
        if not success:
            return web.json_response({"error": result.get("error", "Failed to resolve defaults")}, status=400)
        return web.json_response(result)

    async def api_roles(self, request: web.Request) -> web.Response:
        """List available roles.

        Response:
            {"roles": [{name, description}, ...]}
        """
        try:
            success, result = await self.run_agentwire_cmd(["roles", "list"])
            if not success:
                return web.json_response({"roles": []})

            return web.json_response({"roles": result.get("roles", [])})
        except Exception as e:
            logger.error(f"Failed to list roles: {e}")
            return web.json_response({"roles": []})

    async def api_check_path(self, request: web.Request) -> web.Response:
        """Check if a path exists and is a git repo.

        Query params:
            path: The path to check
            machine: Machine ID ('local' or remote machine ID)

        Returns:
            {exists: bool, is_git: bool, current_branch: str|null}
        """
        path = request.query.get("path", "")
        machine = request.query.get("machine", "local")

        if not path:
            return web.json_response({
                "exists": False,
                "is_git": False,
                "current_branch": None
            })

        # Thin wrapper: the git/SSH logic lives in the CLI (SSOT).
        success, result = await self.run_agentwire_cmd(
            ["repo-info", "--path", path, "--machine", machine]
        )
        if not success:
            logger.error(f"repo-info failed for {path} on {machine}: {result.get('error')}")
            return web.json_response(
                {"exists": False, "is_git": False, "current_branch": None},
                status=500,
            )

        return web.json_response({
            "exists": result.get("exists", False),
            "is_git": result.get("is_git", False),
            "current_branch": result.get("current_branch"),
        })

    async def api_check_branches(self, request: web.Request) -> web.Response:
        """Get existing branch names matching a prefix.

        Query params:
            path: The git repo path
            machine: Machine ID ('local' or remote machine ID)
            prefix: Branch name prefix to filter by

        Returns:
            {existing: [branch names]}
        """
        path = request.query.get("path", "")
        machine = request.query.get("machine", "local")
        prefix = request.query.get("prefix", "")

        if not path:
            return web.json_response({"existing": []})

        # Thin wrapper: the git/SSH logic lives in the CLI (SSOT).
        success, result = await self.run_agentwire_cmd(
            ["branches", "--path", path, "--machine", machine, "--prefix", prefix]
        )
        if not success:
            logger.error(f"branches failed for {path} on {machine}: {result.get('error')}")
            return web.json_response({"existing": []}, status=500)

        return web.json_response({"existing": result.get("existing", [])})

    async def api_worktree_cleanup(self, request: web.Request) -> web.Response:
        """Tear down an orphaned worktree (Session HUD ghost card "Clean up").

        Thin wrapper over the plain `agentwire worktree --remove` form — the
        CLI's own merge/open-PR guard decides whether the branch is also
        deleted; this never escalates to --force-delete-branch.

        Body: {"name": "<branch or session>", "project": "/path/to/repo"}
        Returns whatever the CLI returns verbatim: on success
        {success, session, path, killed, worktree_removed, branch,
        branch_deleted, branch_note, orphaned_tabs}; on failure
        {success: false, error}.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON body"}, status=400)

        name = (data.get("name") or "").strip()
        project = (data.get("project") or "").strip()
        if not name or not project:
            return web.json_response({"success": False, "error": "name and project are required"}, status=400)

        # Thin wrapper: the git/registry/branch-safety logic lives in the CLI (SSOT).
        success, result = await self.run_agentwire_cmd(
            ["worktree", "--remove", name, "-p", project]
        )
        return web.json_response(result, status=200 if success else 400)

    async def api_worktree_adopt(self, request: web.Request) -> web.Response:
        """Spawn a session into an existing worktree (Session HUD ghost card "Adopt").

        Thin wrapper over `agentwire worktree <name> -p <project> --existing
        [--created-by <createdBy>]` — checks out the branch already on disk
        (no new branch) and, when the dead session's recorded creator is
        known, roots the new session under that same parent so it reports
        back the way the original session would have.

        Body: {"name": "<branch>", "project": "/path/to/repo", "createdBy": "<session>"}
        Returns whatever the CLI returns verbatim (same shape as `agentwire new`'s
        json — success/session/path/... — or {success: false, error}).
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON body"}, status=400)

        name = (data.get("name") or "").strip()
        project = (data.get("project") or "").strip()
        created_by = (data.get("createdBy") or "").strip()
        if not name or not project:
            return web.json_response({"success": False, "error": "name and project are required"}, status=400)

        args = ["worktree", name, "-p", project, "--existing"]
        if created_by:
            args += ["--created-by", created_by]

        success, result = await self.run_agentwire_cmd(args)
        return web.json_response(result, status=200 if success else 400)


def register_projects_routes(server, app):
    """Wire the projects domain's routes onto ``app``."""
    app.router.add_get("/api/projects", server.api_projects)
    app.router.add_post("/api/projects/create", server.api_projects_create)
    app.router.add_post("/api/projects/delete", server.api_projects_delete)
    app.router.add_get("/api/roles", server.api_roles)
    app.router.add_get("/api/session/defaults", server.api_session_defaults)
    app.router.add_get("/api/check-path", server.api_check_path)
    app.router.add_get("/api/check-branches", server.api_check_branches)
    app.router.add_post("/api/worktree/cleanup", server.api_worktree_cleanup)
    app.router.add_post("/api/worktree/adopt", server.api_worktree_adopt)
