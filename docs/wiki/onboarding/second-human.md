# 10-Minute Second-Human Onboarding

> Design/scope doc for [#390](https://github.com/dotdevdotdev/agentwire-dev/issues/390). Implementation follows owner review. Living document — update, don't fork.

## The goal, stated precisely

Get **exactly one person who isn't the builder** from zero to **their first push-to-talk utterance against a Claude session** in **under ~10 minutes**, via **roughly one command + a QR/link**.

This is a validation move, not a feature. The binding constraint on the project is `n=0 → n=1` external feedback: nobody but the builder has ever reached the core loop, and the install cliff is the prime suspect. So the design goal is **ruthless removal of everything a stranger must do by hand** — not a richer setup wizard.

The success test is concrete and adversarial: hand a fresh device + a link to someone who isn't you, *don't help*, and time them to first successful utterance. Target < 10 min.

---

## Who does what: the guest is a *device*, not an operator

The single most important framing decision. There are two readings of "guest onboarding":

| Model | Guest must… | Time to first utterance | Verdict |
|---|---|---|---|
| **A — Guest-as-device** (host provisions) | Open a link, accept a cert once, hold a button | ~2–5 min | **This is the MVP.** |
| **B — Guest-as-operator** (full self-install) | Install brew/tmux/ffmpeg, `pip install`, auth Claude Code, `agentwire init`, start portal… | 30–90 min, high failure rate | Out of scope for #390. |

Everything about #390 — "one command or QR", "first utterance", "the install cliff is why no one reached the loop" — points at **Model A**. The dependency issues confirm it: [#424](https://github.com/dotdevdotdev/agentwire-dev/issues/424) literally describes *"a phone whose only job is push-to-talk"* getting a scoped credential to one session. **That phone is the guest.**

So: **the host (builder) runs one command. The guest just talks.** The guest never installs anything, never sees a terminal, never authenticates Claude. Their Claude session runs on the host's machine; their device is a microphone + speaker + screen.

> "Their own machine / their own Claude session" in the issue resolves to: *their own isolated session* (named for them, scoped to them) running on the host. Not a second agentwire install. Model B (the guest stands up their own box) is a real but separate journey; it's the existing `agentwire init` path, and #390 is explicitly the short one.

---

## Current onboarding friction (what a newcomer faces today)

Tracing the real code paths a second human hits today:

### Host-side setup (the builder already did this, mostly)
`agentwire init` → `agentwire/onboarding.py::run_onboarding()` asks 3 questions (projects dir, agent, standalone-vs-multi), writes `config.yaml`, optionally installs the tmux config, then hands off to a Claude "init" role session for TTS/STT/SSL. This is fine for the builder and **irrelevant to a guest** — it configures the host, not a visitor.

### What a guest must do today to reach PTT — the cliff
There is **no guest path**. To get a second person talking, the host currently has to manually assemble all of this:

1. **Expose the portal off-loopback.** Default standalone bind is `127.0.0.1` (`onboarding.py` writes `host: "127.0.0.1"`). A guest device can't reach loopback. The host must switch to `host: "0.0.0.0"`, which `security.validate_startup_security()` *refuses* unless an auth token exists.
2. **Generate SSL certs.** Browser mic access requires HTTPS (`agentwire generate-certs`). Self-signed → the guest hits a scary cert warning and must click through "Advanced → proceed".
3. **Get the URL to the guest.** LAN IP (`https://192.168.x.x:8765`) typed by hand, or a full **Cloudflare Tunnel + Cloudflare Access** setup (`docs/wiki/deployment/remote-access.md`) — which needs a domain, a Cloudflare account, `cloudflared`, a tunnel, DNS, and an Access policy. That's a 30-minute task on its own.
4. **Hand over the god-token.** Today there's exactly one token (`~/.agentwire/portal.token`), read by `security.py`. The host reads it (`agentwire portal token`) and the guest pastes it into their browser. **That token is root-equivalent** — it can open `GET /ws/terminal/{name}` → `tmux attach` → an interactive shell on the host (this is precisely the gap [#424](https://github.com/dotdevdotdev/agentwire-dev/issues/424) calls out). Handing it to a guest is handing them the box.
5. **Bind the guest to a session.** Nothing routes a fresh device to "your session". The guest lands on the portal and has to understand the desktop UI, find/create a session, and figure out which one is theirs.
6. **STT actually works for them.** The `default` STT tier is **Chrome-only browser speech recognition** (`roles/init.md`, quickstart §3). A guest on iOS Safari gets *no transcription*. PTT silently does nothing.

Net: today, "let a friend try it" is a 30–90 minute expert task with a security footgun (#4) and a silent-failure trap (#6). No wonder n stayed at 0.

---

## Proposed flow: `agentwire guest`

One host command provisions the whole guest path and renders a QR. The guest scans it and is talking within a couple of minutes.

```bash
agentwire guest                          # mint a guest, print URL + QR
agentwire guest --name alex              # named (for the device registry / revocation)
agentwire guest --reach lan|tunnel       # how the guest reaches the portal (see below)
agentwire guest --session guest-alex     # bind to a specific session (else auto-create)
agentwire guest revoke alex              # kill the credential + (optionally) the session
agentwire guest list                     # who's currently paired
```

### What the command provisions (in order)

1. **A guest session.** Auto-create an isolated session (`guest-<name>`) pointed at a scratch project, OR bind to an existing one named by `--session`. This is "their own Claude session". **Capability of that session is a key open decision** (see below) — a guest talking to a `claude-bypass` session is arbitrary RCE-by-voice on the host.
2. **A scoped, per-device credential.** A `ptt`-scoped device credential (from #423/#424), *not* the god-token. Scope = transcribe + send to **one whitelisted session** + receive TTS. No terminal WS, no create/recreate/spawn/fork, no config/safety/scheduler routes. This is what makes putting a credential in a QR acceptable: a leaked guest link = "can talk to one sandbox", not "owns the box".
3. **Portal reachability.** Ensure the portal is up and reachable by the guest device:
   - `--reach lan` (default, zero external deps): flip the bind to `0.0.0.0`, ensure certs exist, ensure a token exists, advertise the host's LAN IP. Guest must be on the same network.
   - `--reach tunnel`: bring up an **ephemeral public URL** (quick Cloudflare tunnel, `trycloudflare.com` — no account, no domain, no DNS) so a remote guest works. Random hostname, torn down on `guest revoke`.
4. **A QR + link** encoding the reach URL **and** the scoped credential (e.g. `https://<host>/mobile#pair=<one-time-pairing-code>`), landing the guest directly on the mobile PTT shell (`/mobile` is already a public bootstrap path in `security._is_public_path`), pre-authed and pre-bound to their session. The pairing code is exchanged once for the device credential so the long-lived secret isn't sitting in QR history forever (ties to #423's pairing flow).

### How the guest reaches the portal

- Scan QR (phone camera) → opens `/mobile` in the default browser.
- One-time: accept the self-signed cert (LAN) — *or* the tunnel serves a real cert and this step vanishes (a real argument for `--reach tunnel` even on-network).
- The page exchanges the pairing code for its device credential, stores it, and binds to the guest session. No token typing.

### How PTT works for them

- The `/mobile` shell already renders a hold-to-talk button.
- **STT must work cross-browser.** The `default` Chrome-only tier is a trap for a guest on Safari. The guest path should **prefer the bundled moonshine/faster-whisper shim** (`agentwire stt start`, works from any browser/device) and have `agentwire guest` ensure it's running — or at minimum *detect* a non-Chrome guest and warn the host. (Decision below.)
- Hold → speak → release → transcript lands in the edit-before-send bar → send to **their** session. Agent replies via TTS (Kokoro default, already cross-OS) → plays on the guest's device. That round trip **is** the first utterance.

---

## Assumed prerequisites

The host has a working agentwire (this is `n=1`, not `n=2` — we're not also onboarding the host). Specifically:

- agentwire installed, `agentwire doctor` green, Claude Code authed.
- Portal can start; Kokoro TTS works (default, no setup).
- For `--reach lan`: host + guest on the same network; host firewall allows `:8765`.
- For `--reach tunnel`: `cloudflared` available (quick tunnels need no account; we can bundle a check + install hint, pairs with `agentwire doctor`).
- A guest device with a browser + mic (any modern phone). **No install on the guest side. Ever.**

---

## Dependencies — and where onboarding can stand alone

This work **overlaps the portal per-device-auth track** and should not reinvent it.

| Need | Belongs to | Onboarding's relationship |
|---|---|---|
| Named, individually-revocable device credentials; pairing-code/QR issuance; device registry under `~/.agentwire/` | **[#423](https://github.com/dotdevdotdev/agentwire-dev/issues/423)** (per-device creds + pairing) | **Hard dependency for the *safe* version.** `agentwire guest` is essentially the first consumer of #423's pairing flow. `guest revoke` = #423 revocation. |
| `ptt` vs `full` capability scopes; middleware route allowlist (no terminal WS, no create/config/safety for `ptt`) | **[#424](https://github.com/dotdevdotdev/agentwire-dev/issues/424)** (capability scopes) | **Hard dependency for *handing a stranger a link*.** Without `ptt` scope, the guest credential is the god-token = voice-RCE on the host. This is the line between "demo to a friend" and "negligence". |

### What onboarding owns regardless (stands alone on top of #423/#424)
- The `agentwire guest` command + UX (one command → QR).
- Session auto-provisioning + binding the guest to *their* session.
- Reachability orchestration (`--reach lan|tunnel`, ephemeral tunnel lifecycle, cert/bind/firewall handling).
- The `/mobile` deep-link landing (pre-auth + pre-bind, skip the desktop UI).
- Guest-appropriate STT defaulting (moonshine over Chrome-only).
- The end-to-end 10-minute happy path + the adversarial timing test.

### Can a useful MVP ship *before* #423/#424 land?
Yes, with an explicit, loud trust boundary — **for trusted guests on the same LAN only**:

- **MVP-now (LAN, trusted guest):** `agentwire guest` automates bind-flip + certs + token + session-create + QR, but issues the **existing shared token** scoped to a guest session by convention. It removes the *friction* but **not** the security gap. It must print a blunt warning ("this guest can reach a shell on this machine — only for people you'd hand your laptop to") and is fine for the immediate `n=0→1` validation with a *trusted* friend in the same room.
- **MVP-safe (the real product):** the same UX, but the credential is a real `ptt`-scoped per-device token (#423 + #424). This is the version you can hand to a stranger or send over a tunnel.

Recommendation: **build the UX now against a credential seam, ship MVP-now to unblock validation immediately, and swap in #423/#424 underneath without changing the guest's experience.** The command shape shouldn't change when the scoping lands.

---

## Open product decisions

These need an owner call before/at implementation:

1. **Guest session capability — the big one.** A guest talking to `claude-bypass` = arbitrary code execution on the host by voice. Options, roughly safest-first:
   - **(a) Sandboxed/throwaway session** in a container or a locked scratch dir with damage-control on full restriction. Safest; most work.
   - **(b) `claude-auto`** (classifier-gated) pointed at a scratch project. Medium safety, ships on existing primitives.
   - **(c) `claude-bypass` + a stern warning.** Only defensible for a fully-trusted guest. *Recommend (b) as the default, (a) as the eventual target, (c) never silent.*
2. **Reach default: LAN vs tunnel.** LAN = zero deps but same-network + cert-warning friction. Quick tunnel = real cert (no warning), works remotely, but adds a `cloudflared` dependency and a random public URL (mitigated by the `ptt` scope + short TTL). *Recommend LAN default, `--reach tunnel` one flag away; revisit once tunnel UX is proven.*
3. **STT for guests.** Force-enable the moonshine shim for guests (works everywhere, ~costs a model download + a running server), or keep Chrome-default and *detect + warn* on non-Chrome guests? *Recommend: detect the guest UA; if non-Chrome, ensure/►start moonshine; else Chrome default is fine.* Silent PTT-does-nothing is the worst outcome.
4. **Credential TTL + revocation UX.** Do guest links expire (e.g. 24h)? Does `guest revoke` also kill the session and tear down the tunnel? *Recommend: default 24h TTL, `revoke` kills credential + tunnel, prompts before killing the session.* (Lands naturally on #423's registry.)
5. **What the guest sees first.** A bare PTT button, or a one-line "hold to talk, then speak" coachmark + a sample prompt to remove the blank-page freeze? The 10-minute clock includes *the guest figuring out what to say.* *Recommend a minimal first-run coachmark on `/mobile`.*
6. **Pairing secret hygiene.** One-time pairing code exchanged for the device credential (#423) vs. the credential embedded directly in the QR. *Recommend the one-time-code exchange* so the durable secret never lives in QR/scrollback/screenshot history.
7. **`doctor` integration.** `agentwire guest` should fail loud and early on missing pieces (firewall, certs, cloudflared, STT). The issue explicitly pairs this with `agentwire doctor`. *Recommend a `doctor --guest` preflight that the command runs first.*

---

## Phased implementation plan

### Phase 0 — Recon / decisions (this doc)
Land the design; get owner calls on the open decisions (esp. #1 guest capability and the MVP-now vs MVP-safe sequencing).

### Phase 1 — MVP happy path (LAN, trusted guest) — unblocks `n=0→1` now
- `agentwire guest` command: preflight (`doctor --guest`) → ensure bind=`0.0.0.0` + certs + token → auto-create a `claude-auto` guest session → render URL + QR (terminal QR via a tiny dep, e.g. `qrcode`/`segno`) to `/mobile#…`.
- `/mobile` deep-link: read the fragment, store creds, auto-bind to the guest session, show the PTT coachmark.
- Guest STT: detect non-Chrome UA → ensure moonshine running.
- `agentwire guest list` / `revoke` (revoke = today's blunt removal; refined in Phase 3).
- **Ships the validation test**: hand a trusted friend the QR, time them. This is the unlock the issue is about — done here even before the security hardening.
- *Caveat printed loudly:* credential is still effectively shared-token-grade until Phase 3.

### Phase 2 — Remote reach
- `--reach tunnel`: ephemeral quick-tunnel lifecycle (up on `guest`, down on `revoke`), real cert kills the warning, works off-LAN.
- `doctor --guest` learns the tunnel checks.

### Phase 3 — Security hardening (consume #423 + #424)
- Swap the credential seam from shared-token to a real **`ptt`-scoped per-device credential** (#424) issued via the **pairing-code flow + device registry** (#423). Guest UX unchanged; the warning from Phase 1 goes away.
- `guest revoke` becomes true per-device revocation; `guest list` reads the device registry; actions are attributable.
- One-time pairing code instead of embedded credential.

### Phase 4 — Polish
- First-run coachmark + sample prompt on `/mobile`.
- Guest capability hardening: move default from `claude-auto` toward a real sandbox (decision #1a).
- TTL/expiry on guest links; "guest is talking" presence indicator for the host; optional host approval on first connect.
- Multi-guest (n>1) ergonomics.

---

## Verification (the only test that matters)

Per the issue: **hand a fresh device + the QR to someone who isn't the builder, don't help, and time them to first successful utterance. Target < 10 min.** Phase 1 must pass this with a trusted in-room guest; Phase 2/3 extend "trusted + in-room" to "anyone, anywhere, safely".
