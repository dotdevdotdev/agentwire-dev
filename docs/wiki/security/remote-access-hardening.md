# Remote-Access Hardening — Threat Model & Plan

> Living document. Update this, don't create new versions.
>
> Audit deliverable for [#396](https://github.com/dotdevdotdev/agentwire-dev/issues/396)
> (portal-boundary hardening) and [#420](https://github.com/dotdevdotdev/agentwire-dev/issues/420)
> (networking/tunnel footprint). Verified against the code on 2026-06-22.
> Implementation lands in [#423](https://github.com/dotdevdotdev/agentwire-dev/issues/423)
> / [#424](https://github.com/dotdevdotdev/agentwire-dev/issues/424)
> / [#425](https://github.com/dotdevdotdev/agentwire-dev/issues/425).

## TL;DR

The premise this audit started from — *"wide-open, unaudited any-device→tunnel→shell"* — is
stale. The core boundary already shipped (PR #247/#248): loopback default, bearer-token auth,
refuse-to-start-without-token on non-loopback, constant-time compare, MCP stdio-only.

Two corrections frame everything below:

1. **agentwire owns no internet tunnel.** The phone→portal "from anywhere" path is the user's
   own cloudflared/tailscale/ngrok — documentation, never code. agentwire's responsibility ends
   at *"the portal refuses unauthenticated requests regardless of what's in front of it."* (#420)
2. **The real residual risk is blast radius, not the front door.** One shared god-token unlocks
   the entire ~90-route API, including a live interactive shell and the ability to disable its own
   auth. The hardening wave shrinks that: per-device credentials (#423), capability scopes (#424),
   and freezing security-critical config from the API (#425).

---

## What shipped (2026-06-22)

The hardening wave landed as **#425 + #423** plus the **#420** networking cuts. **#424 (capability
scopes, `full` vs `ptt`) was dropped not-planned** — its only real use case (restricted guest/PTT
devices) was cut by the owner, so every credential is full-access and there is no scope system.

| Item | Status | Shape |
|---|---|---|
| **#420** networking cuts | ✅ shipped | Tunnel auto-spawn removed from `portal start`; reverse-tunnel (`autossh -R` / `~/.local/bin/agentwire-tunnels`) guidance stripped from `machine add/remove`; `network status` is read-only (already was — confirmed). `agentwire tunnels *` stays as an opt-in manual helper, never auto-fired. |
| **#425** freeze config | ✅ shipped | `POST /api/config` rejects (403) any change to frozen keys `server.auth_token`, `server.host`, `executables`, `services`, `safety`; `POST /api/safety/config` is frozen entirely (host-edit-only). The read-side redaction round-trip is now reversed on save so the editor can't blank `auth_token`. Constant: `security.FROZEN_CONFIG_KEYS`. |
| **#423** per-device creds | ✅ shipped | New `agentwire/devices.py`: `devices.json` registry (0600) stores a **sha256 hash** per device + `pairings.json` for short-lived codes. Pairing: `agentwire portal pair` → code + QR → `GET /pair?code=` page → `POST /api/pair` mints a device token. Middleware resolves the presented token to a device (bootstrap token = synthetic `host` device, else registry lookup); unknown/revoked → 401. CLI: `portal pair` / `portal devices` / `portal revoke <id>`. |
| **#424** scopes | ❌ dropped | Not-planned. No `full`/`ptt`, no per-route allowlist, no PTT session whitelist. |

**Auth model now:** the bootstrap token (`~/.agentwire/portal.token` / `server.auth_token`) is the
host/owner full credential used by the CLI, MCP, hooks and daemons — unchanged. *Remote* devices no
longer paste that shared token; they pair to get their own named, individually-revocable credential.
Revoking one device (`agentwire portal revoke dev_xxxx`) doesn't log out the others. Every credential
is full-access. The registry is read through an mtime-cached loader so revocation takes effect on the
next request without a portal restart.

---

## Part 1 — Network footprint map (#420)

### What agentwire actually opens / owns

| # | Touchpoint | Code | What it is | Verdict |
|---|---|---|---|---|
| **C** | Portal bind / host / token / TLS | `config.py` (`ServerConfig.host="127.0.0.1"` @ `config.py:61`, `SSLConfig`), `server.py`, `security.py` | **The actual boundary.** HTTP(S) listener, default `127.0.0.1:8765`; non-loopback bind requires a token; self-signed TLS when cert+key exist. | **KEEP** — the only thing #396 hardens |
| **A** | `agentwire tunnels up/down/status/check` + MCP `tunnels_*` | `tunnels.py` (385 LOC), `network.py` (206), CLI handlers | SSH `-L` port-forward **manager** (create / track-PID / health / teardown) to reach a *service* on another box. Auto-invoked at **portal startup** (`__main__.py:835-857`, via `NetworkContext.get_required_tunnels()` + `TunnelManager.create_tunnel`). | **CUT the auto-spawn** (scope-down or delete the manager) |
| **B** | `agentwire network status` + MCP `network_status` | `__main__.py` | Read-only diagnostic: machine SSH reachability + service health + tunnel rows + worker sessions. | **SCOPE-DOWN** to read-only (decouple from create-missing) |
| **D** | Remote-machine wiring | `machines.json`, `machine add/remove/list`, MCP `machine_*` | SSH-based remote **session** management (`name@machine`, `ssh -t … tmux attach`). Core feature. But `machine add` prints `autossh -R` reverse-tunnel next-steps + references `~/.local/bin/agentwire-tunnels`. | **KEEP**, strip the reverse-tunnel print |
| **E** | Tunnel-provider integration (cloudflared/ngrok/tailscale) | **none** | Doc-only (`remote-access.md`). `grep -rE 'cloudflared\|ngrok\|tailscale' agentwire/` over code returns only CORS comments. | **already BYO** — reframe docs only |

### The key fact about the SSH service-router (A)

`get_required_tunnels()` (`network.py:124`) returns **empty** unless a service has `.machine` set
*and* it resolves non-local. For the default single-box install **none of the tunnel machinery
ever fires.** It only goes live in a multi-machine **service split** — historically remote-GPU
TTS/STT — a scenario that got materially rarer: STT `default` is now an in-process shim, TTS
`default` is browser/OS voice. The remote-GPU-over-`ssh -L` case is increasingly vestigial.

So the auto-spawn at portal startup is dead weight on every normal install and a "we're in the
networking business" liability the owner wants out of.

### Target posture (#420)

> agentwire owns the portal's **local security boundary** (127.0.0.1 default, token-gated LAN
> opt-in, self-signed TLS — see Part 2/3) and SSH-based remote **session** management (machines
> list, `/api/sessions/remote`, `ssh -t … tmux attach`). It does **not** own internet exposure or
> service-routing tunnels — those are bring-your-own, documented but never code.

### #420 follow-up cuts (separate from the #396 auth wave)

- Remove the tunnel auto-spawn from `portal start` (`__main__.py:835-857`); decide delete-vs-thin-helper for `agentwire tunnels *` / `tunnels.py` / the `network.py` tunnel paths.
- Reframe `docs/wiki/deployment/remote-access.md` as a provider-agnostic BYO-tunnel guide; strip personal `solodev.dev` specifics; state plainly that agentwire ships no tunnel code.
- Strip `autossh -R` reverse-tunnel guidance + `~/.local/bin/agentwire-tunnels` from `machine add/remove` output — keep pure session management.
- Scope `network status` / `network_status` to read-only diagnostics.
- Add a single "Exposing the portal" posture doc (local boundary vs BYO internet), linked from quickstart.

These are the networking-footprint deliverables. **None of them is a prerequisite for the auth
hardening below** — the auth wave hardens *our* listener, which is ours regardless of who owns the
tunnel in front of it.

---

## Part 2 — Threat model & current auth analysis (#396)

### The path

```
any device  ──▶  BYO tunnel (cloudflared/tailscale)  ──▶  portal :8765  ──▶  tmux / shell
            (user owns this — agentwire ships no code)    (agentwire's boundary)
```

agentwire can only harden the last hop. Everything below is about that hop.

### How a device authenticates today

- **Single shared bearer token** at `~/.agentwire/portal.token` (0600, auto-generated by
  `ensure_auth_token`, `security.py:76`).
- HTTP: `Authorization: Bearer <token>`. WebSocket: `Sec-WebSocket-Protocol: agentwire.bearer.<token>`
  subprotocol (keeps the token out of the URL → out of logs/referrers — good).
- Constant-time compare (`hmac.compare_digest`, `security.py:210`).
- One aiohttp middleware (`create_security_middleware`, `security.py:190`) enforces **two** layers:
  origin/CSRF check on every mutation + WS upgrade (always on), and token check on everything
  outside the public bootstrap surface.
- `server.auth_token` semantics: `None` → use token file; `""` → auth disabled (loopback only);
  any string → explicit override.
- **Refuse-to-start** guard: non-loopback bind with auth disabled is a hard `SystemExit`
  (`validate_startup_security`, `security.py:123`, wired at `server.py:5838`).

### What a device can do once in

The token is **binary and god-mode** — one secret unlocks the entire ~90-route API. By blast
radius:

| Tier | Routes | If the token leaks |
|---|---|---|
| **CRITICAL — full RCE** | `GET /ws/terminal/{name}` (`tmux attach` → live shell, incl. `ssh -t … tmux attach` to remote machines), `POST /send/{name}` (arbitrary keystrokes), `POST /api/create`·`recreate`·`spawn-sibling`·`fork`, `POST /api/scheduler/tasks/{name}/run`, `POST /api/desktop/*`, `POST /api/council/start` | Own the dev box + any SSH-reachable remote machine |
| **HIGH — config/persistence** | `POST /api/config` (writes raw `config.yaml` — can rewrite `executables`/`services` = RCE, set `host`, or set `auth_token: ""` to **disable auth entirely**), `POST /api/config/reload`, `POST /api/safety/config` (**disable rm-rf damage-control**), `POST/DELETE /api/machines` | Persist, escalate, and turn off the safety net with the same token |
| **MEDIUM — info disclosure** | `GET /api/sessions{,/local,/remote}`, `/api/projects`, `/api/history{,/{id}}` (+resume), `/api/roles`, `/api/scratchpad`, `/api/scheduler/{board,events,live}`, `/api/council/archive`, session output WS `/ws/{name}` | Leak project paths, full conversation history, machine inventory, notes |
| **MEDIUM — input/cost/write** | `POST /transcribe` (burns cloud STT credits if a cloud backend is set), `POST /upload`, `POST /api/artifacts/upload`, `DELETE /api/artifacts/{filename:.+}` | Credit burn, disk writes |

**Public / unauthenticated** (`_is_public_path`, `security.py:170`): `GET /`, `GET /mobile`,
`GET /health`, `GET /static/*`. Page shells + healthcheck — do nothing without the token, but
confirm "an agentwire portal lives here" (fingerprint).

### Residual gaps (ranked)

1. **Single shared token, not per-device.** Your laptop and a phone that only does PTT hold the
   *same* god-token. Can't revoke one device, can't attribute actions, rotation logs out everyone.
   → **#423**
2. **No command scoping.** A PTT-only phone can `tmux attach` to a root-equivalent shell. PTT
   should be a capability subset, not the whole surface. → **#424**
3. **Config-write is an auth-disable + RCE pivot.** `POST /api/config` writes raw YAML; an attacker
   who's in can set `auth_token: ""`, rewrite `executables`, and persist. → **#425**
4. **Safety rules are API-writable.** `POST /api/safety/config` can disable the rm-rf hooks with
   the same token — defense-in-depth defeated by one secret. → **#425**
5. **No auth-failure logging, no lockout, no alerting.** 401s are silent. → backlog (action E).
6. **Token transport relies on opt-in TLS.** SSL only turns on if cert+key exist (`config.py:45`).
   A non-loopback *plaintext* LAN bind sends the bearer token in cleartext. Fine when the BYO
   tunnel terminates TLS; a footgun for a bare LAN bind. → backlog (action F).
7. **Unauthenticated fingerprint.** `/health`, `/`, `/mobile` confirm agentwire pre-token.
   → backlog (action G), low priority.

### What's already good (the floor — bless it, don't rebuild)

- Loopback default (`config.py:61`).
- Refuse-to-start on non-loopback without a token (`security.py:123`).
- Constant-time token compare (`security.py:210`).
- WS token in subprotocol, not the URL.
- MCP server is stdio-only (`mcp_server.py:3325`, `transport="stdio"`) — started by Claude Code
  locally, **never network-exposed.** Not part of the remote surface at all.
- **Artifact DELETE path-traversal is already closed** — `api_artifacts_delete` (`server.py:1447`)
  regex-rejects any filename containing `/` or a leading `.`, so the `{filename:.+}` route can't be
  walked out of the artifacts dir. (Audit's earlier "open question" → resolved. The *upload* write
  target is still worth a glance but is constrained to the artifacts dir.)

---

## Part 3 — Prioritized hardening plan (frames #423 / #424 / #425)

Sequence is dictated by dependency, not just risk: **#425 first** (highest leverage, no
prerequisites — stops the token from turning off its own protections), then **#423** (per-device
credentials, the substrate), then **#424** (scopes, which *need* per-device identity to attach to).

### #425 — Freeze security-critical config from the portal API *(do first)*

**Why first:** zero dependencies, highest leverage. Today one leaked token can permanently disable
every other defense. Closing the write path means even a fully-compromised token can't escalate to
persistence or auth-disable.

**Design:**
- In `api_save_config` (`POST /api/config`) and `api_safety_config_post` (`POST /api/safety/config`),
  treat a fixed set of keys as **host-file-edit-only** and reject any request that attempts to
  change them: `server.auth_token`, `server.host`, `executables`, `services` (the RCE-bearing
  ones), and the safety-disable toggles.
- Reject = compare the incoming value against the on-disk value for those keys; if changed, return
  `403` with a message naming the frozen key and pointing to "edit `~/.agentwire/config.yaml` on
  the host." Don't silently drop — be honest about why.
- Read-side redaction already exists (`server.py:3583`) but is cosmetic; this closes the **write**
  path it implies.
- Frozen-key list lives in one constant so it's auditable and so #424's `ptt` scope can reuse it
  ("ptt can't touch config at all" is a superset of "nobody can touch these keys via API").

**Verification:** with a valid token, attempt `auth_token: ""`, add an `executables` entry, and
disable a safety rule via the API — each returns 403; each still editable by hand on the host.

### #423 — Per-device credentials + pairing flow *(substrate for scoping)*

**Why second:** #424 needs a device identity to hang a scope on. This issue creates that identity.

**Design:**
- Replace the single `portal.token` with a **device registry** under `~/.agentwire/` (e.g.
  `devices.json`, 0600): each entry `{ id, name, token_hash, scope, created, last_seen, revoked }`.
  Store a **hash** of each device token, not the token itself (the host never needs the plaintext
  after issuance).
- **Issuance via pairing**, host-shown: `agentwire portal pair [--name <device>] [--scope full|ptt]`
  prints a short-lived pairing code and a QR (encoding the portal URL + code). The device posts the
  code to a `POST /api/pair` endpoint (itself gated by the pairing code, time-boxed), receives a
  freshly-minted device token, and stores it in `localStorage` as today.
- Middleware change: `_extract_token` stays, but the compare becomes "hash the presented token,
  look it up in the registry, reject if missing/revoked." Attach the resolved device (id + scope)
  to the request for downstream use (#424 + attribution).
- **Backward-compat:** none required (pre-launch). The existing single `portal.token` becomes the
  bootstrap/first-device credential or is migrated to one registry entry on upgrade — pick one,
  don't keep both code paths.
- CLI: `agentwire portal devices` (list), `agentwire portal revoke <id>` (revoke one without
  logging out the rest), `agentwire portal pair` (add).

**Verification:** pair two devices; revoke one; the revoked device gets 401 on every non-public
route while the other keeps working; actions are attributable to a named device.

### #424 — Capability scopes (full vs ptt) *(the blast-radius shrink)*

**Why last:** depends on #423's per-device identity. This is the change that turns a leaked phone
token from full RCE into "can talk to one session."

**Design:**
- Each device carries a **scope** (`full` | `ptt`), stored in the #423 registry.
- Middleware maps `scope → route allowlist`. Implement as a single declarative table
  (route-pattern → allowed scopes) checked in `security_middleware` after token resolution, so the
  policy is one auditable place, not scattered per-handler.
- **`ptt` allowlist (minimum viable):** `POST /transcribe` and `POST /send/{name}` — but
  `/send/{name}` constrained to a **whitelisted session** (the device's paired target), not
  arbitrary `{name}`. Plus the public bootstrap surface. **Denied for `ptt`:** `/ws/terminal/*`,
  `/api/create`·`recreate`·`spawn`·`fork`, `/api/config`, `/api/safety/config`,
  `/api/scheduler/tasks/*/run`, `/api/machines`, everything else → `403`.
- `full` retains everything (subject to #425's frozen-config keys, which apply to all scopes).
- The PTT target whitelist lives on the device entry (`{ scope: "ptt", session: "<name>" }`), set at
  pairing time (`agentwire portal pair --scope ptt --session mysession`).

**Verification:** pair a `ptt` device; it can transcribe + send to its whitelisted session but gets
`403` on `/ws/terminal/*`, `/api/create`, `/api/config`, `/api/safety/config`; a `full` device
retains everything.

### Backlog (pull when ready — not in the first wave)

- **Auth-failure audit log + per-IP lockout + optional owner email on a burst** (reuse the Resend
  wiring from usage-limit recovery). Closes gap #5.
- **TLS-or-loopback enforcement** — refuse a non-loopback *plaintext* bind without an explicit
  `--insecure`; document that the BYO tunnel must terminate TLS. Closes gap #6.
- **Reduce unauthenticated fingerprint** — minimal `/health`, optional generic token-gate on the
  page shells. Closes gap #7 (low priority).
- **Verify the artifact *upload* write target** stays constrained to the artifacts dir (delete is
  already safe).

---

## Sequencing summary

```
#420 networking cuts        ── independent ──▶  (docs + tunnel auto-spawn removal)
#425 freeze config  ─────────────────────────▶  do first (no deps, highest leverage)
#423 per-device creds  ──┐
                         ├──▶  #424 capability scopes (needs device identity)
                         └──▶  attribution + revocation
backlog: 401 log/lockout, TLS-or-loopback, fingerprint, upload-target
```

The auth wave (#423/#424/#425) and the networking cuts (#420 follow-ups) are **independent** — they
touch different code and neither blocks the other. Within the auth wave the order is
**#425 → #423 → #424**.
