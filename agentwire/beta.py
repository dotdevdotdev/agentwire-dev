"""``agentwire.beta`` — the one implementation of the beta gate.

A beta feature ships on ``main`` and stays off until a user asks for it. The
flag VALUES live in :mod:`agentwire.config` (``BetaConfig``); the MECHANICS of
hiding gated text live here, because the same mechanism now serves two
model-facing surfaces and a third would otherwise grow its own copy:

- **role prompts** (markdown) — resolved in ``roles.parse_role_file``;
- **MCP tool descriptions** (docstrings) — resolved by :func:`gated_doc`.

Both are text a model reads and a user pays tokens for, which is the property
that decides what belongs behind a gate. The MCP schema was missed on the first
pass: ``msg_send``'s description grew ~316 characters of voice-buddy prose that
loaded into every agent session in every install, ungated, while the commit
message asserted the role prompts were the only such surface. The lesson is in
this module's existence — one gate, one place, and a byte-identity proof per
surface rather than per file.
"""

from __future__ import annotations

import re

#: A gated region:
#:
#:     <!-- beta:voice_layer -->
#:     ...text that only ships when beta.voice_layer is on...
#:     <!-- /beta:voice_layer -->
#:
#: Leading whitespace is allowed on both markers (a docstring cannot put them in
#: column 0) and the close tag may end the string with no trailing newline. Both
#: shapes previously failed OPEN — the region simply did not match, and its text
#: shipped ungated, which is the one failure direction a gate may not have.
#:
#: The backreference means open and close must name the SAME flag, so a mistyped
#: close tag matches nothing. That case still fails open — it cannot be resolved
#: without guessing where the region ends — and is caught instead by the audit
#: in ``tests/unit/test_beta_flag.py``, which asserts no marker-shaped text
#: survives :func:`apply_beta_blocks` in any shipped role file.
BETA_BLOCK_RE = re.compile(
    r"^[ \t]*<!-- beta:([a-z0-9_]+) -->[ \t]*\n(.*?)^[ \t]*<!-- /beta:\1 -->[ \t]*(?:\n|\Z)",
    re.MULTILINE | re.DOTALL,
)

#: Anything marker-SHAPED, paired or not. The audit's instrument: what
#: :data:`BETA_BLOCK_RE` leaves behind is exactly the set of broken markers.
LOOSE_MARKER_RE = re.compile(r"<!--\s*/?\s*beta:[^>]*-->")

#: A stray marker on its own line, removed at render time only — see
#: :func:`render` for why that is deliberately NOT done in
#: :func:`apply_beta_blocks`.
_STRAY_MARKER_LINE_RE = re.compile(
    r"^[ \t]*<!--\s*/?\s*beta:[^>]*-->[ \t]*(?:\n|\Z)", re.MULTILINE
)

_CACHE: "set[str] | None" = None


def flag_names() -> frozenset[str]:
    """The flags a marker may legally name. Owned by :mod:`agentwire.config`."""
    from .config import BETA_FLAG_NAMES

    return BETA_FLAG_NAMES


def enabled_flags() -> set[str]:
    """Which beta features are on, cached for the life of the process.

    Cached because the gate sits on a hot path: ``parse_role_file`` runs once
    per role per session launch, and an uncached read cost a full
    ``load_config`` — with its stderr INFO line — each time, taking ``roles
    list`` from 11ms to 242ms and printing 24 log lines, for a feature almost
    nobody has enabled.

    The consequence is stated rather than hidden: a long-lived process
    observes the flag state it started with, so turning a beta feature on takes
    the restart that ``config.yaml`` changes already take. An import-time
    surface like an MCP tool description is process-lifetime by construction
    anyway. :func:`reset_cache` exists for tests.

    **The precondition that makes the ON direction safe, written down because
    it is not visible from here.** A cached ``{"voice_layer"}`` outlives a
    config that has since said off — verified: gated prose keeps rendering
    until :func:`reset_cache`. That is unreachable today only because every
    in-process caller of ``load_roles``/``parse_role_file`` is a short-lived
    CLI module, and the surfaces that look long-lived are not: ``role show``
    and ``roles list`` shell out through ``run_agentwire_cmd``, so each render
    happens in a fresh process that re-reads the flag.

    **A long-lived in-process caller would violate that**, and would do it
    silently — the gate would keep a feature ON for a user who turned it off,
    with no error anywhere. If you add one (a portal route rendering role text
    in-process, a daemon calling ``parse_role_file``), either call
    :func:`reset_cache` at the top of each request or give that caller an
    explicit ``enabled`` set. Do not discover this precondition by tripping it.
    """
    global _CACHE
    if _CACHE is None:
        from .config import enabled_beta_flags

        _CACHE = enabled_beta_flags()
    return _CACHE


