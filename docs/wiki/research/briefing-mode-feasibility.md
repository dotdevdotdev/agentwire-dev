---
name: Briefing Mode — asymmetric-verbosity orchestration (feasibility)
status: active
last_updated: 2026-06-21
---

# Briefing Mode — asymmetric-verbosity orchestration

> Feasibility + design report. Investigates building an orchestration mode where the **human-facing orchestrator is deliberately terse** (and splits its summary across voice vs. on-screen text so the two channels complement rather than duplicate) while **researcher worktree sessions are exhaustively verbose** and go deep. Researchers signal the orchestrator only with **passive, non-driving** "output ready" awareness; the orchestrator acts only when the **human** directs it.
>
> All file:line citations are against the `briefing-mode` branch as checked out (`~/worktrees/agentwire-dev-briefing-mode`). **This is a design report, not an implementation.** A few cheap spikes were used to verify load-bearing claims (e.g. that `msg` delivery presses Enter).

## TL;DR — Recommendation

**Build it. ~80% of the machinery already exists; the mode is mostly a pair of role files plus one genuinely new primitive.** The novel/uncertain part — "awareness without being driven" — is the only thing that needs new code, and it's small (~15–25 lines on top of the existing inbox).

The recommended shape, in one breath:

> An **anchor** session (terse, persona-replacing orchestrator role) shells out to `agentwire worktree` to spawn N **correspondent** worktree sessions (verbose researcher role stacked on the worktree-session safety contract). Correspondents write big markdown reports to their own worktree dirs and drop a **passive awareness signal** the watchdog never auto-delivers. The anchor sits idle until the human says "what's ready?", then *pulls* its inbox, reads the report files, synthesizes, and briefs the human across two complementary channels: a punchy spoken **`say`** headline + a richer scannable **`portal_notify`** card. Spawn more or tear down via worktree teardown. The anchor is never interrupted by a correspondent — only by the human.

What's missing is exactly one thing worth building: a **non-driving message flavor** (a `kind=ingest` / `passive: true` message the drain skips, that the orchestrator pulls on its own cadence). Everything else is role discipline + thin wrappers over primitives that already ship.

**Name recommendation:** keep **Briefing Mode** (self-documenting; the orchestrator *briefs*), with the **newsroom metaphor** for the roles — **anchor** (orchestrator) + **correspondents** (researchers). It maps perfectly onto asymmetric verbosity: the anchor is terse on-air, the correspondents file deep from the field. Other candidates in §7.

---

## A. Dual-channel `say` (voice) + text, with *different* content per channel

**Verdict: fully feasible, and the two decoupled channels already exist.** The asymmetry is achievable today with zero code as pure role discipline; a small unified API is a nice Phase-3 polish.

### What exists

- **`say` is TTS-only.** MCP `say(text, session, voice)` at `agentwire/mcp_server.py:795-830`; CLI `cmd_say` at `agentwire/__main__.py:2572-2633` (parser `11032-11044`). It shells to `agentwire say`, returns `"Queued speech."` to the agent, and **shows the human nothing on the desktop**. There is **no `display=`/`text=` second-content param anywhere**.
- **Smart routing** (`cmd_say`, `__main__.py:2612-2633`): `_check_portal_connections` (`__main__.py:2415`) → browser connected → POST `/api/say/{session}` (`_remote_say`, `__main__.py:3024-3052`); else local audio. Server side `api_say` → `speak()` (`server.py:4077-4113`, `5640+`) broadcasts audio only to that session's clients, with a local-speaker fallback (`server.py:5596-5613`). The text-to-speak is a single `text` field throughout — **no second stream**.
- **Incidental coupling (mobile only):** TTS broadcasts a `tts_start` WS frame carrying `text` (`server.py:5684`); the **mobile** chat client renders it as a bubble (`mobile.js:285-288`), but the **desktop** session window deliberately renders nothing (`session-window.js:737-738`). So on desktop, voice has no on-screen echo — which is *good* for us: the spoken and displayed channels are independent.
- **A fully decoupled text-to-screen channel already exists:** `portal_notify(text, session, priority)` — MCP `mcp_server.py:2825-2845` → `/api/desktop/notification` (`server.py:1305-1343`) → a persistent desktop **toast** (`notifications-panel.js:63-98`, body at `:76`). It does **not** speak. This is the asymmetric partner to `say`. (Note: `notify` / `notify-parent` at `mcp_server.py:834-854` is tmux text injection into *another session*, **not** a human-screen channel — wrong tool here.)

