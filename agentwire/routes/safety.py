"""Portal routes — safety domain (damage-control status/logs/rules; frozen config).

Handlers moved verbatim from ``AgentWireServer`` for the #560 server.py split
(#585). Read-only views over the damage-control policy files; the config POST is
frozen (#425).
"""

import json
import logging
from datetime import datetime
from typing import Dict

from aiohttp import web

logger = logging.getLogger(__name__)


class SafetyRoutesMixin:
    async def api_safety_status(self, request: web.Request) -> web.Response:
        """Damage-control current state: master enable, disabled rules, today's counts."""
        try:
            from ..safety_commands import LOGS_DIR, load_patterns
            patterns = load_patterns()
            today = datetime.now().strftime("%Y-%m-%d")
            log_file = LOGS_DIR / f"{today}.jsonl"
            counts: Dict[str, int] = {}
            if log_file.exists():
                try:
                    with open(log_file, "r") as f:
                        for line in f:
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            d = entry.get("decision", "")
                            counts[d] = counts.get(d, 0) + 1
                except Exception:
                    pass
            # Read the kill switch + disabled rules from the agent-unwritable
            # damage-control policy files (#466), not from config.yaml.
            from ..safety._core import load_safety_config
            safety_cfg = load_safety_config()
            return web.json_response({
                "enabled": safety_cfg.get("enabled", True),
                "disabled_rules": list(safety_cfg.get("disabled_rules", []) or []),
                "rule_count": len(patterns.get("bashToolPatterns", [])),
                "today_counts": counts,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_safety_logs(self, request: web.Request) -> web.Response:
        """Recent safety audit log entries with optional filters."""
        try:
            from ..safety_commands import query_audit_logs
            limit = int(request.query.get("limit", "200"))
            decision = request.query.get("decision")
            entries = query_audit_logs()  # all entries, newest-first by file
            if decision:
                wanted = {d.strip() for d in decision.split(",") if d.strip()}
                entries = [e for e in entries if e.get("decision") in wanted]
            if limit > 0:
                entries = entries[-limit:]
            return web.json_response({"entries": entries})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_safety_rules(self, request: web.Request) -> web.Response:
        """Flat list of bash-rule IDs with their patterns/reasons."""
        try:
            from ..safety_commands import load_patterns
            patterns = load_patterns()
            rules = []
            for p in patterns.get("bashToolPatterns", []):
                if not isinstance(p, dict):
                    continue
                rules.append({
                    "id": p.get("id"),
                    "pattern": p.get("pattern"),
                    "reason": p.get("reason"),
                    "ask": bool(p.get("ask", False)),
                    "bypassable": bool(p.get("bypassable", False)),
                    "source": p.get("source"),
                })
            return web.json_response({"rules": rules})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_safety_config_post(self, request: web.Request) -> web.Response:
        """Frozen (#425): the safety block is host-file-edit-only.

        Disabling the damage-control rules with the same token that the rules are
        meant to contain defeats defense-in-depth. The master switch and
        disabled-rules list can now only be changed by editing
        ~/.agentwire/damagecontrol.yml on the host — itself a protected,
        agent-unwritable control-plane file (#466). GET /api/safety/* still works
        for viewing status, rules and logs.
        """
        return web.json_response(
            {
                "error": (
                    "Safety configuration is frozen and can only be changed by "
                    "editing ~/.agentwire/damagecontrol.yml on the host, then "
                    "reloading the portal."
                ),
                "frozen_keys": ["safety"],
            },
            status=403,
        )


def register_safety_routes(server, app):
    """Wire the safety domain's routes onto ``app``."""
    app.router.add_get("/api/safety/status", server.api_safety_status)
    app.router.add_get("/api/safety/logs", server.api_safety_logs)
    app.router.add_get("/api/safety/rules", server.api_safety_rules)
    app.router.add_post("/api/safety/config", server.api_safety_config_post)
