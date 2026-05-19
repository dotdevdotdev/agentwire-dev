"""Eligibility gates for mission dispatch.

Two gates AND'd:

1. Issue carries the ``agent-ready`` label and is in state ``OPEN``.
2. Issue body contains a case-insensitive ``## Acceptance criteria`` header,
   followed (before the next ``##`` header) by at least one bullet
   (``- ...``, ``* ...``, or ``+ ...``).

Pure logic — no I/O.
"""

from __future__ import annotations

import re

from agentwire.missions.github import Issue

AGENT_READY_LABEL = "agent-ready"

_HEADER_RE = re.compile(
    r"^[ \t]*##[ \t]+acceptance[ \t]+criteria[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_NEXT_HEADER_RE = re.compile(r"^[ \t]*##[ \t]+\S+", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+(\S.*)$", re.MULTILINE)


def has_agent_ready_label(issue: Issue) -> bool:
    return AGENT_READY_LABEL in issue.labels


def extract_acceptance_criteria(body: str) -> list[str] | None:
    """Extract bullet text under the first ``## Acceptance criteria`` header.

    Returns the list of bullet text (stripped, marker removed), or None if
    the header is missing or no bullets follow it before the next ``##``.
    """
    if not body:
        return None
    header = _HEADER_RE.search(body)
    if not header:
        return None
    start = header.end()
    next_header = _NEXT_HEADER_RE.search(body, pos=start)
    end = next_header.start() if next_header else len(body)
    section = body[start:end]
    bullets = [m.group(1).strip() for m in _BULLET_RE.finditer(section)]
    return bullets or None


def is_eligible(issue: Issue) -> tuple[bool, str]:
    """Returns ``(eligible, reason_if_not_eligible)``."""
    if issue.state != "OPEN":
        return False, f"issue state is {issue.state or '<unknown>'}, not OPEN"
    if not has_agent_ready_label(issue):
        return False, f"missing label '{AGENT_READY_LABEL}'"
    criteria = extract_acceptance_criteria(issue.body)
    if not criteria:
        return False, "no '## Acceptance criteria' section with bullets"
    return True, ""
