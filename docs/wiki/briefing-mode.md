# Briefing Mode

> Asymmetric-verbosity orchestration. A terse, human-facing **anchor** fans out exhaustively-verbose **correspondent** worktrees; correspondents go deep and signal passively; the anchor pulls on the human's cue and briefs across two complementary channels (voice + screen). Many-deep-into-one-concise.
>
> Design + feasibility history: [`research/briefing-mode-feasibility.md`](research/briefing-mode-feasibility.md).

## The shape

```
HUMAN ──"research X"──▶ ANCHOR ──worktree_create(roles=correspondent, prompt=…)──▶ N CORRESPONDENTS
                         │ (terse, never self-driven)                                │ (verbose, isolated)
HUMAN ◀──say(text,display)── ANCHOR ◀──msg_pull (on human's cue)── inbox ◀──msg ingest(ref)── each files
                                          reads ref'd report files          (passive, never drives)
```

The anchor is the calm funnel: pushed only by the human, pulling from correspondents on its own cadence.

## The two roles

| Role | Spawn | Behavior |
|---|---|---|
| **`anchor`** | `agentwire new -s <name> --roles anchor` | Replaces the orchestrator persona. Terse with the human; fans out correspondents; **acts only on the human's cue**; briefs asymmetrically; tears down. |
| **`correspondent`** | `worktree_create(name, roles="correspondent", prompt=…)` (or `agentwire worktree <name> --roles correspondent`) | Stacks on the worktree-session safety rail. Exhaustive; files a report to the dropbox; signals passively. |

`anchor` *replaces* the persona (persona kind); `correspondent` *adds to* the worktree-session etiquette (safety-rail kind) — see [roles](roles.md).

## The two channels (asymmetric brief)

One call hits both, with **deliberately different content**:

```
say(text="<punchy spoken headline>", display="<richer scannable card>")
```

- **`text` (voice)** — the verdict + the single most important thing.
- **`display` (screen toast, via `notify_user`)** — the structured summary with tradeoffs, paths, and **[links](url)**. Renders a safe markdown subset (bold, links, line breaks).

Need a toast without speaking? `notify_user(text, priority="high")`.

## Awareness without being driven

Correspondents never interrupt the anchor. They drop a **passive** pointer:

```
agentwire msg send --to <anchor> --kind ingest --ref "<report-path>" "<topic>"
```

`ingest` is never auto-delivered (see [messaging](sessions/messaging.md)). The anchor collects pointers only when the human cues it:

```
msg_pull()        # consumes the ingest pointers; each carries a typed ref: path
```

…then reads the referenced report files, synthesizes across them, and briefs. The reports persist in the dropbox after teardown.

## The dropbox

Correspondents file into a per-anchor directory, resolved (and created) by:

```
agentwire research ensure -s <anchor>     # CLI → ~/.agentwire/research/<anchor>/
research_dir()                            # MCP, defaults to the calling session
```

The big report lives here as a file; the `ingest` message only points at it.

## Lifecycle / teardown

```
worktree_remove("<angle>")   # kill session + remove worktree + branch + unregister, in one
worktree_prune()             # GC stale registry entries after many spawn/teardown cycles
```

## Surface added for Briefing Mode

- **MCP:** `worktree_create` / `worktree_status` / `worktree_list` / `worktree_remove` / `worktree_prune`; `msg_pull` / `msg_flush`; `research_dir`; `say(display=)`; `notify_user` / `notify_parent` / `notify_event`.
- **CLI:** `agentwire worktree --status` / `--prune` / `--prompt`; `agentwire msg pull`; `agentwire research dir|ensure`; `agentwire say --display`; `agentwire notify-user`.
- **Inbox:** the passive `ingest` kind + typed `ref` field.

Shipped across three phases (#430-group → #433 → #435 → #437).
