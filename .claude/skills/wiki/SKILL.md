---
name: wiki
description: "Manage the agentwire LLM Wiki knowledge base. Authoring is in-context; mechanical ops (query, lint, status, new, done) run through the deterministic `agentwire wiki` CLI / `wiki_*` MCP tools. Use when recording knowledge, querying it, or health-checking the wiki. Subcommands: /wiki ingest, /wiki query <question>, /wiki lint (incl. ground-truth audit against the codebase)"
---

# AgentWire Wiki

LLM-maintained knowledge base at `~/.agentwire/wiki/`. Read `~/.agentwire/wiki/CLAUDE.md` for the full schema and conventions.

**How the loop actually works:** authoring is **in-context** — the session that learns something writes the page itself (`Write`/`Edit` into `wiki/<category>/<name>.md`), with full context, for free. That's the working path; there is **no scheduled batch ingester**. The *mechanical* parts — searching, health-checking, scaffolding, archiving sources — are deterministic and run through the `agentwire wiki` CLI (and the `wiki_query` / `wiki_lint` / `wiki_status` MCP tools), exactly like `/handoff` splits "LLM distills in-context, CLI renders deterministically."

| Mechanical op | Command |
|---|---|
| Search the wiki | `agentwire wiki query <q> [--limit N] [--json]` / MCP `wiki_query` |
| Health-check (structural + ground-truth) | `agentwire wiki lint [--strict] [--json]` / MCP `wiki_lint` |
| Overview (counts, unprocessed raw, health) | `agentwire wiki status [--json]` / MCP `wiki_status` |
| Scaffold a page with correct frontmatter | `agentwire wiki new <category> <name> [--title T]` |
| Archive a consumed source | `agentwire wiki done <rawfile>` |

(All stdlib-only — they run from a source checkout with no rebuild.)

## Subcommands

### `/wiki ingest`

**Authoring is in-context and primary.** When you discover something worth keeping, write or update the page *now*, in your own session — `agentwire wiki new <category> <name>` scaffolds the frontmatter, then you fill in the body and add `[[page-name]]` wikilinks. This is how the wiki is maintained; don't wait for a batch job (there isn't one).

`raw/` is an **optional verbatim-source inbox** — a place to stash an article, gist, transcript, or findings dump you might author from later. It is **not** the primary path and nothing drains it automatically.

**To author from a stranded raw source:**
1. Read the file in `raw/`.
2. Identify entities (technologies, patterns, APIs, research topics) and, for each, create (`agentwire wiki new …`) or update the matching `wiki/<category>/<name>.md` — in-context, citing the raw file as a source.
3. Add `[[page-name]]` wikilinks for cross-references.
4. When the source is consumed, archive it: **`agentwire wiki done <rawfile>`** moves `raw/<f>` → `raw/processed/<f>` (content is never edited — only relocated, so immutability holds; "unprocessed = files directly in `raw/`" stays a trivial check, no `.ingested` marker).

**Never edit the *content* of files in `raw/`** — they are immutable source material. `wiki done` only relocates them.

### `/wiki query <question>`

Answer a question grounded in wiki content.

**Steps:**
1. **`agentwire wiki query "<question>"`** (or MCP `wiki_query`) — deterministic ranked search (name/title weighted over body), returns top pages with `path + score + snippet`.
2. Read the top pages it points at.
3. Synthesize an answer citing specific wiki pages. (The CLI does **not** call an LLM — *you* synthesize, in your own context.)
4. If the wiki doesn't have enough information, say so clearly — do not hallucinate.
5. If you discover new knowledge while answering, write/update the relevant wiki pages (in-context).

### `/wiki lint`

Health check the wiki. Two passes: a **structural** pass (links, freshness, frontmatter) and a **ground-truth audit** that verifies concrete claims against the actual codebase. Both run from one command:

```bash
agentwire wiki lint            # structural + ground-truth, report-only
agentwire wiki lint --json     # machine-readable findings
agentwire wiki lint --strict   # exit 1 when issues found (CI)
```

**Structural pass (implemented as code in `agentwire/wiki.py`):**
- **Stale pages**: `last_updated` older than 90 days
- **Orphaned pages**: no other page links to them via `[[wikilink]]`
- **Broken wikilinks**: `[[page-name]]` pointing to a non-existent page
- **Missing/invalid frontmatter**: no frontmatter, missing `name`/`last_updated`, or an unparseable date

It does NOT auto-fix — review and decide.

#### Ground-truth audit

The wiki accrues concrete claims about the codebase — `agentwire` subcommands and flags, repo file paths, config keys, qualified Python symbols — that nothing ever verifies. Left unchecked they rot into confident-but-wrong, which is worse than no wiki. This audit extracts the checkable assertions from every page and flags the ones that no longer resolve against the source. **`agentwire wiki lint` folds this pass in automatically** alongside the structural checks; the standalone module below is the same engine, handy for pointing at a different wiki/codebase.

**Run it** (from a checkout of the agentwire repo — stdlib only, no build/install needed):

```bash
agentwire wiki lint                            # structural + this ground-truth pass
python -m agentwire.wiki_audit                 # ground-truth pass only, against this repo
python -m agentwire.wiki_audit --json          # machine-readable findings
python -m agentwire.wiki_audit --strict        # exit 1 when drift is found (CI)
python -m agentwire.wiki_audit \
  --wiki-dir <dir> --repo-dir <repo>           # point at a different wiki / codebase
```

Each finding is reported as `wiki_file:line  [kind] claim  → reason`, so you can jump straight to the stale line.

**What it checks (precision over recall — it would rather miss a stale claim than cry wolf on a true one):**

| Kind | Claim it verifies | How |
|------|-------------------|-----|
| `subcommand` | `` `agentwire <cmd>` `` in a code span | `<cmd>` must be a registered `add_parser("<cmd>")` |
| `flag` | `--flag` inside an `agentwire …` command span | must be a declared `add_argument("--flag")` |
| `path` | repo-relative paths under `agentwire/ docs/ scripts/ tests/ examples/ .claude/ .github/` | must exist on disk |
| `symbol` | `` `module.symbol` `` where `agentwire/<module>.py` exists | `symbol` must be defined in that module |
| `config-key` | dotted key near a config-file mention | every segment must be a field on a `@dataclass` in `config.py` |

Scoping that keeps it quiet: flags/subcommands are only read inside code spans (bare prose mentioning `agentwire` or a flag isn't a claim); paths under the wiki's own `wiki/`/`raw/` trees and `~`/absolute paths are ignored; common method calls (`config.get`), filenames (`__main__.py`), and version strings (`agentwire v1.35.1`) are not mistaken for claims.

**Act on it:** treat each finding as "a human renamed/removed/moved something and the wiki didn't follow." Confirm against the code, then update the wiki page (or, if the page is a deliberate historical record — e.g. a retrospective on removed code — leave it and note that in the page). As with the structural pass, the audit **never auto-fixes**.

## Guidelines

- Always read `~/.agentwire/wiki/CLAUDE.md` first for the current schema
- One page per entity — check before creating duplicates
- Practical over theoretical — "how we use it" and "what broke" over textbook definitions
- Include code snippets, commands, config examples
- Date your updates in frontmatter `last_updated`
- Cite sources (URLs, commit hashes, issue numbers)
