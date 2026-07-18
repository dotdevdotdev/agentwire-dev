# The Council

> Multi-soul orchestrator: fan a prompt out to distinct lens sessions, collect
> their takes, synthesize with attribution. The bundled `soul` role is the
> blended default voice ([#212](https://github.com/dotdevdotdev/agentwire-dev/issues/212));
> the council unbundles it into its constituent lenses
> ([#213](https://github.com/dotdevdotdev/agentwire-dev/issues/213)).

## Mental model

A **sitting** is one `council start` → `council stop` span, **namespaced by
`<name>`** so independent councils run concurrently (one per project/decision).
It comprises:

- The **orchestrator** — the `agentwire-council-<name>` session (role
  `council-orchestrator`). You talk to it; it fans out, collects, and
  synthesizes.
- The **souls** — one `council-<name>-<lens>` session per roster lens, each
  loading the shared `council-member` protocol role plus its own
  `council-<lens>` lens role.

### Targeting (which sitting a command hits)

`<name>` is identity — deterministic and inspectable, never a hidden
active-pointer. Every command resolves the same way and **echoes which sitting
it acted on** (`→ council 'agentwire-dev' (prompt #3)`):

1. explicit `--name`, else
2. the **cwd-repo-slug** if it matches a live sitting, else
3. the **sole** live sitting, else
4. **error** + the candidate list (0 live → `no council for this repo`; N live
   and ambiguous → refuse, demand `--name`). Never guesses by recency.

`<name>` is validated by the lens grammar (`[a-z0-9][a-z0-9-]*` — tmux-safe);
cwd-derived defaults are slugified + capped (~24 chars, short path-hash when
truncated, derived from repo/worktree root so N worktrees don't collide). The
lens→session map lives in `sitting.json` — **never** recover a name/lens by
splitting a session string.

Default roster:

| Lens | Looks at |
|------|----------|
| `brain` | Research, predictions, stats, sense-checking claims |
| `conscience` | Ethics, audience reception, trust implications |
| `gut` | Instinct — one short visceral read |
| `critic` | The weakest load-bearing assumption in the premise |
| `historian` | What we tried before, what worked, what didn't |
| `devils-advocate` | The strongest opposing case, argued in good faith |

All of a sitting's sessions run in its own workspace
(`~/.agentwire/council/<name>/workspace/`) whose `.agentwire.yml` sets
`parent: agentwire-council-<name>`, and none of them receive the standard
`soul` role — `inject_soul()` skips any session carrying a `council-*` role.

## The protocol

Per prompt, on disk under `~/.agentwire/council/<name>/prompts/NNNN/`:

```
prompt.md        # the fanned-out prompt
meta.json        # {id, created_at, roster}
replies/
  brain.take.md            # substantive take
  conscience.ack.md        # "researching, follow-up coming"
  conscience.followup-1.md # the substantive follow-up
  gut.pass.md              # nothing to add — synthesis omits it
```

Reply kind is encoded in the **filename**; `ls` is the protocol. Every soul
files exactly one initial reply (`take` / `ack` / `pass`) per prompt, so
collection can distinguish "still thinking" (no file) from "nothing to add"
(`.pass.md`) and return the moment the round is complete instead of always
waiting out a timeout. After an ack, a later `--take` lands as a numbered
follow-up and the CLI itself nudges the orchestrator's pane with a
`[COUNCIL FOLLOW-UP]` message — delivery doesn't depend on the soul
remembering to notify.

Sitting state (roster, session names, originating cwd, prompt counter) lives at
`~/.agentwire/council/<name>/sitting.json`. `council stop` clears it but keeps
the `prompts/` history.

## CLI

```bash
agentwire council start [--name N] [--roster brain,gut,...] [--posture P] [--model M] [--force]
agentwire council list                          # every sitting: name·cwd·age·live·prompts
agentwire council stop    [--name N] [--minutes|--no-minutes] [--synthesis S]
agentwire council status  [--name N]
agentwire council ask     [--name N] "Should we ship X?"   # or --file / stdin
agentwire council collect [--name N] [--prompt P] [--timeout 120] [--no-wait]
agentwire council reply   --name N --prompt P --take --text "..."   # souls run this
agentwire council reply   --name N --prompt P --ack
agentwire council reply   --name N --prompt P --pass
agentwire council minutes [--name N] [--prompt P|all] [--synthesis S]  # render record → HTML
```

`--name` is optional everywhere (resolved per the targeting rules above); the
fanned-out `[COUNCIL PROMPT #N]` message hands each soul the exact `reply`
command, `--name` already filled in. `reply` infers `--soul` by reverse-looking
its session up in `sitting.json` (never by splitting the name); `--take` text
comes from `--text`, `--file`, or stdin. All subcommands support `--json`.

## MCP tools (orchestrator-facing)

| Tool | Wraps |
|------|-------|
| `council_start(name, roster, model)` | `council start` |
| `council_list()` | `council list` |
| `council_stop(name, minutes, synthesis)` | `council stop` — renders minutes on the way out by default |
| `council_status(name)` | `council status` |
| `council_ask(prompt, name)` | `council ask` — returns the prompt id |
| `council_collect(prompt_id, timeout, name)` | `council collect` (subprocess timeout padded past the blocking window) |
| `council_minutes(name, prompt, synthesis)` | `council minutes` — returns the artifact path |

Every tool takes the optional `name` and echoes `[council: <name>]` so you can
see which sitting it hit.

`council reply` is deliberately CLI-only — souls invoke it via Bash.

## Minutes (the sitting's record)

`council minutes` renders a presentation-quality standalone HTML record of a
sitting — the original question, the orchestrator's synthesis, and the
verbatim per-soul replies (attributed, badged take/ack/pass/followup), one
section per prompt — following the handoff pattern
([#708](https://github.com/dotdevdotdev/agentwire-dev/issues/708)):

- **Deterministic render of disk state.** Everything verbatim already
  persists under `~/.agentwire/council/<name>/prompts/NNNN/`; the renderer is
  a pure function of it. The synthesis exists only in the orchestrator's
  context, so it's an *input*: `--synthesis <file-or-text>` (omitted →
  minutes without a synthesis section).
- **Output** is one fully self-contained file (inline CSS only — the portal's
  artifact CSP blocks external fetches; verbatim text is HTML-escaped),
  theme-aware light/dark, at
  `~/.agentwire/artifacts/council-<name>-minutes/index.html`. The command
  prints the path and best-effort announces it as a click-to-open portal
  notification — toast + Session HUD entry, never a focus-stealing window
  open (#817) — when the portal is up (`notified` in the `--json` payload).
- **`council stop` renders minutes automatically** when any prompt exists
  (`--no-minutes` to skip, `--synthesis` to include your synthesis), so
  closing a sitting leaves the record behind. Prompt history survives stop,
  so minutes can also be rendered for dismissed sittings any time — pass
  `--name` explicitly (a dismissed sitting is never auto-resolved).
- `--prompt <id|all>` scopes the render (default: every prompt on disk).
  Re-rendering overwrites the same `index.html` — one minutes artifact per
  sitting name.

## Extending the roster

Lens roles are ordinary role files, so discovery shadowing applies:

1. Drop `council-<newlens>.md` in `~/.agentwire/roles/` (or a project's
   `.agentwire/roles/`) — frontmatter `name` + `description`, body = the lens.
2. `agentwire council start --roster brain,critic,newlens`

Overriding a bundled lens's content works the same way — a user-level
`council-brain.md` shadows the bundled one.

## Troubleshooting

- **A soul never replies** — `council status` shows per-prompt `pending`
  souls; check the session is alive and the `[COUNCIL PROMPT #N]` message
  landed in its pane. `collect` returns `timed_out: true` with the pending
  list rather than blocking forever.
- **Stale sitting after a crash** — `council start --force` (same `--name`)
  tears down whatever is left and starts fresh. First run after upgrading from
  the pre-namespace singleton sweeps any zombie `council-*` / `agentwire-council`
  panes + orphaned global `sitting.json` automatically.
- **"multiple councils live" error** — pass `--name`; `council list` shows the
  candidates (the age column flags forgotten token-burning sittings).
- **Soul replies rejected** — a soul gets one initial reply per prompt;
  after that only `--take` follow-ups are accepted (a second `--ack`/`--pass`
  errors by design).