### Minimal change

| Option | Cost | Notes |
|---|---|---|
| **A. Pair the two existing tools** (recommended for MVP) | **Zero code** | Anchor calls `say("<spoken TL;DR>")` + `portal_notify("<richer card>", priority="high")`. Two channels, independent content, today. Caveats: two calls not one; toast body is HTML-escaped plain text (`notifications-panel.js:76`) so markdown/links render literally; on mobile the `say` text *also* appears as a bubble. |
| **B. Unified `say(text=..., display=...)`** (Phase-3 polish) | Small, contained | Add optional `display` param to `say` MCP (`mcp_server.py:796`) + CLI (`__main__.py:11033`); after queuing TTS, reuse `api_desktop_notification` (`server.py:1305`) for the text side. One atomic action. No new endpoint. |

**Recommendation:** ship the asymmetry as **role discipline over Option A** (the anchor role *tells* the model to split voice vs. text). Promote to Option B only if the two-call pattern proves clumsy in practice. Separately, upgrading the toast renderer to allow markdown/links (`notifications-panel.js:76`) is the one real frontend enhancement that makes the "scannable summary with structure/links" land well — worth doing regardless of A vs B.

---

## B. Role injection — concise anchor + verbose correspondent

**Verdict: the role system expresses both roles cleanly, and they stack correctly with the always-on worktree etiquette. No conflicts.**

### How roles resolve (`agentwire/roles/__init__.py`)

- Roles are markdown files with frontmatter, discovered project → user → bundled (`discover_role`, `__init__.py:296-332`), merged by union-of-tools / intersection-of-disallowed / concatenated-instructions (`merge_roles`, `__init__.py:120-160`).
- **Session kind is *derived* from the spawn verb, never user-set** (`derive_session_kind`, `__init__.py:228-240`): `agentwire new` → `orchestrator` (a **persona** — replaceable default); `agentwire worktree` → `worktree-session` (a **safety-rail** — non-overridable contract).
- **`resolve_roles` (`__init__.py:243-293`) is the crux for stacking:**
  - For **safety-rail kinds** (`worktree-session`, `worker`): `intrinsic etiquette + project roles + cli roles`, stacked and de-duped. `--roles` **adds** to the contract, never removes it (`__init__.py:277-286`).
  - For the **persona kind** (`orchestrator`): `--roles` / `.agentwire.yml roles:` **replace** the default (`__init__.py:288-293`).
- `soul` is auto-appended last unless excluded (`inject_soul`, `__init__.py:168-199`).

### What this means for Briefing Mode

- **Anchor role** = a persona that *replaces* the orchestrator default. Spawn the anchor with `agentwire new -s <name> --roles anchor` (or set it in `.agentwire.yml`). Because orchestrator is a replaceable persona (`__init__.py:288-293`), the anchor role fully owns the prompt — no leftover generic-orchestrator etiquette to fight. It should bake in: be terse with the human; split summaries asymmetrically across `say`+`portal_notify`; **never act on a correspondent signal until the human directs you**; pull the inbox only on the human's cue.
- **Correspondent role** = a verbose researcher role that **stacks on top of** the `worktree-session` safety contract. Spawn correspondents with `agentwire worktree <name> -p <repo> --roles correspondent`. Because `worktree-session` is a safety-rail kind, `--roles correspondent` **adds** to (never replaces) the isolation / verify / draft-PR / notify-back etiquette (`__init__.py:277-286`).

### The one tension, and why it's actually a fit

The `worktree-session` etiquette (`agentwire/roles/worktree-session.md:21-35`) says "when done: commit, push, open a **draft PR**, notify back." A correspondent producing a *research report* may not always want a PR. But this composes well:

