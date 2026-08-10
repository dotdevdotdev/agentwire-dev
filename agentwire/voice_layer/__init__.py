"""``agentwire.voice_layer`` — an EXPERIMENTAL realtime voice buddy (spike).

A realtime voice model the owner talks to about the fleet. It is **not a
coding harness** and must never become one — see :doc:`docs/wiki/voice-layer`
for the boundary and the reasoning behind it. The package ships on ``main``
but the feature is gated on ``beta.voice_layer`` (default off); nothing here is
wired into the portal, the scheduler, or any ungated command path.

Module map:

- :mod:`~agentwire.voice_layer.identity` — the buddy's session identity, so
  the existing inbox/cohort/notify machinery addresses it like any other
  session without it ever owning a tmux session.
- :mod:`~agentwire.voice_layer.delivery` — the ONE new piece of plumbing: a
  delivery adapter for inbox messages addressed to a session that has no pane
  to paste into.
- :mod:`~agentwire.voice_layer.realtime` — mints an OpenAI Realtime ephemeral
  client secret (``POST /v1/realtime/client_secrets``).
- :mod:`~agentwire.voice_layer.tools` — the fleet-awareness tool surface (the
  READS, plus the dispatcher), through the ``agentwire`` CLI (the documented
  SSOT). Which capabilities may ever appear there is ruled by
  :mod:`~agentwire.voice_layer.surface`'s tier audit.
- :mod:`~agentwire.voice_layer.write_tools` — the buddy's writes. It has one:
  ``msg send`` to a session that is already running, and it is reachable only
  through :mod:`~agentwire.voice_layer.confirm`'s spoken-nonce gate. The entry
  above used to say "the read-only fleet-awareness tool surface", and kept
  saying it for as long as the write path has shipped.
- :mod:`~agentwire.voice_layer.instructions` — the buddy persona prompt.
"""
