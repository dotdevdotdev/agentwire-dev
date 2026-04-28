# Google Workspace CLI (`gws`)

> Reference guide for `@googleworkspace/cli` — a Google-built (but not officially supported) CLI for all Google Workspace APIs.

- **GitHub:** https://github.com/googleworkspace/cli
- **npm:** https://www.npmjs.com/package/@googleworkspace/cli
- **License:** Apache-2.0
- **Status:** Pre-v1.0, not officially supported by Google. Breaking changes expected.
- **Latest (2026-03-10):** v0.9.1

---

## Installation

```bash
npm install -g @googleworkspace/cli
```

Requires Node.js 18+. The npm package bundles pre-built native binaries (Rust, no toolchain needed).

**Alternatives:**
```bash
# Pre-built binary from GitHub Releases
# https://github.com/googleworkspace/cli/releases

# From source (requires Rust)
cargo install --git https://github.com/googleworkspace/cli --locked

# Nix
nix run github:googleworkspace/cli
```

---

## Authentication

### Option A: Automated setup (requires `gcloud`)

```bash
gws auth setup   # creates GCP project, enables APIs, OAuth client, logs you in
```

Subsequent logins:
```bash
gws auth login -s drive,gmail,calendar   # select only needed services
```

### Option B: Manual OAuth setup (no `gcloud`)

