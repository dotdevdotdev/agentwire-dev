# Usage Telemetry: Local Counters and Opt-In Sharing (design draft)

Status: **draft for owner decision** (#1068 Phase 1 and Phase 2). No code ships with this document. Phase 0 (damage-control outcome rows) is done (#1074, #1075).

Goal, in the owner's words: know which features get used and how often, locally, with negligible speed cost. Later, let users opt in to sending anonymized data. Default off, explicit question, nothing leaves the machine before consent.

Assumptions are flagged **[A]**. Decisions for the owner are collected in [section 8](#8-owner-decisions).

## 0. What exists today (verified 2026-10-20)

| Thing | State |
|-------|-------|
| Audit log | `~/.agentwire/logs/damage-control/YYYY-MM-DD.jsonl`, one row per PreToolUse decision (`allowed`/`asked`/`blocked`), written by `hooks/damage-control/audit_logger.py` with a plain `open(..., "a")` and **no fsync** |
| Outcome rows | `hooks/outcome_logger.py` (PostToolUse, PostToolUseFailure, PermissionDenied), `kind: "outcome"`, same daily file, joined on `tool_use_id` by `safety/audit.py:join_outcomes` |
| Report | `agentwire safety report --days N [--json]`: tiers, rules, sessions, Outcomes, Friction by rule, Per day |
| Retention | None. Unbounded. Owner decision 2026-10-12: nothing is deleted; first step if size matters is gzip of old days. No copy outside `~/.agentwire/` |
| Size | 10 daily files, **17 MB total** since the 2026-10-11 wipe, 0.03 to 3.6 MB/day (about 1.7 MB/day average, not the 0.5 MB/day quoted in #1068; it grows with activity). Rows contain full command text |
| Hot-path cost already paid | Attribution may spawn `tmux display-message` (timeout 2 s) per hook run when `AGENTWIRE_SESSION_ID` is unset, plus a scan of `sessions/*/metadata.json` |
| Non-hook usage data | None. No counter of CLI commands, MCP tools, portal windows, or scheduler tasks |

Consequence: the audit log is a **damage-control decision journal with command text**. It is the wrong container for usage telemetry (content-bearing, per-tool-call volume, only covers hook-matched tools). Reuse its *patterns* (daily JSONL, best-effort, never changes a decision, `tool_use_id` join), not its file.

## 1. Questions the data should answer

These drive the schema; an event that answers none of them is not collected.

1. **Feature use:** which CLI commands, MCP tools, portal windows/panels, scheduler task types, and optional subsystems (voice, council, briefing, handoff, wiki, packs) are used, how often, on how many days?
2. **Dead weight:** which of the ~40 CLI modules and ~100 MCP tools are never touched (candidates to cut under the no-backcompat stance)?
3. **Sequences, coarse:** "X within 5 min after Y" for a short allowlist of pairs (e.g. `new` then `msg send`; `worktree` then `wait --children`). Pairs of enums only, never traces.
4. **Topology mix:** role x topology x rooting x posture of sessions (the three-axis model, #716), and how many sessions run per day.
5. **Damage-control friction:** per rule id, asked / approved / declined / blocked counts (already computable from audit rows; this is the shareable aggregate form).
6. **Failure shape:** exit-code class per command (ok / usage / error / usage_limit / auth_expired), doctor findings by check id.
7. **Environment:** agentwire version, OS family, Python minor, install method. For "can we drop X" decisions.

Explicit non-questions: what users typed, what agents did, which projects exist.

## 2. Event schema

Rule: **counters and enums only.** Never command text, arguments, paths, prompts, session names, branch names, hostnames, usernames, repo names, error messages, tool input/output. Free text cannot be allowlisted, so there is none.

### Where it lives (so "what we collect" is checkable)

One module, `agentwire/telemetry/schema.py`, is the whole contract. Everything is a closed enum; `record()` raises (in tests) or drops (in prod) any value not in the schema.

```python
# agentwire/telemetry/schema.py  (proposed)
SCHEMA_VERSION = 1
EVENTS: dict[str, EventSpec] = {
    "cli":      EventSpec(fields={"cmd": CLI_COMMANDS, "result": RESULTS}),
    "mcp":      EventSpec(fields={"tool": MCP_TOOLS, "result": RESULTS}),
    "ui":       EventSpec(fields={"surface": UI_SURFACES}),
    "session":  EventSpec(fields={"role": ROLES, "topology": TOPOLOGIES, "rooted": BOOL, "posture": POSTURES}),
    "dc":       EventSpec(fields={"rule": RULE_IDS, "label": DC_LABELS}),
    "task":     EventSpec(fields={"kind": TASK_KINDS, "result": RESULTS}),
    "doctor":   EventSpec(fields={"check": DOCTOR_CHECK_IDS, "grade": GRADES}),
    "pair":     EventSpec(fields={"first": PAIR_NAMES, "then": PAIR_NAMES}),
}
```

The enum sets are **derived from the registries that already exist** (argparse subparser names, the MCP tool list, rule ids, `ROLES`), so a new command is covered automatically and a free-form value cannot be added by accident. Anything not in the enum is bucketed as `other` (one counter, no text).

A test enumerates `EVENTS` and fails if any field type is `str` without an enum, or any enum value contains a path separator, whitespace, or `@`. That test is the checkable promise; the docs table below is generated from the same module (`agentwire telemetry schema` prints it).

### Schema table

| Event | Fields (all enums/ints) | Source hook point | Answers |
|-------|-------------------------|-------------------|---------|
| `cli` | `cmd` (top-level + subcommand id, e.g. `worktree.remove`), `result` | `__main__.main()` exit | Q1, Q2, Q6 |
| `mcp` | `tool`, `result` | MCP server dispatch wrapper | Q1, Q2 |
| `ui` | `surface` (window kind: monitor, terminal, artifact, collage, sidebar section, voice) | portal WebSocket events already handled server-side; **no JS analytics** | Q1 |
| `session` | `role`, `topology`, `rooted`, `posture` | `core.record_session_launch()` (already the one writer, #871) | Q4 |
| `dc` | `rule` (rule id), `label` (ran/approved/declined/blocked/classifier-denied/failed) | folded from existing audit rows by the aggregator, not re-emitted | Q5 |
| `task` | `kind` (shell/prompt/pre/post), `result` | scheduler dispatch | Q1, Q6 |
| `doctor` | `check` (id), `grade` | `doctor` run | Q6 |
| `pair` | `first`, `then` from a fixed list of about 10 named pairs | derived from `cli`/`mcp` order within a 5 min window, in the aggregator | Q3 |
| `env` (once per day) | `version`, `os` (darwin/linux/windows), `py` (minor), `install` (uv-tool/pip/dev) | daily rollover | Q7 |

Dimensions attached at **aggregation**, not per event: UTC **day** (no timestamps finer than a day leave the machine), schema version. No per-event timestamp is stored in the shareable form; the local store may keep hour buckets.

Cardinality guard: a field with more than 256 distinct values in the schema is a design smell (rule ids are about 300; send only the top-N by count above a floor, see section 5).

## 3. Local store

### Options

| Option | Write cost | Bounded | Survives crash | Notes |
|--------|-----------|---------|----------------|-------|
| Append-only JSONL per event | one `open("a")` + write per event, ~20 to 50 us | needs rotation | yes | Volume: every CLI call and MCP call. Fine for count, but the reader must re-aggregate |
| SQLite | connection open ~1 ms, WAL write; lock contention across concurrent hook processes | yes, fixed schema | yes | Multi-process writers from hooks and CLI: `SQLITE_BUSY` handling is exactly the sort of thing that must never delay a hook |
| **Per-process spool of increments, merged into daily counters** | one small `O_APPEND` write at process exit | yes, by construction | yes (spool is replayed) | Recommended |

### Recommendation: daily counter files fed by an O_APPEND spool

- **Write path (every process, including hooks):** at exit, one `os.write()` of a single line (`{"k":"cli","f":["worktree.remove","ok"]}` or a compact form) to `~/.agentwire/telemetry/spool/<day>.jsonl` opened `O_APPEND`. POSIX guarantees small `O_APPEND` writes (under `PIPE_BUF`, 4 KB; our lines are under 100 bytes) do not interleave between processes, so **no lock, no fsync, no extra process spawn, no imports beyond `os`/`json`**. Cost reasoned at 10 to 30 us; to be **measured** in Phase 1 against `agentwire --version` and the hook benchmark (acceptance: p99 hook latency delta under 0.5 ms).
- **No fsync, by decision.** Losing the last few events on power loss is acceptable for usage counts. This differs from nothing in the audit logger, which is also unsynced.
- **Aggregation (off the hot path):** the limits watchdog already ticks every 60 s. Add one stage that renames `spool/<day>.jsonl` to `.merging`, folds it into `counters/<day>.json` (`{"cli:worktree.remove:ok": 12, ...}`, a few KB), and deletes the spool. A crash mid-merge is safe: a leftover `.merging` is folded next tick.
- **Hot paths specifically:**
  - *PreToolUse damage-control hook:* **emits nothing.** The audit row it already writes is the data; the `dc` events are derived from audit rows by the aggregator. Zero added work on the hottest path.
  - *CLI startup:* one `atexit` write. No import of the telemetry package unless enabled-for-local (local is on by default, so the module must be import-light: `os`, `json`, a frozenset of allowed ids).
  - *MCP server:* in-memory `Counter` flushed every 60 s and on shutdown, not per call.
  - *Portal:* in-memory `Counter`, flushed on the same cadence.
  - *Kill switch:* `AGENTWIRE_TELEMETRY=0` / `telemetry.local: false` makes `record()` a no-op after one env/attr check.
- **Bounds:** counter files are tiny (low KB/day); cap spool at 1 MB/day (drop with a counted `dropped` event beyond that); keep `counters/` daily files for **400 days**, then fold into a `monthly.json`. Worst-case footprint about 2 to 3 MB. No deletion of anything else.
- **Wipe survival:** same stance as owner decision 2026-10-12: it lives under `~/.agentwire/telemetry/` and a wipe erases it. Counters are *derived, low-value* data and the loss is acceptable; stating it plainly rather than building a second copy. **[A]** The 2026-10-11 incident loss was audit history (the more valuable corpus); that decision stands.
- **Relationship to the audit log:** the audit log stays the system of record for damage-control decisions (content-bearing, local-only forever, never shared). Telemetry reads it for `dc` aggregation and never copies command text. Retention for the audit log remains the existing owner decision (unbounded, gzip first); this doc does not change it.

## 4. Local surfaces

- `agentwire stats [--days N] [--json]`: top commands/tools by count, never-used list (Q2), session mix, friction summary. Reads `counters/` only. `telemetry show` is the same data plus the schema version and store paths.
- `agentwire doctor`: one line, e.g. `[ok] telemetry: local counters on, 14 days, 31 KB, sharing off`; warns if spool backlog is stale (aggregator not running) or the store is unwritable.
- MCP: `telemetry_stats` (read-only, same JSON as `stats --json`). **No MCP tool to enable sharing or change consent**; consent is human-only, same rule as `safety packs enable` (tty gate + protected-path).
- Portal: optional later panel; out of scope here (CLI is SSOT, portal would call `agentwire stats --json`).

## 5. Opt-in sharing

### Config

In the protected `~/.agentwire/damagecontrol.yml`? No: that file is for safety knobs. Put it in `config.yaml` and **protect the key** from agent writes the same way the control-plane files are (an agent must not be able to flip consent). **[A]**

```yaml
telemetry:
  local: true          # local counters; default on
  share: false         # default OFF; only a human-run command sets true
  consent:             # written by `telemetry enable`, never by hand-edit being sufficient
    version: 1         # consent text version; bump re-asks
    at: 2026-10-20
  endpoint: https://t.example.invalid/v1/ingest   # one URL, in config so it's auditable
```

`share: true` without a matching `consent.version` is treated as off.

### Onboarding

`agentwire init` and the first `doctor` after upgrade ask **once**, as an explicit yes/no with default **No**, showing the preview (below) and the exact endpoint. Non-interactive runs (CI, scheduler, unattended, no tty) never ask and never enable. Declining is recorded so we do not nag; re-asking only happens when the consent text version changes materially, and that is a decision, not an automatism (see section 8).

### Commands (human-only mutations)

| Command | Behavior |
|---------|----------|
| `agentwire telemetry status` | local on/off, share on/off, install id age, last send, next send, queue size |
| `agentwire telemetry show` | the local summary (same as `stats`) |
| `agentwire telemetry preview` | **the exact JSON payload that would be sent now**, byte-for-byte, plus its size. Same code path as send |
| `agentwire telemetry export [--out f]` | write that payload to a file; no network |
| `agentwire telemetry enable` / `disable` | tty-gated; `enable` prints preview + endpoint + consent text first |
| `agentwire telemetry delete` | deletes local counters, rotates the install id, and (if an endpoint supports it) sends a deletion request keyed by the old id |

### What is sent

- **Rotating random install id:** `uuid4`, generated at enable time, **rotated every 90 days and on `delete`**. No derivation from hardware, hostname, username, or the Claude conversation ids. Rotation bounds longitudinal linkage; the cost is that retention/cohort analysis is limited to about 90 days. **[A]** acceptable.
- **Aggregated before send:** weekly, only completed UTC days, only counts. Events with count under k=5 on the machine are folded into `other` (single-user rare events can be identifying, e.g. a rare rule id). Send top-N (about 200) keys per event kind.
- **Never sent:** timestamps finer than a day, IP stored server-side (endpoint must drop it, see below), anything from the audit log other than rule-id label counts, any string not in `schema.py`.
- **Transport:** HTTPS POST of the preview payload, stdlib `urllib`, 5 s timeout, fire-and-forget from the watchdog (not any hot path); failure retries next week; queue capped at 4 weeks. Honors `DO_NOT_TRACK=1` and `AGENTWIRE_TELEMETRY=0` as hard off, regardless of config.
- **Local before remote:** nothing leaves until `share: true` + matching consent version. The send function reads the same gate as `enable`, and a test asserts no socket is opened when it is false.

### Endpoint options

| Option | Cost | Abuse/ops | Privacy | Verdict |
|--------|------|-----------|---------|---------|
| **Owned: Cloudflare Worker + D1/KV, or Supabase edge fn** | free tier to a few $/mo at this scale | Needs rate limit per IP (edge), payload size cap (8 KB), schema validation against the same `schema.py` (reject unknown keys), no IP persistence | Full control; can state "we never store IPs" truthfully | **Recommended** |
| Vendor: PostHog (cloud or self-host), Plausible-style | free tier then per-event pricing; self-host = ops | Vendor handles rate limits | Vendor SDKs tend to add identifiers/geo-IP by default; must be configured off; third-party processor in the legal story | Viable if the owner wants dashboards for free; use the HTTP capture API, not the SDK |
| GitHub-issue-based "send us your export" | $0 | n/a | Fully manual, fully transparent | A fine Phase 2.0 stopgap: `export` + paste |

Abuse protection for any owned endpoint: per-IP and global rate limit, 8 KB body cap, strict schema validation, reject unknown event/field/enum values, treat the data as **untrusted** (anyone can POST; aggregates are indicative, not authoritative; never auto-act on them). **[A]** The owner has a Cloudflare account already (not verified here).

### Legal (questions for counsel, not conclusions)

1. Is a random rotating install id, with IP not stored, "personal data" under GDPR / "information stored on terminal equipment" under ePrivacy Art. 5(3)? Does opt-in consent plus our preview satisfy both, or is a separate banner-style disclosure needed?
2. What is the lawful basis and controller identity if the project is a one-person open source effort versus a company? Is a privacy notice URL required in the consent text, and where does it live?
3. CCPA/CPRA: does a random install id count as a "unique identifier" making this "personal information", and does opt-in-only collection with deletion support avoid "sale/share" obligations?
4. Right to erasure with a rotating id: how do we honor deletion if we cannot link a request to rows? (The draft answer: aggregates are not linkable by design and `delete` removes the queue and rotates the id; counsel to confirm that is enough.)
5. Cross-border transfer if the endpoint is outside the EU: any additional notice or mechanism needed for aggregate counters?
6. Do enterprise or employer-managed machines need a policy-level off switch (env var / managed config) to be defensible? (Already designed: `AGENTWIRE_TELEMETRY=0`.)

## 6. Norms for dev-tool telemetry

Summarized from memory of public docs; **the URLs below should be re-checked before the owner quotes any of them** (not re-fetched in this draft; budget).

| Tool | Default | Notes | Source |
|------|---------|-------|--------|
| Go toolchain | **Opt-in**, local-only by default | After the 2023 opt-out proposal drew heavy pushback, Go shipped `go telemetry local|on|off` with local collection and uploads only if enabled. The closest precedent to this design (local counters, explicit consent, published counter list) | go.dev/doc/telemetry |
| Angular CLI | **Opt-in**, interactive prompt on first run | Asks once; decision stored | angular.dev/cli/analytics |
| VS Code | Opt-out (`telemetry.telemetryLevel`) | Large, established; documents categories | code.visualstudio.com/docs/configure/telemetry |
| Homebrew | Opt-out (`HOMEBREW_NO_ANALYTICS`) | Notice shown on first run, anonymous | docs.brew.sh/Analytics |
| Next.js | Opt-out (`next telemetry disable`) | Anonymous, notice at install | nextjs.org/telemetry |
| .NET CLI | Opt-out (`DOTNET_CLI_TELEMETRY_OPTOUT`) | First-run notice | learn.microsoft.com/dotnet/core/tools/telemetry |
| Terraform | Opt-out (`CHECKPOINT_DISABLE`) | Version check only | developer.hashicorp.com/terraform |
| Claude Code | Telemetry/error reporting controllable via env (`DISABLE_TELEMETRY` etc.) | Relevant: agentwire users already run a tool with its own telemetry knobs | docs.claude.com (Claude Code settings) |
| Console Do Not Track | `DO_NOT_TRACK=1` convention | Cross-tool env signal many CLIs honor | consoledonottrack.com |

Reading: big incumbents mostly opt out with a notice; the newest, most scrutinized precedent (Go) and Angular chose opt-in, and the community reaction to opt-out in CLIs has been consistently negative. For a small project whose users are developers running agents with shell access, opt-in is also the only choice consistent with the owner's "nothing leaves before consent". Expect low opt-in rates (typically single-digit to low double-digit %); the data is directional, which is why the schema favors coarse counts.

## 7. Phased plan

| Phase | Scope | Size | Gate |
|-------|-------|------|------|
| **1a** | `telemetry/schema.py` + `record()` + spool write + aggregator stage + schema test (+ mutation: add a str field, test must fail) | M (about 1 day) | Benchmark: CLI startup and hook p99 delta |
| **1b** | Instrument CLI exit, MCP dispatch, session launch, scheduler, doctor; `dc` derivation from audit rows | M | Counters appear; coverage test: every registered subcommand maps to an enum value |
| **1c** | `agentwire stats`, `telemetry show`, doctor line, `telemetry_stats` MCP tool, wiki page | S | Owner reads own 2 weeks of data and decides whether it answers section 1 |
| **2a** | Config block, consent record, `status/preview/export/enable/disable/delete`, protected key, DO_NOT_TRACK; **no network code** | M | Preview payload reviewed by owner |
| **2b** | Endpoint (owned worker), sender in watchdog, k-anonymity floor, install-id rotation, rate-limit and validation server-side | M | Legal questions answered; owner approves consent text |
| **2c** | Onboarding prompt in `init`/`doctor`, announcement via `announcements.json` | S | After 2b has run on the owner's machine for a week |

1a to 1c are useful without Phase 2 and ship independently. Phase 2 does not start until the owner reads real Phase 1 output.

## 8. Owner decisions

1. **Opt-in vs opt-out for sharing.** Draft assumes strict opt-in, default off (matches the brief). Confirm; opt-out is not designed here.
2. **Local counters on by default?** Draft: yes (nothing leaves the machine), with a visible kill switch and a doctor line. Alternative: also ask for local. Confirm.
3. **Store format:** O_APPEND spool plus daily counter files (recommended) vs SQLite vs plain JSONL. Accept the no-fsync, may-lose-last-seconds tradeoff?
4. **Wipe survival:** confirm counters are disposable and live only under `~/.agentwire/telemetry/` (consistent with the 2026-10-12 decision).
5. **Endpoint:** owned Cloudflare Worker (recommended), vendor (PostHog HTTP API), or manual export-only first?
6. **Install id rotation period** (draft 90 days) and **k floor** (draft 5): privacy vs. usefulness.
7. **Where consent lives and who can write it:** protected key outside agent reach (draft) and human-only `enable` via tty gate.
8. **Re-ask policy** when the schema or consent text changes: automatic re-prompt vs only on material change.
9. **Legal:** who answers the section 5 questions, and is a privacy-notice page needed before 2b?
10. **Audit log:** any change to retention now that telemetry exists? Draft: none, it stays local-only forever and is never uploaded.
11. **Sequence instrumentation (Q3):** ship the `pair` event at all? It is the most identifying-adjacent and the least certain value; draft keeps it behind a fixed allowlist of about 10 named pairs and can be cut.
12. **Portal UI events (`ui`):** server-side only (draft) vs any client-side beacons (not recommended).

## Assumptions to verify

- Audit volume figure (17 MB in 10 days) is from this machine only.
- Hot-path costs in section 3 are reasoned, not measured; Phase 1a includes the measurement and a stop condition.
- Norms table is from memory; confirm each source before external use.
- The `config.yaml` protection mechanism for the consent key needs a look at how `PROTECTED_COMMAND_PATTERNS` handles `config.yaml` today.
