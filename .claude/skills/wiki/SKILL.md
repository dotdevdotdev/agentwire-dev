---
name: wiki
description: "Manage the agentwire LLM Wiki knowledge base. Use when ingesting raw sources, querying accumulated knowledge, or running health checks on the wiki. Subcommands: /wiki ingest, /wiki query <question>, /wiki lint (incl. ground-truth audit against the codebase)"
---

# AgentWire Wiki

LLM-maintained knowledge base at `~/.agentwire/wiki/`. Read `~/.agentwire/wiki/CLAUDE.md` for the full schema and conventions.

## Subcommands

### `/wiki ingest`

Process new files in `~/.agentwire/wiki/raw/` into wiki pages.

**Steps:**
1. List all files in `raw/` that haven't been processed (check for a `.ingested` marker or compare against existing wiki pages)
2. For each file, read its contents
3. Identify entities: technologies, patterns, APIs, research topics
4. For each entity, check if a wiki page already exists in `wiki/<category>/<name>.md`
   - **Exists**: update the page with new information, bump `last_updated`
   - **New**: create the page following the schema in CLAUDE.md
5. Add wikilinks `[[page-name]]` for cross-references between pages
6. Report what was created/updated

**Do NOT** modify or delete files in `raw/`. They are immutable source material.

### `/wiki query <question>`

Answer a question grounded in wiki content.

**Steps:**
1. Search `~/.agentwire/wiki/wiki/` for relevant pages (use Grep/Glob)
2. Read the relevant pages
3. Synthesize an answer citing specific wiki pages
4. If the wiki doesn't have enough information, say so clearly — do not hallucinate
5. If you discover new knowledge while answering, offer to update the relevant wiki pages

### `/wiki lint`

Health check the wiki. Two passes: a **structural** pass (links, freshness, frontmatter) and a **ground-truth audit** that verifies concrete claims against the actual codebase.

**Structural pass — steps:**
1. Scan all pages in `wiki/` for:
   - **Stale pages**: `last_updated` older than 90 days
   - **Orphaned pages**: no other page links to them via `[[wikilink]]`
   - **Broken wikilinks**: `[[page-name]]` that point to non-existent pages
   - **Missing frontmatter**: pages without the required YAML frontmatter
2. Report findings grouped by severity
3. Do NOT auto-fix — let the user decide what to do

#### Ground-truth audit

The wiki accrues concrete claims about the codebase — `agentwire` subcommands and flags, repo file paths, config keys, qualified Python symbols — that nothing ever verifies. Left unchecked they rot into confident-but-wrong, which is worse than no wiki. This audit extracts the checkable assertions from every page and flags the ones that no longer resolve against the source.

**Run it** (from a checkout of the agentwire repo — stdlib only, no build/install needed):

```bash
python -m agentwire.wiki_audit                 # audit ~/.agentwire/wiki/wiki against this repo
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
