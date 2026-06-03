# Decision: Obsidian / Karpathy LLM Wiki as cross-machine second brain

> Decision record for [issue #183](https://github.com/dotdevdotdev/agentwire-dev/issues/183).
> Research/decision task — **no code shipped in this PR**. This file records yes/no/defer
> for each proposed direction and resolves the open questions for the ones we accept.

## Context

We already run the Karpathy LLM Wiki pattern at `~/.agentwire/wiki/` — git-versioned
markdown maintained by the orchestrator and agents, with `/wiki ingest`, `/wiki query`,
`/wiki lint` skills and a scheduled `wiki-ingest` task that processes anything dropped
into `~/.agentwire/wiki/raw/`. The "Obsidian as a second brain" hype is the *same pattern*
with Obsidian as a viewer/editor layer on top of the folder.

The pattern holds up for a small, curated, single-author knowledge base (≈70x cheaper than
RAG, human-readable, portable) and breaks down at enterprise scale (hallucinations on
unanswerable questions, errors propagate across linked pages, sync conflicts compound).
The `lint` step is non-negotiable. These decisions stay inside the pattern's sweet spot.

## Decisions — the 6 directions

| # | Direction | Decision | One-line reasoning |
|---|---|---|---|
| 1 | Open `~/.agentwire/wiki/` as an Obsidian vault | **YES** | Zero code — point Obsidian at the existing folder for graph view, wikilink autocomplete, and mobile access; pure documentation. |
| 2 | Per-session MCP attachment so workers share the orchestrator's knowledge | **DEFER** | Workers can already read the wiki via its filesystem path; an MCP attachment is a nice-to-have, not a blocker, and is implementation work out of scope here. |
| 3 | **Cross-machine wiki sync** ★ | **YES** | Highest-interest thread; backend chosen below (git remote, private repo). |
| 4 | **`/note` voice-capture → `raw/` → overnight ingest** ★ | **YES** | Highest-interest thread; STT (Moonshine) is already wired and the `wiki-ingest` task already drains `raw/` — capture is the only missing piece. Sub-questions resolved below. |
| 5 | Graph view inside the portal | **NO** | Medium-high cost to rebuild what Direction 1 already gives us for free via Obsidian; not worth duplicating. |
| 6 | Single shared vault across agentwire + fragmentz + future projects | **DEFER** | Trivial to symlink, but mixing project scopes risks cross-contaminating retrieval; revisit once cross-machine sync (Direction 3) is proven stable. |

## Resolved open questions

### Direction 3 — Cross-machine sync

**Backend: git remote (private GitHub repo).**

| Option | Verdict | Why |
|---|---|---|
| **Git remote (private repo)** | ✅ chosen | The wiki is already git-versioned markdown; sync is `pull`/`push`. Explicit history, real diffs, mergeable conflicts, no extra daemon, no subscription. Matches the existing release/identity workflow. |
| Obsidian Sync | ❌ | Paid, vendor lock-in, and double-syncing a git repo invites two conflict-resolution systems fighting each other. |
| iCloud | ❌ | Free but conflict-prone on rapidly-edited folders — exactly the failure mode the issue warns about, and Karpathy-pattern errors propagate across linked pages. |
| Syncthing | ❌ | Works, but adds an always-on daemon per machine for no benefit over git here. |

Sync hygiene:
- **`git pull` before the `wiki-ingest` task runs, `git push` after** — so ingest never races a remote edit and machines converge once per cycle.
- Stagger machine sync windows; do not let two machines ingest the same `raw/` files simultaneously.
- Keep simultaneous human edits rare — the pattern's error-propagation risk compounds merge conflicts.

**Location:** keep the vault at `~/.agentwire/wiki/`. It is already the documented home, the
scheduler points at it, and CLAUDE.md references it — making it a git repo with a remote is
additive, not a move. No relocation.

### Direction 4 — Voice → wiki capture

| Open question | Decision | Why |
|---|---|---|
| Worker pane vs portal-only | **Portal push-to-talk button → STT → `raw/`** (plus a thin `/note` skill for terminal users that writes the same file) | No pane-spawn overhead, lowest latency, and capture stays a dumb write — the LLM work happens later at ingest. |
| One file per note vs daily-journal append | **One file per note** | Cleaner provenance, trivial to ingest-then-move, and avoids append conflicts — which matter once Direction 3 sync is live. |
| Immediate vs batch ingest | **Batch overnight via the existing `wiki-ingest` scheduler task** | Reuses the SSOT drain of `raw/`; no new pipeline. Capture is instant, structuring is deferred. |
| Auto-tag at capture vs defer-to-ingest | **Defer to ingest** | Capture stays fast and offline-safe; the ingest LLM tags with full wiki context, which produces better tags than a capture-time guess. |

Net: a voice note lands as a single timestamped markdown file in `~/.agentwire/wiki/raw/`,
and the nightly `wiki-ingest` run structures, tags, and files it into `wiki/`, then moves
the raw file to `raw/processed/`. No new scheduled task required.

### General

- **Obsidian as the recommended viewer: YES.** Document the setup (open the folder as a vault,
  read/lightweight-edit only). Explicitly recommend **not** enabling Obsidian Sync, so git
  (Direction 3) stays the single sync authority and we avoid dueling conflict resolvers.
- **Interaction with existing scheduled tasks:** the voice-capture flow deliberately rides the
  current `wiki-ingest` task instead of adding a new one. The only new requirement is the
  git pull/push ordering around that task (see Direction 3). The `/wiki lint` health check
  remains mandatory and gains importance as sync and voice capture add more pages.

## Reference material

- [Karpathy gist (original pattern)](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [Mehul Gupta — "LLM Wiki is a Bad Idea"](https://medium.com/data-science-in-your-pocket/andrej-karpathys-llm-wiki-is-a-bad-idea-8c7e8953c618) — strongest critique weighed above
- [iansinnott/obsidian-claude-code-mcp](https://github.com/iansinnott/obsidian-claude-code-mcp) — MCP plugin inside Obsidian
- [obsidian-web-mcp](https://www.reddit.com/r/ObsidianMD/comments/1rwmuiq/) — sync-safe MCP over Cloudflare Tunnel (mobile)
- [eugeniughelbur/obsidian-second-brain](https://github.com/eugeniughelbur/obsidian-second-brain) — cross-CLI skill
- [r/ClaudeCode discussion](https://www.reddit.com/r/ClaudeCode/comments/1sm374u/) — community variants

## Status

Directions **1, 3, 4** accepted; **2, 6** deferred; **5** declined. Implementation of the
accepted directions is tracked as follow-up work, not part of this decision record.
