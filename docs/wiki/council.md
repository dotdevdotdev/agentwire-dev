# The Council

> Multi-soul orchestrator: fan a prompt out to distinct lens sessions, collect
> their takes, synthesize with attribution. The bundled `soul` role is the
> blended default voice ([#212](https://github.com/dotdevdotdev/agentwire-dev/issues/212));
> the council unbundles it into its constituent lenses
> ([#213](https://github.com/dotdevdotdev/agentwire-dev/issues/213)).

## Mental model

A **sitting** is one `council start` → `council stop` span. It comprises:

- The **orchestrator** — the `agentwire-council` session (role
  `council-orchestrator`). You talk to it; it fans out, collects, and
  synthesizes.
- The **souls** — one `council-<lens>` session per roster lens, each loading
  the shared `council-member` protocol role plus its own `council-<lens>`
  lens role.

Default roster:

| Lens | Looks at |
|------|----------|
| `brain` | Research, predictions, stats, sense-checking claims |
| `conscience` | Ethics, audience reception, trust implications |
| `gut` | Instinct — one short visceral read |
| `critic` | The weakest load-bearing assumption in the premise |
| `historian` | What we tried before, what worked, what didn't |
| `devils-advocate` | The strongest opposing case, argued in good faith |

All council sessions run in a shared workspace
(`~/.agentwire/council/workspace/`) whose `.agentwire.yml` sets
`parent: agentwire-council`, and none of them receive the standard `soul`
role — `inject_soul()` skips any session carrying a `council-*` role.

## The protocol

Per prompt, on disk under `~/.agentwire/council/prompts/NNNN/`:

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

Sitting state (roster, session names, prompt counter) lives at
`~/.agentwire/council/sitting.json`. `council stop` clears it but keeps the
`prompts/` history.

## CLI

```bash
agentwire council start [--roster brain,gut,...] [--type T] [--model M] [--force]
agentwire council stop
agentwire council status
agentwire council ask "Should we ship X?"      # or --file / stdin
agentwire council collect [--prompt N] [--timeout 120] [--no-wait]
agentwire council reply --prompt N --take --text "..."   # souls run this
agentwire council reply --prompt N --ack
agentwire council reply --prompt N --pass
```

All subcommands support `--json`. `reply` infers `--soul` from the current
tmux session name (`council-gut` → `gut`); `--take` text comes from `--text`,
`--file`, or stdin.

## MCP tools (orchestrator-facing)

| Tool | Wraps |
|------|-------|
| `council_start(roster, model)` | `council start` |
| `council_stop()` | `council stop` |
| `council_status()` | `council status` |
| `council_ask(prompt)` | `council ask` — returns the prompt id |
| `council_collect(prompt_id, timeout)` | `council collect` (subprocess timeout padded past the blocking window) |

`council reply` is deliberately CLI-only — souls invoke it via Bash.

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
- **Stale sitting after a crash** — `council start --force` tears down
  whatever is left and starts fresh.
- **Soul replies rejected** — a soul gets one initial reply per prompt;
  after that only `--take` follow-ups are accepted (a second `--ack`/`--pass`
  errors by design).
