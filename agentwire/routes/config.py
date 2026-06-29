"""Portal routes — config domain (read/save/reload ~/.agentwire/config.yaml).

Handlers moved verbatim from ``AgentWireServer`` for the #560 server.py split
(#585). Security-critical keys are frozen (#425); ``api_reload_config`` calls the
base ``self.init_backends`` via the composed class MRO.
"""

import logging
import re
from pathlib import Path

from aiohttp import web

from ..security import frozen_config_violations, restore_redactions

logger = logging.getLogger(__name__)


class ConfigRoutesMixin:
    async def api_get_config(self, request: web.Request) -> web.Response:
        """Get config file contents or display format.

        Query params:
            format=display - Return key/value pairs for UI display
        """
        # Check if display format requested
        if request.query.get("format") == "display":
            # Return flattened key/value pairs from current config
            items = [
                {"key": "TTS Backend", "value": self.config.tts.backend},
                {"key": "TTS URL", "value": self.config.tts.url or "—"},
                {"key": "TTS Default Voice", "value": self.config.tts.default_voice},
                {"key": "STT Backend", "value": self.config.stt.backend},
                {"key": "STT URL", "value": self.config.stt.url or "—"},
                {"key": "Server Host", "value": self.config.server.host},
                {"key": "Server Port", "value": self.config.server.port},
                {"key": "SSL Enabled", "value": self.config.server.ssl.enabled},
                {"key": "Projects Directory", "value": str(self.config.projects.dir)},
                {"key": "Worktrees Enabled", "value": self.config.projects.worktrees.enabled},
                {"key": "Worktrees Suffix", "value": self.config.projects.worktrees.suffix},
                {"key": "Agent Command", "value": self.config.agent.command},
                {"key": "Machines File", "value": str(self.config.machines.file)},
            ]
            return web.json_response({"items": items})

        # Default: return raw config file contents
        config_path = Path.home() / ".agentwire" / "config.yaml"
        content = ""
        if config_path.exists():
            try:
                content = config_path.read_text()
                # SECURITY: Redact sensitive fields before returning
                # Matches patterns like: api_key: "secret" or auth_token: secret
                content = re.sub(
                    r'((?:api_key|auth_token)\s*:\s*)["\']?[^"\'\n]+["\']?',
                    r'\1"[REDACTED]"',
                    content
                )
            except IOError as e:
                return web.json_response({"error": str(e)})
        else:
            # Return default config template (instant mode: browser voice, loopback)
            content = """# AgentWire Configuration
server:
  host: "127.0.0.1"
  port: 8765

tts:
  backend: "default"  # browser voice — or "custom" with url: pointing at your shim
  # url: "http://localhost:8100"

stt:
  backend: "default"  # browser speech recognition — or "cloud" (hosted API),
                      # or "custom" with url:
  # url: "http://localhost:8101"
  # cloud:  # any OpenAI-compatible transcription API; key from env, never in config
  #   base_url: "https://api.openai.com/v1"
  #   model: "gpt-4o-mini-transcribe"
  #   api_key_env: "OPENAI_API_KEY"

projects:
  dir: "~/projects"
  worktrees:
    enabled: true
    suffix: "-worktrees"
"""
        return web.json_response({
            "path": str(config_path),
            "content": content,
            "exists": config_path.exists(),
        })

    async def api_save_config(self, request: web.Request) -> web.Response:
        """Save config file contents.

        Security-critical keys are frozen (#425): even a valid token cannot use
        this endpoint to disable auth, move the bind host, rewrite the
        executables/services that run as RCE, or turn off the damage-control
        rules. Those are host-file-edit-only.
        """
        try:
            data = await request.json()
            content = data.get("content", "")

            # Validate YAML syntax
            import yaml
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as e:
                return web.json_response({"error": f"Invalid YAML: {e}"})

            config_path = Path.home() / ".agentwire" / "config.yaml"
            old_content = config_path.read_text() if config_path.exists() else ""

            # Reverse the read-side secret redaction so saving the editor's text
            # back doesn't overwrite real secrets with "[REDACTED]" (and so the
            # frozen-key check below sees the true auth_token, not the marker).
            content = restore_redactions(content, old_content)

            violations = frozen_config_violations(content, old_content)
            if violations:
                return web.json_response(
                    {
                        "error": (
                            "These keys are frozen and can only be changed by "
                            "editing ~/.agentwire/config.yaml on the host: "
                            + ", ".join(violations)
                        ),
                        "frozen_keys": violations,
                    },
                    status=403,
                )

            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(content)

            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_reload_config(self, request: web.Request) -> web.Response:
        """Reload configuration from disk."""
        try:
            from ..config import reload_config
            self.config = reload_config()

            # Reinitialize backends with new config
            await self.init_backends()

            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)})


def register_config_routes(server, app):
    """Wire the config domain's routes onto ``app``."""
    app.router.add_get("/api/config", server.api_get_config)
    app.router.add_post("/api/config", server.api_save_config)
    app.router.add_post("/api/config/reload", server.api_reload_config)
