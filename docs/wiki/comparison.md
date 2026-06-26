# How AgentWire compares

> Repositioning proposal (issue #481) — draft copy for the owner to review and revise,
> not final marketing. Frames AgentWire against the now-commoditized "talk to your
> agents from anywhere" field by leading with the un-commoditized moat.

Remote access to a coding agent is no longer a differentiator. Anthropic ships
first-party **Remote Control** (`/rc` → QR → live session in the Claude mobile app,
push notifications, E2E-encrypted), and there's a crowded free/freemium field doing
the same thing — Omnara, Orca, Happy, AgentsRoom, Nimbalyst, and others, several using
near-verbatim "talk to your AI agents from anywhere" copy.

So AgentWire shouldn't lead with "from anywhere." It should lead with what those tools
**structurally can't match**:

1. **Self-hosted, keys-local.** Runs entirely on your hardware. No cloud account, no
   third party in the path, no telemetry. Your code and API keys never leave the machine.
2. **Voice-native with cloning on CPU.** Push-to-talk in, neural TTS out, and voice
   cloning that runs without a GPU — your agents talk back in a voice you chose or cloned.
3. **Multi-agent orchestration.** Council (multi-soul deliberation), an overnight
   scheduler, briefing mode, and worktree-parallel workers — tmux- and Claude-Code-native.

## Comparison table

| | **AgentWire** | **Omnara** | **Anthropic Remote Control** | **raw SSH + tmux** |
|---|:---:|:---:|:---:|:---:|
| Self-hosted (your hardware) | ✅ | ❌ hosted | ❌ via Claude app | ✅ |
| Cloud account required | ❌ none | ✅ | ✅ Anthropic | ❌ none |
| Keys / code stay local | ✅ | ❌ | ❌ | ✅ |
| Neural voice + cloning | ✅ CPU clone | ❌ | ❌ | ❌ |
| Voice push-to-talk | ✅ | ❌ | ❌ | ❌ |
| tmux / Claude-Code-native | ✅ | partial | ✅ (Claude only) | ✅ |
| Multi-agent orchestration | ✅ council, scheduler, workers | partial | ❌ one session | manual |
| Zero-setup remote access | ⚠️ certs + tunnel | ✅ | ✅ QR scan | ⚠️ |
| Cloud session persistence | ❌ | ✅ | ✅ | ❌ |
| License / price | AGPL-3 / commercial | freemium SaaS | bundled w/ Claude | free |

> Competitor details (Remote Control ship specifics, star counts, exact feature sets)
> are summarized from public sources and should be re-verified before publishing. The
> goal is a fair frame, not a takedown.

## Where the alternatives genuinely win

- **Anthropic Remote Control** — zero-setup. Scan a QR code and you're live, E2E-encrypted,
  with push notifications and nothing to install or keep running. If you just want to watch
  and nudge one Claude session from your phone, it's the lowest-friction option.
- **Omnara** — cloud session persistence and a polished native mobile app. No home server
  to maintain; your sessions survive your laptop sleeping.
- **raw SSH + tmux** — free, universal, nothing to trust but yourself. AgentWire is a layer
  on top of exactly this for people who want voice and orchestration without hand-rolling it.

## Where AgentWire wins

Pick AgentWire when:

- your code and keys must **never leave your machine** (privacy, compliance, or principle);
- you want to **drive a fleet of agents by voice**, not babysit a single session;
- you want them to **answer in a neural voice you cloned**, running on your own CPU;
- you live in **tmux + Claude Code** and want orchestration native to that, not a wrapper.

The one-liner: **use the official app to watch one session; use AgentWire to orchestrate
many, by voice, on your own hardware.**

## Proposed homepage hero (agentwire.dev)

Ready-to-port copy for the website repo (kept here because the site lives in a separate
repo). Owner to lift and refine.

- **Headline:** Self-hosted, voice-native control for Claude Code.
- **Subhead:** Your machine, your keys, no cloud account. Orchestrate many agents by
  voice — and hear them answer in a voice you cloned, running on your own CPU.
- **Positioning line (above or below CTA):** Remote access is table stakes now. AgentWire
  is the one that never phones home. *Use the official app to watch one session; use
  AgentWire to orchestrate many, on hardware you own.*
- **Three pillars (feature row):**
  - 🔒 **Local-first** — runs on your hardware, keys stay local, zero telemetry.
  - 🎙️ **Voice-native** — push-to-talk in, cloned neural voice out, CPU-only.
  - 🧠 **Orchestration** — council, scheduler, briefing mode, parallel worktree workers.

Sources: <https://github.com/omnara-ai/omnara>, <https://www.ycombinator.com/companies/omnara>,
<https://www.star-history.com/blog/playbook-for-more-github-stars/>.
