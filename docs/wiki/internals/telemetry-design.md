# Usage Telemetry: Local Counters and Opt-In Sharing (design, revised)

Status: **owner decisions applied 2026-10-20** (#1068 Phase 1 and 2). No code ships with this document. Phase 0 (damage-control outcome rows) is done (#1074, #1075). **[A]** marks an assumption.

Goal: know which features get used, how often and how well, locally, with negligible speed cost. Then let users opt in to anonymous sharing. Nothing leaves the machine without an explicit yes.

## 0. What exists today (verified 2026-10-20)

| Thing | State |
|-------|-------|
| Audit log | `~/.agentwire/logs/damage-control/YYYY-MM-DD.jsonl`; one row per PreToolUse decision (`allowed`/`asked`/`blocked`) from `hooks/damage-control/audit_logger.py`: plain `open(..., "a")`, **no fsync**. Rows contain full command text |
| Outcome rows | `hooks/outcome_logger.py`; `kind: "outcome"`, joined on `tool_use_id` by `safety/audit.py:join_outcomes` into labels (ran / approved / declined-or-abandoned / rule-blocked / classifier-denied / failed). Payload carries `duration_ms` and `prompt_id` |
| Report | `agentwire safety report` (tiers, rules, Outcomes, Friction by rule, Per day) |
| Retention | None; owner decision 2026-10-12: keep everything, gzip before ever deleting, no copy outside `~/.agentwire/` |
| Size | 17 MB in 10 days (about 1.7 MB/day; #1068 said 0.5). Machine-specific |
| Hot-path cost already paid | attribution may spawn `tmux display-message` (2 s timeout) per hook run |
| Non-hook usage data | none: no CLI, MCP, portal, scheduler counters |

The audit log is a content-bearing decision journal and stays local forever. Telemetry reuses its *patterns* and *reads* it for aggregates; it never copies command text out of it.

## 1. Questions the data answers

1. **Adoption:** which CLI commands, MCP tools, roles, topologies, portal windows, and subsystems are used, how often, on how many days? Which are never touched (cut candidates)?
2. **Mix:** role x topology x rooting x posture of sessions.
3. **Friction:** per damage-control rule id: tier, outcome (approved / denied / ran), and whether a block was followed by a rephrase.
4. **Speed:** hook latency and session startup time, as distributions.
5. **Failure shape:** failure class per command; doctor findings by check id.
6. **Environment:** agentwire version, OS family, Python minor, install method.

Sequence ("X after Y") events are **cut** (owner decision); adoption counts cover the need.

## 2. Event schema

Rule: **enums, counters and bucketed numbers only.** Never recorded or shared: command text, arguments, paths, prompts, session/branch/repo names, hostnames, usernames, error message text, tool input/output, **or hashes of any of those** (a hash of a low-entropy string is reversible by dictionary, so a hash is not anonymization). Rule ids and doctor check ids are fixed public vocabulary and **may** be recorded and shared.

### Contract location

`agentwire/telemetry/schema.py` is the whole contract. Every field is a closed enum, an int count, or a histogram bucket index. `record()` drops (prod) or raises (tests) anything outside it. Enum sets derive from existing registries (argparse subcommand names, the MCP tool list, rule ids, `ROLES`), so new commands are covered automatically and unknown values land in `other`, not as text. A test fails if a field is `str` without an enum, or an enum value has a path separator, whitespace or `@`. `agentwire telemetry schema` prints the table below from the same module, which makes "what we collect" checkable.

### Events

| Event | Fields | Emitted from | Q |
|-------|--------|--------------|---|
| `cli` | `cmd` (e.g. `worktree.remove`), `result` (ok / usage / error / usage_limit / auth_expired), `dur` bucket | `main()` exit | 1,4,5 |
| `mcp` | `tool`, `result`, `dur` bucket | MCP dispatch wrapper | 1,4,5 |
| `ui` | `surface` (window kind), server-side only, no client beacons | portal WebSocket handlers | 1 |
| `session` | `role`, `topology`, `rooted`, `posture`, `startup` bucket | `core.record_session_launch()` | 2,4 |
| `dc` | `rule`, `tier`, `outcome` (approved / denied / ran), `rephrased` (bool) | **derived by the aggregator from audit rows**, not emitted | 3 |
| `hook` | `which` (permission / outcome / idle), `dur` bucket | aggregator, from `duration_ms` in outcome rows; the hook itself emits nothing | 4 |
| `fail` | `where` (cmd/tool/check id), `class` (enum: timeout, not_found, permission, usage_limit, auth_expired, conflict, other) | as above | 5 |
| `doctor` | `check` id, `grade` | `doctor` run | 5 |
| `env` (daily) | `version`, `os`, `py`, `install` | day rollover | 6 |

**Histogram buckets:** log2 milliseconds, fixed edges (`<1, <2, <4 ... <8192, >=8192`), stored as a counter per bucket. Percentiles are computed from buckets; no raw timings are kept.

**`rephrased`:** set when, within the same session and 5 minutes of a block, the next call of the same tool class is allowed. The comparison happens locally on audit rows; only the boolean leaves. **[A]** the audit log has no rephrase linkage today, so this is new aggregator logic (the #1068 comment list mentions it as wanted); `prompt_id` is available to scope a turn.

Granularity: counts per **UTC day** and schema version. No finer timestamp is shareable.

## 3. Local store

| Option | Hot-path cost | Bounded | Concurrency |
|--------|---------------|---------|-------------|
| Per-event JSONL | open + write per event | needs rotation | fine, but reader re-aggregates |
| SQLite | ~1 ms connect, WAL; `SQLITE_BUSY` risk from concurrent hook processes | yes | the wrong thing to risk on a hook |
| **Spool + daily counters** (recommended) | one `O_APPEND` write at process exit | yes | lock-free |

### Recommendation

- **Write path:** at exit, one `os.write()` of a line under 100 bytes to `~/.agentwire/telemetry/spool/<day>.jsonl` opened `O_APPEND`. Writes under `PIPE_BUF` (4 KB) don't interleave between processes: no lock, **no fsync**, no extra process spawn, imports limited to `os`/`json`. Cost reasoned at 10 to 30 us; **measured in Phase 1a** with acceptance "p99 hook and `agentwire --version` delta under 0.5 ms". Losing the last seconds on power loss is accepted.
- **Aggregation:** one new stage on the limits watchdog (60 s tick) renames the spool to `.merging`, folds it plus audit-derived events into `counters/<day>.json` (counter key to int, a few KB), deletes the spool. A leftover `.merging` is folded next tick.
- **Hot paths:** the damage-control hook adds **zero** work (its audit row is the data). CLI: one `atexit` write. MCP server and portal: in-memory `Counter` flushed every 60 s and on shutdown. Kill switch: `AGENTWIRE_TELEMETRY=0` or `telemetry.local: false` makes `record()` return after one check.
- **Bounds:** spool capped at 1 MB/day (excess counted as `dropped`); daily counters kept 400 days then folded into `monthly.json`. Footprint about 2 to 3 MB worst case.
- **Wipe:** counters are disposable and live only under `~/.agentwire/telemetry/`, consistent with the 2026-10-12 no-second-copy decision. A wipe erases them; accepted.
- **Audit relationship:** audit log unchanged (retention policy as decided 2026-10-12, never uploaded).

Prior art for the same shape: Go keeps only counter names and totals, no event log, per-week files, local by default ([Go telemetry design](https://research.swtch.com/telemetry-design.pdf), read).

## 4. Local surfaces

- `agentwire stats [--days N] [--json]` / `telemetry show`: top commands/tools, never-used list, session mix, friction, latency percentiles. Reads `counters/` only.
- `agentwire doctor` line: `telemetry: local on, 14 days, 31 KB, sharing off`; warns on stale spool (aggregator not running) or unwritable store.
- MCP `telemetry_stats` (read-only). **No MCP tool changes consent**; consent is human-only (tty gate), same rule as `safety packs enable`.

## 5. Opt-in sharing

### Decided

- **Opt-in, default off.** One onboarding question (in `agentwire init` and the first interactive `doctor` after upgrade) **suggests** opting in: it explains what the data is for and shows the exact preview payload and endpoint. **No answer, Enter, non-interactive, or skipped = off.** The declined/skipped state is recorded so it is not re-asked. Nothing leaves before an explicit yes.
- **Local counters on by default**, kill switch and doctor line.

### Config

```yaml
telemetry:
  local: true       # default on; nothing leaves the machine
  share: false      # default OFF; only a human-run `telemetry enable` sets true
  consent:          # written by `telemetry enable` / `decline`
    state: unasked  # unasked | accepted | declined
    version: 1      # consent text version
    at: null
  endpoint: https://t.example.invalid/v1/ingest   # one URL, auditable
```

`share: true` without `consent.state: accepted` and a matching version is off. The key is protected from agent writes like the control-plane files **[A]** (mechanism for `config.yaml` to be checked in Phase 2a). `DO_NOT_TRACK=1` and `AGENTWIRE_TELEMETRY=0` are hard off regardless of config.

### Commands (mutations human-only)

`telemetry status` (states, last/next send, queue) / `show` / `preview` (the exact payload, byte-for-byte, same code path as send) / `export [--out f]` (no network) / `enable` (prints preview, endpoint, consent text) / `disable` / `decline` / `delete` (wipes counters and queue).

### Identity: none persistent

- **No install id by default.** Aggregation is per **UTC day + version**.
- To compute the k floor the server must count distinct contributors without identifying them. Proposal: a **fresh random batch token (uuid4) generated for each weekly send, held only until that send succeeds, never reused**. A server can count distinct tokens per bucket but cannot link two weeks to one install. Weekly cadence follows Go (weekly counters reduce noise and make clear no fine-grained trace exists). **[A]** Alternative if even a per-send token is unwanted: client-side random sampling as Go does, with no token; costs k accuracy.
- **k floor:** the server publishes or retains a bucket only when at least **k = 10 distinct batch tokens** contributed to it **[A]** value for the owner (section 8). Client-side, any counter under 3 on the machine for a rare rule id is folded into `other` so one user's idiosyncratic behavior does not form a lone bucket.
- **Nothing that identifies a person or device:** no hardware/hostname/username derivation, no Claude conversation ids, no hashes, no IP storage (the endpoint must discard it).
- **Why this shape (EU):** the EDPB's reading is that storing or accessing information on a user's terminal equipment needs consent under ePrivacy Art. 5(3) with an affirmative action, before any access or storage, withdrawable as easily as given ([EDPB letter OUT2023-0098, annex, read](https://www.dirittobancario.it/wp-content/uploads/2024/01/EDPB-letter-13-dicembre-2023-OUT2023-0098.pdf)). The design aims to avoid being a consent-requiring or personal-data case at all by sending only aggregates with no persistent identifier, while still collecting explicit opt-in before anything is sent. The letter does not itself say a persistent identifier is personal data; that comes from general GDPR definitions and is exactly a question for counsel.

### Transport (automatic after opt-in)

After an explicit yes, collection is **automatic**: a weekly background send from the limits watchdog (never a hot path), completed UTC days only, HTTPS POST of exactly the previewed JSON via stdlib `urllib`, 5 s timeout. Failure backs off (next tick, then exponential up to the next week) and **never blocks or slows a command**; queue capped at 4 weeks. A kill-switch response from the server (below) makes the client stop sending until the next release. `export` remains only as the no-network inspection path. A test asserts no socket opens while `share` is false.

### Endpoint: decided, Cloudflare Worker, isolated

**Decision (2026-10-20):** a Cloudflare Worker with D1, **fully isolated**: its own Cloudflare account or project scope (at minimum its own zone, Worker and D1), no shared secrets, no connection to the website's Supabase or any other store.

Reasons: (1) a public unauthenticated write endpoint is an abuse target, so its blast radius must be zero; (2) near-zero idle cost; (3) rate limiting runs at the edge before our code; (4) the Worker is open source, so the no-IP-storage claim is checkable.

| Option | Verdict |
|--------|---------|
| **Cloudflare Worker + D1, isolated** | **Chosen** |
| PostHog | Rejected: SDK identity model fights the no-persistent-id design; IP-discard and EU hosting unverified |
| AWS Lambda + API Gateway | Rejected: more ops, no benefit here |
| Supabase as ingest | Rejected unless it is its own project: shared quota and abuse blast radius with whatever else lives there. A **separate** Supabase project is acceptable later as an optional analysis store fed aggregates |

Inbound data is **untrusted**: anyone can POST. Aggregates are indicative; nothing auto-acts on them.

#### Worker spec

- `POST /v1/ingest`, `Content-Type: application/json`; anything else 404/405.
- **Body cap 8 KB** (enforced in the Worker; Cloudflare's own plan limit is 100 MB, so this is ours to enforce).
- **Strict validation against `schema.py`** (shipped as a versioned JSON export of the schema): reject unknown keys, unknown enum values, wrong types, out-of-range counts. Schema is versioned in the payload; unsupported versions get a clear 4xx.
- **Rate limits:** per-IP via a Cloudflare rate-limiting rule (before the Worker runs) plus a global cap in the Worker. Counting by IP is the only characteristic on Free and Pro plans.
- **No IP stored or logged by our code.** Cloudflare itself sees IPs at the edge; what its logs retain is a question for counsel and the dashboard settings (section 5 legal list).
- **Kill switch:** the response body can carry `{"stop": true, "until_version": "..."}`; clients stop sending. Lets a bad schema or bug be shut off without a release.
- **Storage: one row (or one R2 object) per batch, never per counter.** A batch is one JSON blob plus `received_day`, `schema_version`, `agentwire_version`. A **daily scheduled job** aggregates batches into the queried/published tables and applies the k floor, then drops raw batches past a short retention **[A]** (7 days).
- Optional later: forward aggregates to a separate analysis store.

**Capacity:** 100k opted-in installs sending weekly is about **14k batches/day** (100,000 / 7). That is one row write each: well under the D1 free cap of 100k row writes/day and the Worker free cap of 100k requests/day. Per-counter rows (about 200 per batch) would be about 2.9M writes/day and break the free tier, which is why batches are one row.

#### Verify before building 2b

Official pages read 2026-10-20 (figures as the pages state them; re-check at build time because limits change):

| Item | Free | Paid | Source |
|------|------|------|--------|
| Workers requests | 100,000/day; 10 ms CPU | $5/mo min, 10M requests/mo included, then $0.30/M | [limits](https://developers.cloudflare.com/workers/platform/limits/), [pricing](https://developers.cloudflare.com/workers/platform/pricing/) |
| D1 | 100,000 rows written/day, 5M read/day, 5 GB | 50M written/mo included then $1/M; 5 GB then $0.75/GB-mo | [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/) |
| R2 | 10 GB-mo, 1M Class A, 10M Class B per month, free egress | $0.015/GB-mo, $4.50/M Class A | [R2 pricing](https://developers.cloudflare.com/r2/pricing/) |
| Analytics Engine | 100k data points/day, 10k queries/day | 10M/mo then $0.25/M; billing currently paused | [pricing](https://developers.cloudflare.com/analytics/analytics-engine/pricing/) |
| Rate-limiting rules | 1 rule, IP only, 10 s period | Pro 2 rules; Business 5 | [WAF rate limiting](https://developers.cloudflare.com/waf/rate-limiting-rules/) |

Still to confirm before 2b: (a) rate-limiting rules attach to a **zone**, so the isolated project needs a zone (a custom domain) on the plan we choose, and the Free plan's single 10 s-period rule may be too coarse for a weekly-per-install pattern, so a Worker-side limiter is the fallback; (b) what Cloudflare retains in edge/Worker logs about IPs and how to minimize it; (c) D1 behavior on bursty writes if installs sync on the same weekday (client jitter on the send time mitigates).

### Legal: questions for counsel, not conclusions

1. Is an aggregate-only payload with a per-send random token and no stored IP outside "personal data" (GDPR) and outside Art. 5(3) ePrivacy? If Art. 5(3) still applies, does our onboarding yes + preview satisfy "affirmative action, before access or storage, withdrawable"?
2. Does the *local* counter store count as "storage of information in terminal equipment" needing consent, given it never leaves the machine and is read only by the user? (Go treats local collection as acceptable by default.)
3. Controller identity and lawful basis for a one-person open-source project versus a company; is a privacy notice URL required in the consent text?
4. CCPA/CPRA: does the per-send token count as a "unique identifier"? Does opt-in-only plus deletion avoid "sale/share" duties?
5. Erasure when nothing links rows to a person: is "aggregates not linkable by design" sufficient?
6. Cross-border transfer of aggregate counters to a non-EU endpoint: any extra notice?
7. Do employer-managed machines need a policy-level off switch beyond the env var?

## 6. Norms for dev-tool telemetry

Every row below was read at its URL on 2026-10-20.

| Tool | Default | What the source says | Source |
|------|---------|----------------------|--------|
| Go toolchain | **Opt-in** (shipped in 1.23); default mode `local` collects, does not upload | Original Feb 2023 proposal was opt-out; heavy pushback ("No telemetry in my toolchains", "affirmative consent") and Cox revised to opt-in. Design: counters only, per-week files, random weekly sampling, no event log | [design](https://research.swtch.com/telemetry-design.pdf), [DevClass](https://www.devclass.com/development/2024/08/14/go-123-released-with-telemetry-uploaded-to-google-but-opt-in-after-developer-feedback/1622400), [The Register](https://www.theregister.com/2023/02/10/googles_go_programming_language_telemetry_debate/) |
| Angular CLI | **Opt-in** | "does not collect usage statistics unless you explicitly opt in"; prompted at install | [angular.dev/cli/analytics](https://angular.dev/cli/analytics) |
| .NET CLI | **Opt-out** (`DOTNET_CLI_TELEMETRY_OPTOUT=1`) | First-run notice; collects command, OS, SDK version, timing, 3-octet IP; older versions hashed MAC address and cwd | [Microsoft Learn](https://learn.microsoft.com/en-us/dotnet/core/tools/telemetry) |
| Homebrew | **Opt-out** (`brew analytics off`, `HOMEBREW_NO_ANALYTICS=1`) | Notice shown before first event; no user id or IP field in payload; values stripped from options; 365 d retention | [docs.brew.sh/Analytics](https://docs.brew.sh/Analytics) |
| Next.js | **Opt-out** (`next telemetry disable`, `NEXT_TELEMETRY_DISABLED=1`) | Command, version, machine info, build duration; `NEXT_TELEMETRY_DEBUG=1` prints payload instead of sending | [nextjs.org/telemetry](https://nextjs.org/telemetry) |
| GitHub CLI | **Opt-out** (enabled by default, April 2026) | Criticized as quietly enabled, mainly in release notes, no full list of data points | [Techzine](https://www.techzine.eu/news/devops/140736/github-cli-now-collects-usage-data-by-default/) |
| VS Code | not stated on the page read | Setting `telemetry.telemetryLevel`: all / error / crash / off | [VS Code docs](https://code.visualstudio.com/docs/configure/telemetry) |

Reading: incumbents mostly opt out with a notice; Go reversed to opt-in under backlash and the GitHub CLI default drew the same criticism. Two borrowable details: Next.js's payload-debug switch (our `telemetry preview`) and Go's counters-only local default. The Go team's stated reason for opt-out was that opt-in yields low adoption ([The Register](https://www.theregister.com/2023/02/10/googles_go_programming_language_telemetry_debate/)), so expect directional data only, which the schema is built for.

## 7. Phased plan

| Phase | Scope | Size | Gate |
|-------|-------|------|------|
| **1a** | Local counters: `schema.py`, `record()`, spool write, watchdog aggregator, instrument CLI/MCP/session/scheduler/doctor, derive `dc`/`hook`/`fail`/`rephrased` from audit rows, schema test (mutation: add an unbounded str field, test must fail), coverage test (every subcommand/tool maps to an enum), benchmark | L | hook p99 and `agentwire --version` delta under 0.5 ms |
| **1b** | Local surfaces: `stats`, `telemetry show/schema`, doctor line, `telemetry_stats` MCP tool, wiki page | S | owner reads two weeks of own data |
| **2a** | Config block, consent record, onboarding question, `status/preview/export/enable/decline/disable/delete`, protected key, DO_NOT_TRACK, **automatic weekly sender** with backoff/kill-switch handling, per-send token, client-side fold. Built and tested against a **local test endpoint** | M | owner reviews preview payload; no-socket-when-off test |
| **2b** | Real endpoint: isolated Cloudflare Worker + D1 per the spec, rate limiting, daily aggregation with k floor, "verify before building 2b" list resolved | M | counsel answers section 5; consent text approved |

1a and 1b are useful without Phase 2 and ship independently.

## 8. Owner decisions

**Settled (2026-10-20):**
1. Sharing is opt-in, default off; onboarding suggests it; no answer / Enter / non-interactive / skipped = off; declined recorded.
2. Local counters on by default with kill switch and doctor line. Pair events cut.
3. Collection is automatic after opt-in (weekly watchdog send); `export` is inspection only.
4. **Endpoint: isolated Cloudflare Worker + D1**, batch-per-row storage with a daily aggregation job. No shared secrets or stores with the website or anything else.

**Open, with recommendation:**
5. **Store format:** spool + daily counters (recommended) vs SQLite; accept no-fsync.
6. **Wipe survival:** counters disposable, `~/.agentwire/telemetry/` only (matches 2026-10-12).
7. **Identity:** per-send random token, no persistent id (recommended); or no token and Go-style sampling.
8. **k floor:** 10 server-side contributors, 3 client-side fold (proposed).
9. **Consent key protection:** mechanism for `config.yaml` (2a checks how `PROTECTED_COMMAND_PATTERNS` treats it).
10. **Re-ask policy:** only when the consent text changes materially (recommended).
11. **Legal:** who answers section 5, and is a privacy-notice page needed before 2b?
12. **Audit log retention:** unchanged, never uploaded (recommended).
13. **Rephrase flag:** accept new aggregator logic and its 5-minute window, or defer.
14. **Cloudflare plan and zone:** which plan and which domain for the isolated zone (rate-limiting needs one); raw-batch retention (proposed 7 days).

## Assumptions to verify

- Audit volume (17 MB / 10 days) is from this machine only.
- Hot-path costs are reasoned; Phase 1a measures them and is a stop condition.
- The EDPB letter was read in full text but is about cookie-pledge principles; it supports the consent requirements above, not a claim about identifiers.
- VS Code's default was not on the page read; row states only the setting.