1. Go to [GCP Console](https://console.cloud.google.com) → APIs & Services → OAuth consent screen
2. Set app type to **External**, leave in **testing** mode
3. Add yourself under **Test users** (required — without this you get "Access blocked")
4. Enable the APIs you need (Drive, Gmail, Calendar, etc.)
5. Create credential → **Desktop app** type → download client JSON
6. Save to `~/.config/gws/client_secret.json`
7. Run `gws auth login -s drive,gmail,calendar`

**Important:** Testing-mode apps are limited to ~25 OAuth scopes. Always use `-s` to select only the services you need. The `recommended` preset (85+ scopes) will fail.

### Option C: Service account (headless/CI)

```bash
export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/path/to/service-account.json
gws drive files list
```

For Domain-Wide Delegation (act as a real user):
```bash
export GOOGLE_WORKSPACE_CLI_IMPERSONATED_USER=user@example.com
```

### Option D: Export credentials to headless machine

```bash
# On machine with browser (one-time)
gws auth export --unmasked > credentials.json

# On headless machine
export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/path/to/credentials.json
```

### Option E: Pre-obtained access token

```bash
export GOOGLE_WORKSPACE_CLI_TOKEN=$(gcloud auth print-access-token)
```

### Authentication precedence (highest wins)

| Priority | Source |
|----------|--------|
| 1 | `GOOGLE_WORKSPACE_CLI_TOKEN` env var |
| 2 | `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` env var |
| 3 | Encrypted credentials from `gws auth login` |
| 4 | Plaintext `~/.config/gws/credentials.json` |

### Credential storage

Credentials are AES-256-GCM encrypted. Key stored in OS keyring (Keychain on macOS, Credential Manager on Windows, Secret Service on Linux). Config directory: `~/.config/gws/` (override: `GOOGLE_WORKSPACE_CLI_CONFIG_DIR`).

---

## Command Structure

```bash
gws <service> <resource> [sub-resource] <method> [flags]
```

Commands are built dynamically from Google's Discovery Service (cached 24h). New API endpoints appear automatically.

**Discover available commands:**
```bash
gws drive --help               # browse all Drive resources
gws drive files --help         # browse methods on files resource
gws schema drive.files.list    # show params, types, defaults for a method
```

### Global flags

| Flag | Description |
|------|-------------|
| `--format <FORMAT>` | Output format: `json` (default), `table`, `yaml`, `csv` |
| `--dry-run` | Validate locally without calling the API |
| `--sanitize <TEMPLATE>` | Screen response through Model Armor |

### Method flags

| Flag | Description |
|------|-------------|
| `--params '{"key":"val"}'` | URL/query parameters |
| `--json '{"key":"val"}'` | Request body |
| `-o, --output <PATH>` | Save binary response to file |
| `--upload <PATH>` | Upload file content (multipart) |
| `--page-all` | Auto-paginate, NDJSON output (one JSON line per page) |
| `--page-limit <N>` | Max pages (default: 10) |
| `--page-delay <MS>` | Delay between pages in ms (default: 100) |

---

## Supported Services

| Service | Description |
|---------|-------------|
| `drive` | Files, folders, shared drives (Drive v3) |
| `gmail` | Send, read, manage email |
| `calendar` | Calendars and events |
| `sheets` | Read and write spreadsheets |
| `docs` | Read and write Google Docs |
| `slides` | Presentations |
| `tasks` | Task lists and tasks |
| `people` | Contacts and profiles |
| `chat` | Chat spaces and messages |
| `classroom` | Classes, rosters, coursework |
| `forms` | Google Forms |
| `keep` | Google Keep notes |
| `meet` | Google Meet conferences |
| `events` | Workspace events (push notifications) |
| `admin-reports` | Admin SDK audit logs and usage reports |
| `modelarmor` | Content safety filtering |
| `workflow` | Cross-service productivity workflows |

---

## Common Recipes

### Gmail

```bash
# List unread messages
gws gmail users messages list \
  --params '{"userId": "me", "q": "is:unread", "maxResults": 20}'

# Send email (helper skill)
gws gmail +send

# Reply with threading
gws gmail +reply
gws gmail +reply-all
gws gmail +forward

# Triage inbox
gws gmail +triage
```

### Drive

```bash
# List files
gws drive files list \
  --params '{"pageSize": 10, "fields": "files(id,name,mimeType,size)"}'

# List all files (paginated stream)
gws drive files list --params '{"pageSize": 100}' --page-all | jq -r '.files[].name'

# Upload file
gws drive files create \
  --json '{"name": "report.pdf"}' \
  --upload ./report.pdf

# Share folder
gws drive permissions create \
  --params '{"fileId": "FOLDER_ID"}' \
  --json '{"role": "writer", "type": "user", "emailAddress": "colleague@example.com"}'
```

### Calendar

```bash
# Create event
gws calendar events insert \
  --params '{"calendarId": "primary"}' \
  --json '{
    "summary": "Team Sync",
    "start": {"dateTime": "2026-03-15T10:00:00-07:00"},
    "end": {"dateTime": "2026-03-15T11:00:00-07:00"},
    "attendees": [{"email": "teammate@example.com"}]
  }'

# Find free/busy slots
gws calendar freebusy query \
  --json '{
    "timeMin": "2026-03-15T09:00:00Z",
    "timeMax": "2026-03-15T17:00:00Z",
    "items": [{"id": "user1@example.com"}, {"id": "user2@example.com"}]
  }'
```

### Sheets

```bash
# Read cells (note: use single quotes — bash expands ! in history)
gws sheets spreadsheets values get \
  --params '{"spreadsheetId": "SPREADSHEET_ID", "range": "Sheet1!A1:C10"}'

# Append rows
gws sheets spreadsheets values append \
  --params '{"spreadsheetId": "ID", "range": "Sheet1!A1", "valueInputOption": "USER_ENTERED"}' \
  --json '{"values": [["Name", "Score"], ["Alice", 95]]}'

# Create spreadsheet
gws sheets spreadsheets create --json '{"properties": {"title": "Q1 Budget"}}'
```

### Chat

```bash
# Send message (with dry-run preview)
gws chat spaces messages create \
  --params '{"parent": "spaces/SPACE_ID"}' \
  --json '{"text": "Deploy complete."}' \
  --dry-run
```

### Docs

```bash
# Write to doc (helper skill)
gws docs +write
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_WORKSPACE_CLI_TOKEN` | Pre-obtained OAuth2 access token |
| `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` | Path to credentials JSON (user or service account) |
| `GOOGLE_WORKSPACE_CLI_CLIENT_ID` | OAuth client ID |
| `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET` | OAuth client secret |
| `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` | Config directory override (default: `~/.config/gws`) |
| `GOOGLE_WORKSPACE_CLI_IMPERSONATED_USER` | Email to impersonate via Domain-Wide Delegation |
| `GOOGLE_WORKSPACE_CLI_SANITIZE_TEMPLATE` | Default Model Armor template ARN |
| `GOOGLE_WORKSPACE_CLI_SANITIZE_MODE` | `warn` (default) or `block` |
| `GOOGLE_WORKSPACE_PROJECT_ID` | GCP project ID override (fixes 403s when global ADC project lacks API) |

---

## MCP Integration

`gws` ships a built-in MCP server. Scope it with `-s` to stay within tool count limits (50–100 per client).

```bash
gws mcp -s drive,gmail,calendar
```

**Claude Desktop config (`~/.claude/claude_desktop_config.json`):**
```json
{
  "mcpServers": {
    "googleworkspace": {
      "command": "gws",
      "args": ["mcp", "-s", "drive,gmail,calendar,sheets"]
    }
  }
}
```

---

## AI Agent Skills

The repo ships 100+ SKILL.md files for agent-aware CLI usage.

```bash
# Install all skills
npx skills add https://github.com/googleworkspace/cli

# Install individual service skill
npx skills add https://github.com/googleworkspace/cli/tree/main/skills/gws-gmail
```

Skills index: https://github.com/googleworkspace/cli/blob/main/docs/skills.md

**Service skills** (one per API): `gws-drive`, `gws-gmail`, `gws-calendar`, `gws-sheets`, `gws-docs`, `gws-slides`, `gws-tasks`, `gws-people`, `gws-chat`, `gws-classroom`, `gws-forms`, `gws-keep`, `gws-meet`, `gws-events`, `gws-admin-reports`, `gws-modelarmor`, `gws-workflow`

**Persona bundles**: exec-assistant, project-manager, hr-coordinator, sales-ops, it-admin, content-creator

**Recipes** (40+ multi-step workflows): label-and-archive-emails, organize-drive-folder, create-expense-tracker, block-focus-time, find-free-time, bulk-download-folder, create-meet-space, weekly digest, and more.

---

## Gotchas

**Scope limits on unverified apps:** Testing-mode OAuth apps max out at ~25 scopes. Use `-s drive,gmail,calendar` to select only what you need. Never use the `recommended` preset.

**"Access blocked" error:** Add yourself as a test user in the GCP Console OAuth consent screen. Click Advanced → Continue to proceed past the unverified app warning.

**`redirect_uri_mismatch`:** OAuth client must be **Desktop app** type, not Web application.

**`accessNotConfigured` (403):** The API isn't enabled in your GCP project. `gws` includes an `enable_url` in the JSON error pointing to the GCP Console. Wait ~10 seconds after enabling before retrying.

**Shell escaping for Sheets ranges:** Bash expands `!` in `Sheet1!A1:C10`. Always use single quotes around `--params` values containing `!`.

**Service accounts and Calendar:** Service accounts become data owners of calendars they create. Use Domain-Wide Delegation + `GOOGLE_WORKSPACE_CLI_IMPERSONATED_USER` to act on behalf of real users.

**Discovery cache:** Commands are built from cached Discovery Documents (24h TTL). New API endpoints may not appear until cache clears.

**ADC project conflicts:** If your global `gcloud` project doesn't have an API enabled but your gws-specific project does, set `GOOGLE_WORKSPACE_PROJECT_ID` to override. Fixed properly in v0.8.1.

**Pre-v1.0 stability:** Pin versions in automation scripts and test after updates.
