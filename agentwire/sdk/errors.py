"""Shared classification of claude-agent-sdk errors.

Both the workflow `anthropic` runner and the agentwire REPL surface SDK
errors to humans (morning reports, terminal output). They benefit from the
same one-word tag distinguishing rate-limit blips from real bugs, so the
substring tables live here once.
"""

from __future__ import annotations

_TRANSIENT_MARKERS = (
    "overloaded", "rate_limit", "rate limit", " 429", " 529", " 503",
)
_AUTH_MARKERS = ("authentication", "unauthorized", " 401", " 403")
_INVALID_MARKERS = ("invalid_request", " 400", "validation")


def classify(err_type: str, err_msg: str) -> str:
    """Return one of: 'transient', 'permanent', 'invalid', 'error'."""
    haystack = f"{err_type} {err_msg}".lower()
    if any(m in haystack for m in _TRANSIENT_MARKERS):
        return "transient"
    if any(m in haystack for m in _AUTH_MARKERS):
        return "permanent"
    if any(m in haystack for m in _INVALID_MARKERS):
        return "invalid"
    return "error"
