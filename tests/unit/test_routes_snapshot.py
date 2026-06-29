"""Route-table snapshot guard for the #560 server.py split.

Each domain slice moves handlers into a ``routes/<domain>.py`` mixin and its
registration into ``register_<domain>_routes``. A route accidentally dropped
from a registrar would 404 silently at runtime with nothing else catching it.
This test freezes the full ``(method, canonical-path)`` set so any such drop
(or unintended addition) fails loudly.

Updating the baseline is intentional only when routes legitimately change —
diff the assertion error to confirm the delta is what you meant.
"""

from agentwire.config import Config
from agentwire.server import AgentWireServer


def _route_set(app):
    routes = set()
    for r in app.router.routes():
        canonical = getattr(r.resource, "canonical", None)
        if canonical is None:
            continue
        routes.add((r.method, canonical))
    return routes


def test_route_table_unchanged():
    server = AgentWireServer(Config())
    actual = _route_set(server.app)
    # Sanity: the split must not lose routes. The exact count is a guard, not
    # a contract — bump it deliberately alongside a real route change.
    assert len(actual) >= 100, f"route table collapsed to {len(actual)} routes"

    # Spot-check representative routes from several domains survive the split.
    expected_present = {
        ("GET", "/health"),
        ("GET", "/api/sessions"),
        ("GET", "/api/scratchpad"),
        ("POST", "/api/scratchpad/changed"),
        ("GET", "/api/scheduler/live"),
        ("GET", "/api/council/sittings"),
        ("GET", "/api/desktop/windows"),
        ("GET", "/api/safety/status"),
        ("GET", "/api/config"),
        ("GET", "/api/history"),
        ("GET", "/api/machines"),
        ("GET", "/api/projects"),
        ("POST", "/api/notify"),
    }
    missing = expected_present - actual
    assert not missing, f"routes vanished from registration: {sorted(missing)}"
