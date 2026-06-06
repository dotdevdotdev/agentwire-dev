"""The agentwire council — a multi-soul orchestrator session group (#213).

An ``agentwire-council`` orchestrator session fans user prompts out to a
roster of lens sessions (``council-brain``, ``council-conscience``, …), each
carrying the shared ``council-member`` protocol role plus its own
``council-<lens>`` role. Souls reply through a file-based inbox
(``~/.agentwire/council/prompts/NNNN/replies/``) with exactly one of: a
substantive **take**, an **ack** (researching, follow-up coming), or a
**pass** (nothing to add). The orchestrator collects and synthesizes,
attributed by lens.

Modules:

- ``state``  — sitting lifecycle state (roster, sessions, prompt counter)
- ``inbox``  — per-prompt reply inbox (the fan-out/collect protocol)
- ``cli``    — handlers for ``agentwire council ...`` subcommands
"""
