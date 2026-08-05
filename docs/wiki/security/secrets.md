# Secrets & API Keys

> Living document. Update this, don't create new versions.

**Every user secret lives in one file: `~/.agentwire/.env`.** One key per
line, classic dotenv format. Keys never go in `config.yaml`, shell profiles,
or `.agentwire.yml` — config holds env var *names* where needed, never
values.

```bash
# ~/.agentwire/.env
RESEND_API_KEY=re_...
QUO_API_KEY=...
OPENAI_API_KEY=sk-...
```

```bash
chmod 600 ~/.agentwire/.env
```

**This is checked, not just documented (#887).** It used to be convention
only — and convention lost: the file was found at 0644, world-readable,
holding every key. See [File permissions](#file-permissions) below.

## What loads it

`agentwire/__main__.py` calls `load_dotenv(~/.agentwire/.env)` on **every**
entry point — CLI commands, the portal, the MCP server, the scheduler. Any
code reading `os.environ` sees the keys; so does any feature added later.
That universality is why this is the one blessed spot.

Two consequences:

- **Long-running processes read it at startup.** After editing the file,
  restart what needs the new key: `agentwire portal restart`.
- The file is **dotenv format, not shell**. Values may legally contain `&`,
  spaces, or quotes unescaped, so **never `source` it** — an unquoted `&`
  backgrounds half a line and silently corrupts your shell state. To pull a
  single value out in a script:

  ```bash
  grep '^RESEND_API_KEY=' ~/.agentwire/.env | cut -d= -f2-
  ```

## Which vars each feature reads

| Feature | Env var(s) | Notes |
|---|---|---|
| Email channel (Resend) | `RESEND_API_KEY` | [Channels](../communication/channels.md) |
| Quo / OpenPhone SMS channel | `QUO_API_KEY` (or legacy `OPENPHONE_API_KEY`) | [Channels](../communication/channels.md) |
| Cloud STT | var **named by** `stt.cloud.api_key_env` — `OPENAI_API_KEY` by default; `GROQ_API_KEY`, `MISTRAL_API_KEY`, … per provider | [Cloud STT](../voice/stt-cloud.md) |
| PyPI publish (maintainers) | `PYPI_TOKEN` | release workflow only |

`agentwire doctor` reports, for each configured feature, whether its
expected var is present — names only, never values.

## The `api_key_env` pattern (for new integrations)

Cloud STT (#280) set the shape every new integration should copy: config
names the env var, the env var holds the key.

```yaml
stt:
  cloud:
    api_key_env: "OPENAI_API_KEY"   # the NAME — the key itself never lives in config
```

Why indirection instead of a hardcoded var name: multi-provider features
(one OpenAI-compatible endpoint among many, multiple pi providers) need the
user to pick which key applies without agentwire knowing every provider in
advance. Single-provider features (Resend, Quo) just hardcode their var
name — same convention, no indirection needed.

What this buys, in either form:

- `config.yaml` stays shareable/committable without a redaction pass.
- The portal's config editor never round-trips a secret to the browser.
- One file to `chmod 600`, back up, or rotate.

## File permissions

Four paths under `~/.agentwire/` must never be readable beyond their owner:

| Path | Mode | What leaks if it isn't |
|---|---|---|
| `~/.agentwire/` | `0700` | the filenames of everything below it |
| `~/.agentwire/.env` | `0600` | every API key |
| `~/.agentwire/portal.token` | `0600` | the portal auth token — full access to every session |
| `~/.agentwire/machines.json` | `0600` | remote hosts, users and paths |

Two mechanisms keep them there:

- **Enforced on write.** Every owner-only file agentwire writes goes through
  `core.write_owner_only`: the mode is set on the file descriptor *before any
  bytes land*, and the file is renamed into place, so there is no window where
  the content is world-readable — not even on first creation under a
  permissive umask. A rewrite therefore also *heals* a file that had already
  drifted wide. This covers `portal.token`, the `role-prompts/` store and
  `machines.json`; before #887 the registry was minted with a bare
  `write_text` and inherited the umask, which is how a 0644 registry ended up
  on a live machine.
- **Checked by `agentwire doctor`.** `.env` is the exception to the above —
  nothing in agentwire writes it (it's hand-authored, only ever read via
  `load_dotenv`), so the check is the only thing standing between it and a
  slow drift to 0644. Doctor reports every path that is group- or
  world-readable, naming the exact `chmod` to run; `agentwire doctor --yes`
  tightens them. Healing is opt-in rather than automatic — tightening a file
  is safe in a way loosening never is, but these are your files on your
  machine. A path that is *tighter* than required (`0400`, say) is never
  reported and never "fixed" back open.

## Security posture

- **Damage-control gives `.env` zero access for agents** — agent sessions
  can't read, edit, or even mention the file in shell commands
  ([damage control](../internals/damage-control.md)). Keys flow to features
  through the process environment only.
- **`--env` caveat:** secrets passed via `agentwire new --env KEY=VAL` (or
  `recreate`/`fork --env`) are injected with `tmux set-environment` at session
  creation. That keeps them out of `ps auxwww` and shell history, but anything
  with tmux access on the box can run `tmux show-environment -t <session>`.
  Acceptable on a single-user box; know the trade-off.
- Server-side only: no key is ever sent to the browser or echoed by a
  portal endpoint.
