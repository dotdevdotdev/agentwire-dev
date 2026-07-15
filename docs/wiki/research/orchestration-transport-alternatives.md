# Orchestration transport alternatives: do we need a non-SSH transport?

> Research note for [#297](https://github.com/dotdevdotdev/agentwire-dev/issues/297). Investigates the "we don't use SSH — we use something faster/more secure" pitch from competing agent-orchestration tools, separates real engineering from marketing, and recommends what (if anything) AgentWire should adopt. **This is a recon report, not a transport rewrite.**
>
> **Update (shipped):** both cheap wins below are now implemented, in the same PR (#300/#303). Cheap win #1 — `agentwire/ssh.py::ssh_base_opts()` supplies `ControlMaster`/`ControlPersist` multiplexing and is wired into every remote call site (`agents/tmux.py`, `tunnels.py`, `server.py`, `projects.py`). Cheap win #2 — the Tailscale mesh underlay is documented and supported; see [Tailscale Mesh Underlay](../deployment/remote-machines.md#tailscale-mesh-underlay-no-inbound-port-22). The present-tense "no `ControlMaster` anywhere / fresh process per command" baseline below describes the pre-#297 state and is kept for historical context.

## TL;DR — Recommendation (b): cheap hardening, no transport change

SSH is **not** a real limitation for AgentWire at our scale (personal/small-team, a handful of machines on a LAN plus a few remote boxes). The competitor "no SSH" marketing is mostly about a problem we already solved a different way (mobile push + structured agent events → our portal/WebSocket layer), not about raw transport speed or security. Two cheap wins close the only gaps that are real:

1. **Enable SSH `ControlMaster`/`ControlPersist` multiplexing** in our own `ssh` invocations (a few `-o` flags). Measured locally: this cuts per-command handshake from **~90 ms to ~10 ms even on loopback with zero network latency** — and the gap widens to hundreds of ms over a real network. No transport change, no new code paths.
2. **Document running our existing SSH *over* a Tailscale/WireGuard mesh.** This buys the entire marketed "no inbound port / identity-based / NAT-traversal" security story while changing **zero application code** — the answer to "more secure than SSH?" is "keep SSH, change the network underneath."

A persistent per-machine daemon (gRPC/QUIC/WebSocket) is **not** worth it for us: it's a new service to deploy, secure, and version on every machine, and it fights our "CLI is the single source of truth, everything else is thin" architecture. Revisit only if we ever go multi-tenant cloud.

---

## 1. What we do today (the baseline cost surface)

AgentWire is **all SSH** for cross-machine work — a fresh `ssh user@host "<cmd>"` process per command, no connection reuse:

| Concern | Where | Mechanism |
|---|---|---|
| Remote command exec | `agentwire/agents/tmux.py:135` (`_run_remote`) | `ssh -o BatchMode=yes -o ConnectTimeout=5 user@host <cmd>` — **fresh process per command** |
| Other remote exec | `server.py:73,574,632,689,863`, `projects.py:80` | same per-command `ssh` pattern |
| Port forwarding | `tunnels.py:104` (`create_tunnel`) | `ssh -L local:localhost:remote -N -f` background tunnel, PID-tracked via pgrep |
| Connectivity check | `tunnels.py:308` (`test_ssh_connectivity`) | `ssh -o BatchMode … echo` |
| Topology | `network.py` (`NetworkContext`) | services → machines → SSH targets → tunnels; registry in `~/.agentwire/machines.json` |

**No `ControlMaster`/`ControlPath`/`ControlPersist` is set anywhere in the code.** The docs *recommend* it as a `~/.ssh/config` tip ([`remote-machines.md:139`](../deployment/remote-machines.md), [`troubleshooting.md:320`](../internals/troubleshooting.md)), and because `ssh(1)` reads `~/.ssh/config` by default that tip *would* apply to all our subprocess calls — but it's opt-in and off by default, so out of the box every remote op pays a full handshake.

### Measured handshake cost (this machine, loopback — zero network RTT)

```
Fresh ssh per command:        ~90 ms each   (cold first call ~120–250 ms)
ControlMaster socket reuse:   ~10 ms each   (after the master is established)
```

That ~90 ms is **pure process spawn + key exchange + auth overhead with no network latency at all**. On a real LAN add the round-trips (TCP + SSH KEX + auth ≈ several RTTs); over a VPN/bastion at ~100 ms RTT a cold handshake is commonly **300–500 ms** ([nixCraft](https://www.cyberciti.biz/faq/linux-unix-reuse-openssh-connection/), [howtouselinux](https://www.howtouselinux.com/post/stop-waiting-eliminate-ssh-latency-with-connection-multiplexing)). A multiplexed reuse is ~1 RTT regardless. So our real cost surface is exactly three things: **(a) per-command handshake latency, (b) key distribution/management, (c) inbound SSH port exposure per machine.**

---

## 2. The landscape — what competitors actually use

| System / tool | Transport | What they market | Reality |
|---|---|---|---|
| **Claude Code Agent Teams** (Anthropic's own) | In-process / tmux / iTerm2 panes, **same host only** | "Parallel agents" | No cross-machine story at all today. The baseline of "what Anthropic ships" is *local*. Not a transport competitor. |
| **Conductor, Vibe Kanban, Claude Squad, Crystal** | **Local** git worktrees on your Mac; no remote transport | "Run N agents in parallel" | Same-host orchestration. They never cross a machine boundary, so "SSH vs not" doesn't apply. ([Augment](https://www.augmentcode.com/tools/open-source-agent-orchestrators), [rustman](https://rustman.org/wiki/conductor-parallel-agents/)) |
| **Sculptor** | Local **containers** | Isolation | Docker, not a network transport. |
| **Claude Code Web / Cursor Background Agents / GitHub Copilot Coding Agent / Devin / Factory** | Agents run in **provider cloud VMs/sandboxes**; you talk to a **web control plane** (Slack/Linear/GitHub/web) | "No local setup, no SSH" | True — but only because *they own both ends*. The agent is in their cloud; you never address a machine you own. This is a hosting model, not a better transport for *your* machines. ([MarkTechPost](https://www.marktechpost.com/2026/06/10/ai-coding-agents-development-platforms-2026/), [Ry Walker](https://rywalker.com/research/cloud-coding-agent-platforms)) |
| **rivet-dev/sandbox-agent** | **HTTP + SSE daemon inside the sandbox** ("a server that runs inside your sandbox; your app connects remotely to control Claude Code/Codex/…") | "Designed for remote control from the start" | Real engineering, but aimed at *controlling agents in cloud sandboxes*, not orchestrating your own boxes. ([GitHub](https://github.com/rivet-dev/sandbox-agent)) |
| **Warp** | Worker process **dials home to "Oz" via WebSocket**; agents in Docker | Cloud agent management | Classic outbound-only control plane. Worker connects out; no inbound port. ([Warp docs](https://docs.warp.dev/enterprise/enterprise-features/architecture-and-deployment/)) |
| **"Beyond SSH" / Claude Remote (tacticremote)** | **WebSocket + server layer** | "Beyond SSH" for AI coding | The headline "we don't use SSH" piece. On close read it makes **no speed or security claim** — see §3. ([tacticremote](https://tacticremote.com/blog/2026-02-28-beyond-ssh-websocket-for-ai-coding/)) |
| **MCP over gRPC** (Google) | gRPC/HTTP/2, protobuf instead of JSON-RPC | "Enterprise-grade, bidirectional streaming, faster than JSON" | Real and faster *for the MCP message layer*, at fleet/datacenter scale. Orthogonal to how you reach a machine. ([Google Cloud](https://cloud.google.com/blog/products/networking/grpc-as-a-native-transport-for-mcp/)) |
| **NATS / Redis streams / MQTT bus** | Pub/sub message bus, agents subscribe to subjects | "Location-transparent fan-out, sub-ms" | Real at scale (many agents, many nodes). Adds a broker to deploy + secure. Overkill for a handful of machines. ([NATS](https://nats.io/)) |
| **Tailscale / WireGuard mesh** | WireGuard overlay; **SSH (or anything) runs over it** | "No inbound ports, identity-based, NAT traversal" | The strongest *real* security story — and it's an **underlay**, not a replacement transport. You keep SSH. ([Tailscale](https://tailscale.com/blog/tailscale-ssh), [pvi.sh](https://pvi.sh/blog/self-hosting-with-tailscale)) |
| **mTLS / SPIFFE/SPIRE** | Short-lived X.509 SVIDs, auto-rotated (1h) | "Kills long-lived keys, shrinks blast radius" | Real and genuinely better than static keys — but it's a per-workload identity fabric for fleets, with a SPIRE agent to run everywhere. Enterprise-shaped. ([Teleport](https://goteleport.com/learn/what-is-mtls/), [SPIFFE](https://spiffe.io/docs/latest/spire-about/use-cases/)) |

**Pattern across the whole field:** nobody who orchestrates *your own machines* has a magic faster-than-SSH transport. The "no SSH" players either (a) **own both ends in their cloud** (hosting model, not transport), or (b) put a **WebSocket/HTTP daemon** in front to get *mobile push + structured agent events* (a UX layer, not raw speed), or (c) move the security story to a **mesh/identity underlay that SSH happily rides on**.

---

## 3. Claims vs reality

Each marketing claim, labeled **substantiated / partial / hype**, judged against *our* threat model and scale.

### "Faster — no per-command handshake"

- **The honest version is `partial`, and the fix isn't a new transport.** Per-command SSH handshake cost is real (§1: ~90 ms on loopback, 300–500 ms over a VPN). But it's caused by *opening a fresh connection every time*, not by SSH-the-protocol. **`ControlMaster` multiplexing eliminates it** (measured: 90 ms → 10 ms) — same transport, a few flags. A WebSocket/gRPC daemon would also eliminate it, by paying a much higher fixed cost (a service per machine).
- The loudest "Beyond SSH" article (`tacticremote`) **makes no quantitative benchmark and explicitly does not claim WebSocket is faster** — it frames the choice as "trading SSH's flexibility for ease of setup." So the *speed* framing is largely **hype** when stated as a transport win; the underlying latency is `substantiated` but **closed by multiplexing**, not by abandoning SSH.

| Claim | Verdict |
|---|---|
| "SSH is slow per command" | **substantiated** (handshake is real) — but **fully mitigated by ControlMaster**, no transport change |
| "Our WebSocket/gRPC transport is *faster* than SSH" | **hype** for our scale — the daemon's win is push/streaming UX, not raw latency; multiplexed SSH matches the latency for free |
| "gRPC/protobuf beats JSON-RPC" | **substantiated** but **irrelevant to us** — that's the MCP message layer at datacenter scale, not how you reach a machine |

### "More secure — no inbound ports / no key management"

- **`no inbound ports` is `substantiated` and genuinely valuable** — but it's a property of a **mesh/outbound-tunnel underlay**, not of ditching SSH. Run SSH over Tailscale and you *also* have no public inbound port. (§4.)
- **`no SSH keys to manage` is `substantiated`** for Tailscale SSH / SPIFFE — device/SSO identity and short-lived certs replace static keys, and revocation is instant ([Tailscale](https://tailscale.com/blog/tailscale-ssh), [Teleport](https://goteleport.com/learn/what-is-mtls/)). For **us**, key management is a handful of `authorized_keys` entries we control — low pain, so the benefit is real but small.
- **Is SSH-with-keys actually our weak point?** No. Our threat model is personal/small-team machines, mostly on a LAN, a few remote. OpenSSH with key-only auth, `BatchMode`, and a non-default-or-firewalled port is a hardened, audited, 25-year-old transport. The honest risk isn't "SSH is insecure," it's "each box has a port open to whatever network it's on" — which the mesh underlay removes.

| Claim | Verdict |
|---|---|
| "No inbound ports is more secure" | **substantiated** — and we get it by putting SSH *on a mesh*, not by replacing SSH |
| "Identity/SSO/short-lived certs beat long-lived SSH keys" | **substantiated** in general (blast-radius, instant revoke) — **partial** value for us (few keys, all ours) |
| "SSH itself is a security liability you must escape" | **hype** at our scale — key-only OpenSSH is fine; the only real exposure is the open port, fixed by the underlay |

---

## 4. The two cheap wins, evaluated

### Cheap win #1 — SSH `ControlMaster` multiplexing (closes the entire "faster" gap)

**What:** add connection-reuse flags so the first `ssh` to a host opens a master socket and every subsequent command rides it instead of re-handshaking.

```bash
ssh -o ControlMaster=auto \
    -o ControlPath=~/.ssh/sockets/%r@%h-%p \
    -o ControlPersist=600 \
    user@host <cmd>
```

**Evidence (measured on this machine, loopback, zero network latency):**

```
Fresh handshake per command:   ~90 ms
Multiplexed reuse:             ~10 ms     →  ~9× faster, and that's with NO network RTT
```

Over a real LAN/VPN the absolute saving is far larger (a cold handshake is several RTTs; reuse is ~1). For an orchestrator that fires many short remote commands in a burst (status sweeps, topology checks, `tmux` pokes), this is the single highest-leverage change in this whole report.

**Two ways to ship it, both cheap:**
- **Code:** add the three `-o` flags (plus a `~/.ssh/sockets/` dir and a sane `ControlPersist`) to the `ssh_cmd` builders in `agents/tmux.py:135`, `tunnels.py`, `server.py`, `projects.py`. SSOT-friendly: factor a single `ssh_base_opts()` helper so every call site multiplexes identically.
- **Docs/config:** we *already* recommend the `~/.ssh/config` block — promote it from a buried tip to a first-class setup step. Because `ssh(1)` honors `~/.ssh/config`, this multiplexes our existing subprocess calls with **zero code change**.

**Failure modes:** stale sockets after a network blip (mitigated by `ControlPersist` timeout + `ssh -O exit`); a hung master can wedge followers (use `ServerAliveInterval`). Both are well-trodden; Ansible ships ControlMaster on by default for exactly this reason ([oneuptime](https://oneuptime.com/blog/post/2026-02-21-how-to-configure-ansible-ssh-controlmaster-for-persistent-connections/view)).

### Cheap win #2 — run our existing SSH over a Tailscale/WireGuard mesh (gets the "more secure" story, zero app code)

**What:** install Tailscale on each machine; address machines by their `100.x` tailnet IP (or MagicDNS name) in `machines.json`. SSH rides the WireGuard tunnel.

**What it buys, matching the competitor security pitch point-for-point:**
- **No public inbound port** — close `22` to the internet; SSH is only reachable on the tailnet (this is the *real* security win competitors market). ([pvi.sh](https://pvi.sh/blog/self-hosting-with-tailscale))
- **Identity-based ACLs** — tailnet ACLs gate who/what can reach each node, on top of SSH keys. ([Tailscale ACLs](https://anuragbhatia.com/post/2024/04/understanding-headscale-tailscale-acl/))
- **NAT traversal** — remote boxes behind NAT become reachable with no port-forwarding, which we currently can't do at all.
- **Instant revoke** — drop a device from the tailnet and it's cut off everywhere.

**Cost:** install Tailscale per machine (one-time), nothing else. **Application code changes: none** — `network.py`/`tunnels.py`/`tmux.py` keep speaking SSH to a host that now happens to be a tailnet address. Optional later: adopt **Tailscale SSH** to drop SSH keys entirely (device/SSO identity replaces `authorized_keys`), but that's a follow-on, not required.

**This is the direct answer to question #4 in the issue:** *yes* — the win is "keep SSH, change the network underneath." We can even pair it with the existing Cloudflare Tunnel portal setup ([`remote-access.md`](../deployment/remote-access.md)) — mesh for machine-to-machine SSH, tunnel for the public portal.

---

## 5. Persistent per-machine daemon — evaluated, not recommended

**What a long-lived daemon (gRPC/QUIC/WebSocket) would buy:** server-push notifications, native streaming of agent events, sub-handshake command latency, structured "agent is thinking / awaiting approval" semantics (the `rivet-dev/sandbox-agent` and `tacticremote` model).

**Why it's wrong for us right now:**
- **We already have the event/push layer** — the portal's WebSocket + the idle/notification hooks + channels (email/SMS) already deliver "agent needs you" to a human. The daemon's headline benefit is something AgentWire solved at the *portal* tier, not the transport tier. Adding a transport daemon duplicates it.
- **It's a new service on every machine** to deploy, secure (now *it* needs an identity + open port or its own outbound tunnel), version, health-check, and restart. That is precisely the surface SSH lets us avoid — `sshd` is already there, already hardened, already managed by the OS.
- **It fights our architecture.** CLAUDE.md is explicit: *the CLI is the single source of truth; the portal and everything else are thin wrappers that shell out to it.* A stateful long-lived daemon that owns remote execution inverts that. The latency win it offers is the same win `ControlMaster` gives us for ~5 lines of flags.
- **Multiplexed SSH already gives ~10 ms commands.** The daemon's *only* unique remaining advantage is server-initiated push to the machine — which we don't need, because our orchestrator polls/pushes from the *control* side and the portal handles human push.

**When to revisit:** if AgentWire ever becomes a multi-tenant hosted product (agents in *our* cloud, customers who never touch a shell), the cloud-sandbox-daemon model (rivet/Warp/Devin) becomes the right shape. At personal/small-team scale it's pure overhead.

---

## 6. Recommendation

**(b) Cheap hardening — adopt both cheap wins, change no transport.**

1. **Multiplex SSH.** Add `ControlMaster=auto` / `ControlPath` / `ControlPersist=600` to a single shared `ssh_base_opts()` helper used by every `ssh` call site (`tmux.py`, `tunnels.py`, `server.py`, `projects.py`), **and** promote the `~/.ssh/config` ControlMaster block from a tip to a setup step. Measured ~9× handshake reduction, free.
2. **Document the Tailscale-underlay option.** A short setup recipe: install Tailscale per machine, use tailnet addresses in `machines.json`, close public `22`. Gets the entire "no inbound port / identity / NAT-traversal" security story competitors market, with zero application-code change.

**Not (a)** — doing nothing leaves the handshake tax and the open-port exposure on the table when both fixes are nearly free.

**Not (c)** — no new transport, no daemon, no mesh-as-code, no message bus. None of it is justified by our scale or threat model; all of it fights "CLI is SSOT, everything thin." A real transport spike would be cargo-culting enterprise/multi-tenant patterns onto a handful of personal machines.

### Scaled to our reality

We are personal/small-team with a handful of machines, mostly LAN. At that scale: the handshake tax is the only real "speed" issue and `ControlMaster` erases it; the open SSH port is the only real "security" issue and a Tailscale underlay erases it. Everything else in the competitor pitch is solving fleet/cloud/multi-tenant problems we don't have — or solving a mobile-push/event-semantics problem our **portal already solves**.

If we later want to delete SSH keys entirely, **Tailscale SSH** (device/SSO identity, instant revoke) is the natural next step and slots under the same code — but it's an enhancement, not a need. That would be the moment to file a follow-up issue; we are not there yet.

---

## Sources

- SSH multiplexing / handshake latency: [nixCraft](https://www.cyberciti.biz/faq/linux-unix-reuse-openssh-connection/), [howtouselinux](https://www.howtouselinux.com/post/stop-waiting-eliminate-ssh-latency-with-connection-multiplexing), [DEV (ControlMaster)](https://dev.to/mahafuz/multiplexing-ssh-connections-with-control-master-speed-up-deployments-and-automation-26mh), [Ansible ControlMaster](https://oneuptime.com/blog/post/2026-02-21-how-to-configure-ansible-ssh-controlmaster-for-persistent-connections/view)
- Tailscale / WireGuard mesh: [Tailscale SSH blog](https://tailscale.com/blog/tailscale-ssh), [Tailscale SSH docs](https://tailscale.com/docs/features/tailscale-ssh), [Self-hosting with Tailscale (no open ports)](https://pvi.sh/blog/self-hosting-with-tailscale), [Tailscale/headscale ACLs](https://anuragbhatia.com/post/2024/04/understanding-headscale-tailscale-acl/)
- Outbound-only tunnels / control planes: [Cloudflare Tunnel vs ngrok vs Tailscale](https://dev.to/mechcloud_academy/cloudflare-tunnel-vs-ngrok-vs-tailscale-choosing-the-right-secure-tunneling-solution-4inm), [awesome-tunneling](https://github.com/anderspitman/awesome-tunneling)
- "Beyond SSH" / WebSocket agent control: [tacticremote (the marketing piece)](https://tacticremote.com/blog/2026-02-28-beyond-ssh-websocket-for-ai-coding/), [rivet-dev/sandbox-agent (HTTP+SSE daemon)](https://github.com/rivet-dev/sandbox-agent), [Warp architecture (worker→Oz WebSocket)](https://docs.warp.dev/enterprise/enterprise-features/architecture-and-deployment/)
- Local agent orchestrators (worktree, same-host): [Augment — 9 open-source orchestrators](https://www.augmentcode.com/tools/open-source-agent-orchestrators), [Conductor & the 2026 ecosystem](https://rustman.org/wiki/conductor-parallel-agents/)
- Cloud coding-agent platforms: [MarkTechPost — 2026 platforms](https://www.marktechpost.com/2026/06/10/ai-coding-agents-development-platforms-2026/), [Ry Walker — cloud coding agents](https://rywalker.com/research/cloud-coding-agent-platforms)
- gRPC / message bus: [Google Cloud — gRPC as native MCP transport](https://cloud.google.com/blog/products/networking/grpc-as-a-native-transport-for-mcp/), [NATS](https://nats.io/)
- mTLS / SPIFFE short-lived identity: [Teleport — what is mTLS](https://goteleport.com/learn/what-is-mtls/), [SPIFFE/SPIRE use cases](https://spiffe.io/docs/latest/spire-about/use-cases/)
