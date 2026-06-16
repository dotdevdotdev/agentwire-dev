"""The agentwire council — a multi-soul orchestrator session group (#213).

Sittings are **namespaced by ``<name>``** so independent councils run
concurrently. An ``agentwire-council-<name>`` orchestrator session fans user
prompts out to a roster of lens sessions (``council-<name>-brain``,
``council-<name>-conscience``, …), each carrying the shared ``council-member``
protocol role plus its own ``council-<lens>`` role. Souls reply through a
file-based inbox (``~/.agentwire/council/<name>/prompts/NNNN/replies/``) with
exactly one of: a substantive **take**, an **ack** (researching, follow-up
coming), or a **pass** (nothing to add). The orchestrator collects and
synthesizes, attributed by lens.

All state for a sitting lives under ``~/.agentwire/council/<name>/``
(``sitting.json`` + ``workspace/`` + ``prompts/``). The ``<name>`` is the
source of truth — never recover a name/lens by splitting a session string;
``sitting.json`` is the SSOT lens→session map.

Modules:

- ``state``  — sitting lifecycle state (roster, sessions, prompt counter)
- ``inbox``  — per-prompt reply inbox (the fan-out/collect protocol)
- ``cli``    — handlers for ``agentwire council ...`` subcommands
"""
