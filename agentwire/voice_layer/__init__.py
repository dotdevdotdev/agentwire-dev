"""``agentwire.voice_layer`` — an EXPERIMENTAL realtime voice buddy (spike).

A realtime voice model the owner talks to about the fleet. It is **not a
coding harness** and must never become one — see :doc:`docs/wiki/voice-layer`
for the boundary and the reasoning behind it. This package is branch-only
foundation work; nothing here is wired into the portal, the scheduler, or any
shipped command path.

Module map:

- :mod:`~agentwire.voice_layer.identity` — the buddy's session identity, so
  the existing inbox/cohort/notify machinery addresses it like any other
  session without it ever owning a tmux session.
- :mod:`~agentwire.voice_layer.delivery` — the ONE new piece of plumbing: a
  delivery adapter for inbox messages addressed to a session that has no pane
  to paste into.
- :mod:`~agentwire.voice_layer.realtime` — mints an OpenAI Realtime ephemeral
  client secret (``POST /v1/realtime/client_secrets``).
- :mod:`~agentwire.voice_layer.tools` — the read-only fleet-awareness tool
  surface, dispatched through the ``agentwire`` CLI (the documented SSOT).
- :mod:`~agentwire.voice_layer.instructions` — the buddy persona prompt.
"""
