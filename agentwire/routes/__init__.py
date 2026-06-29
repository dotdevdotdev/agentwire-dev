"""Per-domain aiohttp route modules for the portal.

The portal's ``AgentWireServer`` was a single god-class owning every route
handler (#560). Handlers are being split per-domain, mirroring the #495
``mcp_server`` split — but with one structural twist: portal handlers share
mutable ``self`` state (active_sessions, dashboard_clients, caches, …), so
they cannot become stateless free functions the way MCP tools did.

The seam is therefore a **handler mixin per domain**:

* Each ``routes/<domain>.py`` defines ``class <Domain>RoutesMixin`` whose
  methods are the domain's handlers, moved verbatim — ``self.`` references to
  shared state and core helpers stay intact and resolve through the final
  class's MRO.
* ``AgentWireServer`` inherits every mixin, so the composed instance still
  exposes all handlers and all state on ``self`` exactly as before. Tests that
  do ``AgentWireServer(config)`` / ``server.broadcast_dashboard = AsyncMock()``
  keep working unchanged.
* Each module also exposes ``register_<domain>_routes(server, app)`` which runs
  that domain's ``app.router.add_*`` calls. ``_setup_routes`` calls these in a
  registrar loop, so adding a domain is a new module + one import + one append.
"""
