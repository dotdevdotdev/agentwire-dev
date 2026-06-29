"""Route-table snapshot guard for the #560 server.py split.

Each domain slice moves handlers into a ``routes/<domain>.py`` mixin and its
registration into ``register_<domain>_routes``. A route accidentally dropped
from a registrar would 404 silently at runtime with nothing else catching it.

Every slice is a PURE RELOCATION: the full ``(method, canonical-path)`` route
*set* is invariant — only *where* each route registers changes. So this guard
freezes the complete set and asserts equality. Any dropped or added route
fails loudly, on every slice — not just the spot-checked handful.

``BASELINE_ROUTES`` is the contract. Update it deliberately ONLY when routes
genuinely change (a real feature add/remove) — diff the assertion delta first
to confirm the change is exactly what you intended.
"""

from agentwire.config import Config
from agentwire.server import AgentWireServer

# Complete (method, canonical-path) set registered by a fresh AgentWireServer.
# Frozen baseline — see module docstring before editing.
BASELINE_ROUTES = frozenset({
    ("DELETE", "/api/artifacts/{filename}"),
    ("DELETE", "/api/machines/{machine_id}"),
    ("DELETE", "/api/scratchpad/notes/{note_id}"),
    ("DELETE", "/api/sessions/{name}"),
    ("GET", "/"),
    ("GET", "/api/artifacts"),
    ("GET", "/api/check-branches"),
    ("GET", "/api/check-path"),
    ("GET", "/api/config"),
    ("GET", "/api/council/archive"),
    ("GET", "/api/council/live"),
    ("GET", "/api/council/sittings"),
    ("GET", "/api/council/status"),
    ("GET", "/api/desktop/notifications"),
    ("GET", "/api/desktop/windows"),
    ("GET", "/api/history"),
    ("GET", "/api/history/{session_id}"),
    ("GET", "/api/icons/{category}"),
    ("GET", "/api/machine/{machine_id}/status"),
    ("GET", "/api/machines"),
    ("GET", "/api/projects"),
    ("GET", "/api/push/config"),
    ("GET", "/api/review/{name}"),
    ("GET", "/api/roles"),
    ("GET", "/api/safety/logs"),
    ("GET", "/api/safety/rules"),
    ("GET", "/api/safety/status"),
    ("GET", "/api/scheduler/board"),
    ("GET", "/api/scheduler/events"),
    ("GET", "/api/scheduler/live"),
    ("GET", "/api/scheduler/output"),
    ("GET", "/api/scheduler/tasks/{name}/events"),
    ("GET", "/api/scratchpad"),
    ("GET", "/api/services/custom"),
    ("GET", "/api/session/defaults"),
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/local"),
    ("GET", "/api/sessions/remote"),
    ("GET", "/api/sessions/{name}/connections"),
    ("GET", "/api/voice-status"),
    ("GET", "/api/voices"),
    ("GET", "/api/worktrees"),
    ("GET", "/artifacts"),
    ("GET", "/health"),
    ("GET", "/manifest.webmanifest"),
    ("GET", "/mobile"),
    ("GET", "/pair"),
    ("GET", "/service-worker.js"),
    ("GET", "/static/{path}"),
    ("GET", "/ws"),
    ("GET", "/ws/terminal/{name}"),
    ("GET", "/ws/{name}"),
    ("HEAD", "/"),
    ("HEAD", "/api/artifacts"),
    ("HEAD", "/api/check-branches"),
    ("HEAD", "/api/check-path"),
    ("HEAD", "/api/config"),
    ("HEAD", "/api/council/archive"),
    ("HEAD", "/api/council/live"),
    ("HEAD", "/api/council/sittings"),
    ("HEAD", "/api/council/status"),
    ("HEAD", "/api/desktop/notifications"),
    ("HEAD", "/api/desktop/windows"),
    ("HEAD", "/api/history"),
    ("HEAD", "/api/history/{session_id}"),
    ("HEAD", "/api/icons/{category}"),
    ("HEAD", "/api/machine/{machine_id}/status"),
    ("HEAD", "/api/machines"),
    ("HEAD", "/api/projects"),
    ("HEAD", "/api/push/config"),
    ("HEAD", "/api/review/{name}"),
    ("HEAD", "/api/roles"),
    ("HEAD", "/api/safety/logs"),
    ("HEAD", "/api/safety/rules"),
    ("HEAD", "/api/safety/status"),
    ("HEAD", "/api/scheduler/board"),
    ("HEAD", "/api/scheduler/events"),
    ("HEAD", "/api/scheduler/live"),
    ("HEAD", "/api/scheduler/output"),
    ("HEAD", "/api/scheduler/tasks/{name}/events"),
    ("HEAD", "/api/scratchpad"),
    ("HEAD", "/api/services/custom"),
    ("HEAD", "/api/session/defaults"),
    ("HEAD", "/api/sessions"),
    ("HEAD", "/api/sessions/local"),
    ("HEAD", "/api/sessions/remote"),
    ("HEAD", "/api/sessions/{name}/connections"),
    ("HEAD", "/api/voice-status"),
    ("HEAD", "/api/voices"),
    ("HEAD", "/api/worktrees"),
    ("HEAD", "/artifacts"),
    ("HEAD", "/health"),
    ("HEAD", "/manifest.webmanifest"),
    ("HEAD", "/mobile"),
    ("HEAD", "/pair"),
    ("HEAD", "/service-worker.js"),
    ("HEAD", "/static/{path}"),
    ("HEAD", "/ws"),
    ("HEAD", "/ws/terminal/{name}"),
    ("HEAD", "/ws/{name}"),
    ("POST", "/api/active-session"),
    ("POST", "/api/answer/{name}"),
    ("POST", "/api/artifacts/upload"),
    ("POST", "/api/config"),
    ("POST", "/api/config/reload"),
    ("POST", "/api/council/ask"),
    ("POST", "/api/council/start"),
    ("POST", "/api/council/stop"),
    ("POST", "/api/create"),
    ("POST", "/api/desktop/collage"),
    ("POST", "/api/desktop/layout"),
    ("POST", "/api/desktop/notification"),
    ("POST", "/api/desktop/notification/dismiss"),
    ("POST", "/api/desktop/window/close"),
    ("POST", "/api/desktop/window/focus"),
    ("POST", "/api/desktop/window/minimize-all"),
    ("POST", "/api/desktop/window/open"),
    ("POST", "/api/desktop/window/tile"),
    ("POST", "/api/history/{session_id}/resume"),
    ("POST", "/api/local-tts/{name}"),
    ("POST", "/api/machines"),
    ("POST", "/api/notify"),
    ("POST", "/api/pair"),
    ("POST", "/api/permission/{name}"),
    ("POST", "/api/permission/{name}/respond"),
    ("POST", "/api/projects/create"),
    ("POST", "/api/projects/delete"),
    ("POST", "/api/push/subscribe"),
    ("POST", "/api/push/unsubscribe"),
    ("POST", "/api/review/{name}/answer"),
    ("POST", "/api/safety/config"),
    ("POST", "/api/say/{name}"),
    ("POST", "/api/scheduler/start"),
    ("POST", "/api/scheduler/stop"),
    ("POST", "/api/scheduler/tasks/{name}/disable"),
    ("POST", "/api/scheduler/tasks/{name}/enable"),
    ("POST", "/api/scheduler/tasks/{name}/run"),
    ("POST", "/api/scratchpad/changed"),
    ("POST", "/api/scratchpad/notes"),
    ("POST", "/api/session/{name}/broadcast"),
    ("POST", "/api/session/{name}/config"),
    ("POST", "/api/session/{name}/fork"),
    ("POST", "/api/session/{name}/recreate"),
    ("POST", "/api/session/{name}/restart-service"),
    ("POST", "/api/session/{name}/spawn-sibling"),
    ("POST", "/api/sessions/refresh"),
    ("POST", "/send/{name}"),
    ("POST", "/transcribe"),
    ("POST", "/upload"),
    ("PUT", "/api/scratchpad/notes/{note_id}"),
})


def _route_set(app):
    routes = set()
    for r in app.router.routes():
        canonical = getattr(r.resource, "canonical", None)
        if canonical is None:
            continue
        routes.add((r.method, canonical))
    return routes


def test_route_table_unchanged():
    """The full route set must exactly match the frozen baseline.

    Load-bearing for every #560 slice: because each slice only relocates
    handlers, the set is invariant. A dropped route (missing from the new
    registrar) or a stray addition fails here with an explicit delta.
    """
    server = AgentWireServer(Config())
    actual = _route_set(server.app)

    missing = BASELINE_ROUTES - actual
    extra = actual - BASELINE_ROUTES
    assert not missing and not extra, (
        "Route table drifted from the frozen baseline.\n"
        f"  DROPPED (in baseline, not registered): {sorted(missing)}\n"
        f"  ADDED   (registered, not in baseline): {sorted(extra)}\n"
        "If a slice merely relocated routes, a non-empty delta means a route "
        "was lost or duplicated — fix the registrar. Update BASELINE_ROUTES "
        "only when routes genuinely change (real feature add/remove)."
    )