- For **doc/research output**, the deliverable *is* the report file + the awareness signal — the "notify back" step *is* the briefing-mode signal, just with a different message flavor (§D's passive `ingest` kind instead of `done`). The correspondent role should override the *notify mechanism* (passive signal, not a driving `done`) while keeping isolation + verify.
- For **code spikes** the correspondent does, the draft-PR step is exactly right.

So the correspondent role overlays "be exhaustive; write a full report to a file; signal passively" on top of "stay isolated; verify in-worktree." No structural conflict — the safety rail handles *isolation*, the correspondent role handles *verbosity + signaling*. The only edit is that the correspondent's "finish" instruction points at the passive signal instead of the clobbering `notify-parent`.

---

## C. Spawning worktree sessions from an agent

**Verdict: the orchestrator must shell out to `agentwire worktree` today — and that's acceptable and safe. We should add an MCP `worktree_create` to close the loop, extending the #430 read/teardown tools.**

### Important branch-state correction

The brief says "we just shipped `worktree_status`/`worktree_list`/`worktree_remove` in PR #431 / #430." **Those MCP tools are NOT in this branch, and NOT in `origin/main`.** They exist only on an unmerged commit (`354b7a3`). Verified: `git merge-base --is-ancestor 354b7a3 HEAD` → not in branch; `origin/main`'s worktree history tops out at #314/#307. **So Briefing Mode's teardown story depends on #430 actually landing** — flag this as a dependency, not a given.

### What exists today

- **CLI `agentwire worktree`** (`cmd_worktree`, `__main__.py:5117`; argparse `11276-11293`) does all the heavy lifting: creates tmux session `{project}-{name}`, worktree under `~/worktrees/`, branch off `origin/<base>` honoring the `worktree.naming` template, registers branch↔session, launches via `cmd_new(kind='worktree-session')` so the etiquette auto-injects (`__main__.py:5186-5201`), and is **idempotent/reattaching**. Supports `--json` (`__main__.py:5324-5328`). **Gap: no seed-prompt flag** — it hardcodes `instructions=None` (`__main__.py:5199`), so you must follow up with a separate `session_send` to drive the new session.
- **MCP `session_create` can *already* create worktrees** via `project/branch` naming (`mcp_server.py:301-352`) — but through the older `cmd_new` path, **not** `cmd_worktree`, so it skips the naming template, the registry, and the `worktree-session` etiquette kind. It's a thinner, differently-shaped path. Use it for quick worktrees; use the CLI for the full contract.
- **No `worktree_create`/`worktree_spawn` MCP tool exists** anywhere (deliberately — even #430 left creation out: "no write verb by design").
- **Shelling out is safe.** The damage-control rules (`agentwire/hooks/damage-control/rules/agentwire.yaml`) block `agentwire …--force…remove` (`:25-26`) and `tmux kill-session …agentwire` (`:14-15`), but `agentwire worktree <name>` matches none of them; the CLI's internal `tmux kill-session`/`git worktree remove --force` run as subprocesses the hook never inspects. The bash hook matches the *agent's* command string only.

### Recommendation

- **Now:** the anchor shells out to `agentwire worktree <name> -p <repo> --roles correspondent --json`, parses the JSON, then `session_send`s the task. Acceptable.
- **Build:** add an MCP **`worktree_create`** — a ~10-line thin wrapper over `cmd_worktree --json`, sitting beside #430's `worktree_status`/`list`/`remove` to complete the lifecycle quartet (create + status + list + remove). The CLI is already SSOT and `--json`-capable; the tool just wraps it. **Also add a `--prompt`/`instructions` flag to `cmd_worktree`** so the MCP tool can spawn *and* seed in one call instead of create-then-`session_send`.

---

## D. Messaging semantics — "awareness without being driven" (the crux)

**Verdict: this is the one genuinely new primitive. Today's `msg` is non-clobbering but it DOES drive the agent (it presses Enter). We need a passive flavor the drain skips, that the orchestrator pulls on its own cadence. ~15–25 lines on top of the existing inbox.**

### Confirmed: `msg` delivery drives the recipient

Traced end to end and spike-verified:

1. `inbox.flush_session` → `prompt_router.safe_deliver(session, 0, rendered)` (`inbox.py:352`)
2. → `send_verified` → `send_to_session` → `pane_manager.send_to_target(f"{session}.0", message, enter=True)` (`session_ready.py:19` — **explicit `enter=True`**)
3. `send_to_target` pastes via tmux buffer **then sends Enter** (a second Enter for long/multiline) to *submit* (`pane_manager.py:251-294`).

So a drained `msg` is **pasted AND submitted** — it makes the recipient start a turn. The only difference from `session_send` is *timing* (waits for an empty box via `prompt_is_empty`, `prompt_router.py:323-333`) and *safety* (refuses parked/non-agent/dialog panes) — **not whether it drives.** And "empty box" is exactly the idle state of an anchor waiting for direction, so the watchdog (`inbox.tick` every 60s, `limits_cli.py:92-104`) will fire *precisely* when the anchor is idle. **This is the opposite of what we want.**

### What already gets us halfway

- **`msg_inbox` reads without consuming or driving.** MCP `mcp_server.py:419-441`; CLI `msg_cli.py:74-98` → `inbox.list_messages` → `pending_files` is a pure `glob`+read (`inbox.py:157-166`). No unlink, no paste. Docstrings say "does not drain." So **read-on-my-own-cadence already exists.**
- **But the watchdog drains unconditionally.** `flush_session` coalesces *all* pending files into one paste+submit (`inbox.py:338,351`); there is **no per-message or per-kind opt-out** of auto-delivery (the `Message` schema, `inbox.py:61-89`, has no `hold`/`passive` field). So today you can read passively, but you can't *stop* the auto-drive — the drain wins within 60s.

### The three options, judged

| Option | Verdict |
|---|---|
| **(i) New non-driving message flavor** (`kind=ingest` or `passive: true`) the drain skips | **Recommended.** The real gap. Combined with `msg_inbox` it *is* the desired primitive. |
| **(ii) Poll `msg_inbox` on the anchor's cadence** | **Already works for *reading*** (no consume, no drive) — but useless alone, because the watchdog still auto-pastes the same messages within 60s. Only viable *with* (i). |
| **(iii) Existing `note`/`done` + role discipline** | **Insufficient.** Role discipline shapes the *reaction* but cannot stop the *paste+Enter*. A submitted prompt is a turn — the anchor is already woken/driven even if instructed to do nothing. Mitigates, doesn't solve. |

### Recommended primitive (minimal)

A **passive, pull-drained** message flavor:

1. **Add a flavor** — a `passive: true` field on `Message` (`inbox.py:61-85`), set by `msg send --passive` or a dedicated `--kind ingest`.
2. **Skip it in the auto-drain** — one filter in `flush_session`/`list_messages` (`inbox.py:338,351`) so passive messages are never coalesced/pasted, and `_iter_pending_sessions` ignores inboxes holding only passive messages (so the watchdog leaves them alone).
3. **Add a voluntary pull** — `msg pull` / `msg_inbox(consume=true)` that returns *and clears* passive messages. This is the anchor **voluntarily ingesting** when the human says go — the inverse of being pushed. (Without a clear step, passive messages would pile up forever, since the drain now skips them.)

The result: correspondents send `msg send --to <anchor> --kind ingest "report ready: <path>"`; it lands silently; the anchor is never driven; when the human says "what's ready?", the anchor calls `msg pull`, sees the pending pointers, reads the files (§E), synthesizes, briefs (§A). It reuses the entire inbox/dead-letter/`msg_inbox` machinery — **a flavor + a filter + a pull, not a new subsystem.**

> **Zero-code alternative for the MVP:** skip `msg` entirely for awareness. Correspondents write reports to a **filesystem dropbox** (a dir); the anchor lists that dir when the human asks. A directory listing drives nothing — passivity is intrinsic to "files in a folder." This ships Phase 1 with no inbox changes; the passive `ingest` kind is the Phase-2 upgrade that adds sender/kind/timestamp/dead-letter metadata to the awareness signal.

---

## E. Content transfer — verbose report → concise synthesis

**Verdict: a plain markdown file the correspondent writes + a small awareness pointer is the clean answer. Confirmed against the code; no large-payload mechanism is needed or wanted.**

Ranked candidates (all verified in code):

1. **Plain file + awareness pointer (WINNER).** A correspondent is a worktree session — it already has a private dir (`~/worktrees/<name>/`). It `Write`s `findings.md` there and sends a small signal carrying the **absolute path**. The anchor `Read`s it on the human's cue. **Zero size limit, no clobbering, durable, uses only existing primitives.** Optionally standardize a blessed dropbox (`~/.agentwire/research/<anchor>/`) so paths are predictable — but the worktree-relative absolute path needs *no new infra*.
2. **Scratchpad** — `scratchpad_add`/`list` (MCP `mcp_server.py:2367-2408`; storage `~/.agentwire/scratchpad.json`, `scratchpad.py:20`) is genuinely shared/global cross-session. **But** it caps notes at `MAX_NOTE_CHARS = 20_000` and silently truncates above it (`scratchpad.py:22,67`), and it's framed as the human's portal notes drawer. **Good for the *short final briefing*, wrong for the raw verbose report.**
3. **Handoff bundle** — `handoff_init`/`render`/`list` (`mcp_server.py:1569-1654`) produces `ai-handoff.md` + HTML in `~/.agentwire/artifacts/handoff-<slug>/`. LLM-to-LLM by design, but it's a fill-template-then-render ceremony for distilling a *whole conversation* — **heavyweight** for "here's my report"; the MD it emits is just a file you could `Write` directly.
4. **Wiki** (`~/.agentwire/wiki/`) — right for **durable, compounding** findings worth keeping across runs; **overkill** as a per-run handoff buffer (pollutes the lint/ground-truth lifecycle).
5. **`msg` body** — **never** for the report. The `Message` payload is a single `text` field (`inbox.py:62-89`) pasted into a live prompt box; a big paste renders as a `[Pasted text +N lines]` placeholder that verification can't even confirm landed (`session_ready.py:105-117`). **Perfect for the *pointer*, unusable for the *content*.**

**Missing for max ergonomics (optional):** (a) a blessed `~/.agentwire/research/<anchor>/` dropbox dir + a one-line resolver, mirroring how `inbox/` nests per-session; (b) a typed `ref:`/`path:` field on `Message` (`inbox.py:62`) so pointer-messages are self-documenting instead of free-text-by-convention. Both are nice-to-haves, not blockers.

---

## F. Lifecycle — the full loop

```
                    ┌──────────────────────────── HUMAN ────────────────────────────┐
                    │  "research X across these angles"        "what's ready?" / "go" │
                    ▼                                                   ▼              │
            ┌───────────────┐                                  ┌───────────────┐      │
            │    ANCHOR     │  spawn (shell: agentwire         │    ANCHOR     │      │
            │  (terse role) │  worktree … --roles correspondent│  pulls inbox  │      │
            │               │  --json) → session_send task     │  (msg pull)   │      │
            └──────┬────────┘                                  └──────┬────────┘      │
                   │ 1..N                                             │ reads report  │
        ┌──────────┼───────────┐                                     │ files (E)     │
        ▼          ▼           ▼                                      ▼               │
   ┌─────────┐┌─────────┐┌─────────┐                          synthesize → BRIEF:    │
   │CORRESP. ││CORRESP. ││CORRESP. │  each: verbose role +     • say("spoken TL;DR")  │
   │worktree ││worktree ││worktree │  worktree-session safety  • portal_notify(card)  │
   │ (deep)  ││ (deep)  ││ (deep)  │  → Write findings.md      (asymmetric, §A) ──────┘
   └────┬────┘└────┬────┘└────┬────┘  → msg --kind ingest
        │ PASSIVE  │ PASSIVE  │ PASSIVE  "ready: <path>"  (never drives anchor, §D)
        └──────────┴──────────┴──────────────► anchor inbox (silent until pulled)

   teardown: anchor → worktree_remove (needs #430)  ·  spawn more: loop back to top
```

1. **Human → Anchor:** "research X, these angles." Anchor stays terse.
2. **Anchor spawns N correspondents** via `agentwire worktree … --roles correspondent` (or MCP `worktree_create` once built), seeds each with a deep-dive task.
3. **Correspondents go deep** (verbose role) inside isolated worktrees, each `Write`ing a full `findings.md`.
4. **Correspondents signal passively** — `msg --kind ingest "ready: <abs path>"`. The watchdog **does not deliver** it (§D); the anchor is never interrupted.
5. **Anchor idles** until the **human** says "what's ready?" / "go."
6. **Anchor pulls** its inbox (`msg pull`), reads the report files (§E), **synthesizes**, and **briefs asymmetrically** — spoken headline via `say` + scannable card via `portal_notify` (§A).
7. **Human directs more or less:** spawn more correspondents, or **tear down** via worktree teardown (`worktree_remove`, dependent on #430). Loop.

The anchor is the calm, terse funnel: pushed only by the human, pulling from correspondents on its own cadence.

---

## 7. Name suggestions

| Name | Metaphor | Notes |
|---|---|---|
| **Briefing Mode** *(recommended — keep it)* | The orchestrator *briefs* | Self-documenting, already the working name, matches the `say`+text briefing action. Pair with newsroom role names below. |
| **anchor / correspondents** *(recommended role names)* | News anchor + field reporters | Maps *exactly* onto asymmetric verbosity: anchor terse on-air, correspondents deep in the field. Use as the two role-file names regardless of the mode name. |
| **Funnel Mode** | The orchestrator is the narrow point | Captures "many verbose → one terse." A bit abstract. |
| **Situation Room** | Briefer + analysts | Evocative, slightly heavy. |
| **Dispatch** | A dispatcher fanning out and collating | Clean, but overloads the word "dispatch" already used for scheduler/worktree runs. |

**Pick:** *Briefing Mode* for the mode, **`anchor`** + **`correspondent`** for the two role files.

---

## 8. What's new vs. what exists — the build surface

| Capability | Status | Where |
|---|---|---|
| Decoupled voice channel (`say`) | **EXISTS** | `mcp_server.py:795-830` |
| Decoupled text-to-screen channel (`portal_notify` toast) | **EXISTS** | `mcp_server.py:2825-2845`, `server.py:1305-1343` |
| Asymmetric voice≠text content | **EXISTS via pairing** (role discipline); unified `say(display=)` = polish | §A |
| Role stacking: persona anchor + safety-rail correspondent | **EXISTS** | `roles/__init__.py:243-293` |
| Worktree creation from an agent | **EXISTS via CLI** (`agentwire worktree`); MCP `worktree_create` = new thin wrapper | §C |
| Worktree seed-prompt in one step | **MISSING** (`cmd_worktree` hardcodes `instructions=None`) | `__main__.py:5199` |
| Worktree teardown / read-status MCP tools | **DEPENDS ON #430** (not in branch or main) | commit `354b7a3` |
| Read inbox without driving | **EXISTS** | `msg_inbox`, `inbox.list_messages` |
| **Passive, non-driving awareness signal** | **MISSING — the one core primitive** | §D |
| Big report → synthesis (file + pointer) | **EXISTS** (plain file + `msg` pointer) | §E |
| Blessed research dropbox dir / typed `ref` field | **MISSING** (nice-to-have) | §E |

---

## 9. Phased build plan

**Phase 1 — MVP, ~zero new code (role discipline + filesystem awareness).**
- Write two role files: `anchor.md` (terse; asymmetric `say`+`portal_notify`; never act until the human directs; pull awareness on cue) and `correspondent.md` (exhaustive; write `findings.md`; signal passively).
- Anchor shells out to `agentwire worktree … --roles correspondent --json` and `session_send`s tasks.
- **Awareness via filesystem dropbox** (correspondents write to a known dir; anchor `ls`/reads on the human's cue) — non-driving by construction, no inbox changes.
- Asymmetric briefing via **pairing** `say` + `portal_notify` (Option A).
- *Ships the whole loop today.* Wart: awareness is just files (no sender/kind/dead-letter metadata).

**Phase 2 — the core primitive + lifecycle MCP (small, focused code).**
- **Passive `ingest` message flavor** + drain-skip + `msg pull` (§D) — upgrades awareness to typed, durable, dead-lettered signals that still never drive the anchor.
- **MCP `worktree_create`** (thin wrap of `cmd_worktree --json`) + a `--prompt` seed flag on `cmd_worktree` — closes the loop so the anchor spawns+seeds without shelling out, and completes the #430 quartet.
- Depends on **#430 landing** for `worktree_remove`/`worktree_status` teardown.

**Phase 3 — polish.**
- Unified `say(text=, display=)` shape (Option B) + markdown/link rendering in the toast body (`notifications-panel.js:76`).
- Blessed `~/.agentwire/research/<anchor>/` dropbox + resolver; typed `ref:`/`path:` field on `Message`.

**Quick win:** Phase 1 + the Phase-2 passive-message primitive together deliver the *real* experience (non-driving awareness with proper signals) for very little code. The rest is ergonomics.

---

## Open questions

- **#430 dependency:** Briefing Mode's teardown (`worktree_remove`) and read-status (`worktree_status`) want the #430 MCP tools, which aren't merged. Should Phase 2 wait on #430, or carry its own thin wrappers?
- **Passive-message clearing:** is `msg pull` (consume-on-read) the right clear semantic, or should passive messages auto-expire on a TTL so a forgetful anchor doesn't accumulate them? (Dead-letter already exists for driven messages; passive ones need their own housekeeping since the drain skips them.)
- **Correspondent PR etiquette:** for pure research output, should the correspondent role suppress the worktree-session draft-PR step (deliverable is the report, not code), or always open a draft PR carrying the report for the human record? Leaning: report-only correspondents skip the PR; spike correspondents keep it.

## Related

- [[orchestration-transport-alternatives]] — why we stay SSH/file-based rather than adding a daemon; reinforces "file + pointer" over a streaming transport for content handoff.
