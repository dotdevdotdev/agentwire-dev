"""The unattended contract: agentwire's public "no human is present" signal.

``AGENTWIRE_UNATTENDED=1`` is set in the environment of every scheduler
dispatch (and inherited by the sessions that dispatch spawns). It means exactly
one thing: nobody is at the keyboard to answer a prompt. agentwire refuses a
few host-only actions under it (``agentwire tasks promote``); any other tool in
the session -- a permission hook, a wrapper script -- may read it too.

A task may also carry ``unattended_allow``, a list of grants. agentwire does not
interpret them: it serializes the list into ``AGENTWIRE_UNATTENDED_ALLOW`` as an
opaque string and passes it on. Whatever reads that variable owns its meaning
and its validation.
"""

from __future__ import annotations

import json
import os
from typing import Any

UNATTENDED_ENV = "AGENTWIRE_UNATTENDED"
UNATTENDED_ALLOW_ENV = "AGENTWIRE_UNATTENDED_ALLOW"
UNATTENDED_ENV_KEYS = (UNATTENDED_ENV, UNATTENDED_ALLOW_ENV)


def is_unattended() -> bool:
    """True when this process was dispatched with no human present."""
    return os.environ.get(UNATTENDED_ENV) == "1"


def encode_allow(entries: Any) -> str:
    """Serialize a task's ``unattended_allow`` list to an opaque env string.

    Plain strings become a comma-separated list; any structured entry makes the
    whole list compact JSON. Empty input yields ``""``.
    """
    items = list(entries or [])
    if any(not isinstance(e, str) for e in items):
        return json.dumps(items, separators=(",", ":"))
    return ",".join(e.strip() for e in items if e and e.strip())