def reset_cache() -> None:
    """Drop the cached flag set (tests; a process that rewrote its own config)."""
    global _CACHE
    _CACHE = None


def apply_beta_blocks(text: str, enabled: set[str]) -> str:
    """Resolve every PAIRED gated region in *text*.

    Enabled flag → the body ships, with the marker lines removed (they are
    scaffolding, and shipping them would cost the tokens the gate exists to
    save). Disabled — or a flag name nothing knows about — → the whole region
    goes, markers included.

    **An unknown flag fails CLOSED**, and that direction is chosen rather than
    inherited: text no gate can turn off is precisely the failure this module
    exists to prevent. What makes it safe rather than silent is the paired
    audit — a test fails on any marker naming a flag ``config.BETA_FLAG_NAMES``
    does not know, so a typo is caught before it can delete a section forever.

    Deliberately does NOT remove unpaired markers: this function's output is
    what the audit measures, and swallowing broken markers here would make that
    audit unable to fail. :func:`render` does that, at the edge.
    """
    known = flag_names()

    def _resolve(match: "re.Match") -> str:
        flag, body = match.group(1), match.group(2)
        return body if (flag in known and flag in enabled) else ""

    return BETA_BLOCK_RE.sub(_resolve, text)


def render(text: str, enabled: "set[str] | None" = None) -> str:
    """Resolve gated regions for text about to reach a model.

    :func:`apply_beta_blocks`, then strip any marker line left over. That second
    step fixes only the smaller half of a broken marker: the region it was meant
    to wrap is still ungated (the audit is what catches that), but the
    scaffolding itself never reaches the model as prose.
    """
    # Text with no marker cannot change, so it must not pay for the flag
    # lookup — that is 20 of the 24 shipped role files and 107 of the 108 MCP
    # tool descriptions, several of which resolve at IMPORT time. Cheap
    # substring scan, and it keys on the same literal the regexes do.
    if enabled is None:
        if "beta:" not in text:
            return text
        enabled = enabled_flags()
    return _STRAY_MARKER_LINE_RE.sub("", apply_beta_blocks(text, enabled))


def gated_doc(fn):
    """Resolve gated regions in a function's docstring, in place.

    For MCP tools, apply it BELOW ``@mcp.tool()`` so FastMCP reads the resolved
    text::

        @mcp.tool()
        @gated_doc
        def msg_send(...):
            \"\"\"...\"\"\"

    Decorators apply bottom-up, so the docstring is already resolved by the time
    the tool is registered — and FastMCP snapshots the docstring AT
    registration, publishing it as the tool description, which is what makes
    this the whole fix.

    **Inverting the two silently un-gates the tool**, measured rather than
    feared: with ``@gated_doc`` above ``@mcp.tool()`` the raw text publishes,
    markers and all (2165 chars vs 1800). Nothing about the wrong order looks
    wrong, so the property is pinned over the WHOLE registry —
    ``test_no_published_description_carries_a_marker`` fails on any published
    description containing a marker, whatever put it there.

    The resolution happens ONCE, at import, against whatever flag state the
    process started with — so any later reader that wants to verify the
    resolved text needs to know which flags it was resolved WITH, not which
    flags are on now. ``__beta_enabled__`` records exactly that (and
    ``__beta_raw_doc__`` the pre-resolution text), which is what lets the
    byte-identity tests hold on a machine with a beta flag enabled instead of
    silently asserting the developer's personal config (#1023).
    """
    if fn.__doc__ and "beta:" in fn.__doc__:
        enabled = enabled_flags()
        fn.__beta_raw_doc__ = fn.__doc__
        fn.__beta_enabled__ = frozenset(enabled)
        fn.__doc__ = render(fn.__doc__, enabled=enabled)
    return fn
