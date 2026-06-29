"""Portal routes — artifacts domain (agent-generated HTML windows).

Handlers moved verbatim from ``AgentWireServer`` for the #560 server.py split
(#585). They depend only on ``self.config`` and the filesystem; the ``/artifacts``
static mount is wired in the registrar.
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class ArtifactsRoutesMixin:
    async def api_artifacts_upload(self, request):
        """POST /api/artifacts/upload — write HTML content to the artifacts directory."""
        try:
            data = await request.json()
            filename = data.get("filename")
            content = data.get("content")

            if not filename or not content:
                return web.json_response(
                    {"success": False, "error": "filename and content required"}, status=400
                )

            # Sanitize filename — only allow safe characters
            import re
            if not re.match(r'^[a-zA-Z0-9_\-][a-zA-Z0-9_\-\.]*\.html$', filename):
                return web.json_response(
                    {"success": False, "error": "filename must be alphanumeric with .html extension"},
                    status=400,
                )

            # Check size
            max_bytes = self.config.artifacts.max_size_mb * 1024 * 1024
            if len(content.encode('utf-8')) > max_bytes:
                return web.json_response(
                    {"success": False, "error": f"content too large (max {self.config.artifacts.max_size_mb}MB)"},
                    status=400,
                )

            # Ensure artifacts directory exists
            artifacts_dir = self.config.artifacts.dir
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            # Write file atomically (write to temp, rename)
            filepath = artifacts_dir / filename
            tmp_path = filepath.with_suffix('.tmp')
            tmp_path.write_text(content, encoding='utf-8')
            tmp_path.rename(filepath)

            logger.info(f"Artifact written: {filepath}")
            return web.json_response({
                "success": True,
                "path": str(filepath),
                "url": f"/artifacts/{filename}",
            })

        except Exception as e:
            logger.error(f"Artifact upload failed: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def api_artifacts_list(self, request):
        """GET /api/artifacts — list files in the artifacts directory."""
        try:
            artifacts_dir = self.config.artifacts.dir
            if not artifacts_dir.exists():
                return web.json_response([])

            files = []
            for f in sorted(artifacts_dir.iterdir()):
                if f.is_file() and not f.name.startswith('.'):
                    stat = f.stat()
                    files.append({
                        "name": f.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
            return web.json_response(files)

        except Exception as e:
            logger.error(f"Artifacts list failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_artifacts_delete(self, request):
        """DELETE /api/artifacts/{filename} — delete a file from the artifacts directory."""
        import re
        filename = request.match_info["filename"]

        # Sanitize — prevent path traversal
        if not re.match(r'^[a-zA-Z0-9_\-][a-zA-Z0-9_\-\.]*$', filename):
            return web.json_response(
                {"success": False, "error": "invalid filename"}, status=400
            )

        filepath = self.config.artifacts.dir / filename
        if not filepath.exists():
            return web.json_response(
                {"success": False, "error": "file not found"}, status=404
            )

        try:
            filepath.unlink()
            logger.info(f"Artifact deleted: {filepath}")
            return web.json_response({"success": True})
        except Exception as e:
            logger.error(f"Artifact delete failed: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)


def register_artifacts_routes(server, app):
    """Wire the artifacts domain's routes (and static mount) onto ``app``."""
    app.router.add_post("/api/artifacts/upload", server.api_artifacts_upload)
    app.router.add_get("/api/artifacts", server.api_artifacts_list)
    app.router.add_delete("/api/artifacts/{filename:.+}", server.api_artifacts_delete)
    artifacts_dir = server.config.artifacts.dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/artifacts", artifacts_dir)
