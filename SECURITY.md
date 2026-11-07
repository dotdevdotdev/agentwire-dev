# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in AgentWire, please report it privately.

**Do NOT open a public GitHub issue for security vulnerabilities.**

### How to Report

Email: security@agentwire.dev

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested fixes (optional)

### What to Expect

- **Acknowledgment:** Within 48 hours
- **Initial Assessment:** Within 1 week
- **Resolution Timeline:** Depends on severity, typically 30-90 days

### Scope

This security policy applies to:
- The AgentWire CLI (`agentwire` command)
- The AgentWire portal (web interface)
- Official AgentWire packages on PyPI

### Out of Scope

- Third-party dependencies (report to their maintainers)
- Self-hosted TTS/STT servers
- User misconfiguration

## Security Features

AgentWire is not a sandbox and does not promise to stop an agent from running a
destructive command. Agents run with `--dangerously-skip-permissions` by default
(see below), so what stands between an agent and your machine is whatever you
add.

Command and path screening is provided by the separate, optional
[`damage-control-dev`](https://pypi.org/project/damage-control-dev/) project
(PyPI). AgentWire does not manage or ship it as a feature: it depends on that
package, registers its hooks when you run `agentwire hooks install`, and carries
its own rules layer for AgentWire paths. What the hooks match, how they
normalize commands, and their limits are documented in that project. Treat them
as a best-effort screen, not a security boundary.

Contributor notes on the integration: `docs/wiki/internals/damage-control.md`
and `docs/wiki/security/damage-control-hardening.md`.

### Redundant Permissions Bypass (`--dangerously-skip-permissions`)

By default, AgentWire initializes Claude Code with the `--dangerously-skip-permissions` flag, which skips Claude Code's own approval prompts. The compensating control is the damage-control-dev hook layer described above, installed via `agentwire hooks install`. If those hooks are not installed, or the command is outside what they match, nothing else intervenes. Use `posture: prompted` or `posture: auto` if you want an approval step instead.

## Trust Model

The portal enforces two security layers in-process:

1. **Origin validation (always on).** Every state-changing request (POST/PUT/DELETE/PATCH) and WebSocket upgrade with an `Origin` header must match the portal's own origin, a localhost equivalent, or an entry in `server.allowed_origins` (exact `scheme://host[:port]` strings — needed when fronting with Cloudflare Tunnel, where the browser's origin is the tunnel domain). Mismatches get a 403 and a log line. Requests without an Origin header (curl, CLI, scripts) pass — CSRF is a browser vector. This protects loopback-only users from malicious pages firing cross-site requests at `localhost:8765`.

2. **Bearer-token auth (required for non-loopback binds).** The portal binds `127.0.0.1:8765` by default — local only. Binding anything else (`0.0.0.0`, a LAN IP) auto-generates a token at `~/.agentwire/portal.token` (mode 0600) and requires it on every request outside the public bootstrap surface (`GET /`, `/health`, `/static/*`): `Authorization: Bearer <token>` on HTTP, `agentwire.bearer.<token>` WebSocket subprotocol. The portal **refuses to start** on a non-loopback bind with auth explicitly disabled. Print the token with `agentwire portal token`; rotate with `--rotate`. Browsers prompt once per device and store it in localStorage.

Token configuration (`server.auth_token` in `~/.agentwire/config.yaml`): unset = use the token file (auto-generated); any string = explicit override; `""` = auth disabled, allowed only on loopback binds. Tokens are compared constant-time and redacted from the config served to the portal's config editor.

What this means in practice:

- **LAN exposure (`server.host: 0.0.0.0`) is protected by the token.** Someone on your network who can reach the port gets 401s until they present it. Treat the token like a password; rotate it if a device is lost.
- **Do not expose the portal directly to the public internet.** Token auth raises the bar on a trusted LAN; it is not a substitute for identity, MFA, audit, and revocation. For anything internet-facing, front it with **Cloudflare Tunnel + Zero Trust** (see `docs/wiki/deployment/remote-access.md`) and add your tunnel domain to `server.allowed_origins`.
- **Project deletion via `/api/projects/delete`** validates the path is absolute, contains no `..`, contains no shell metacharacters, and is not in a protected list. Local execution uses argv form (no shell); SSH execution uses `shlex.quote` per argument. These mitigations reduce blast radius if the perimeter fails.
- **Self-hosted TTS/STT servers are a separate trust domain** — they have no auth of their own and are out of scope here.
