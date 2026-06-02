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

AgentWire includes built-in security features:

- **Damage Control Hooks:** Block 300+ dangerous command patterns
- **Path Protection:** Prevent access to sensitive files (.env, SSH keys, credentials)
- **Audit Logging:** All blocked operations are logged

See `docs/wiki/internals/damage-control.md` for details.

## Trust Model

The portal (HTTPS server bound to `0.0.0.0:8765` by default) **has no built-in authentication or authorization on its API endpoints**. This is by design — agentwire is built for a local-network trust perimeter, typically running on the same machine as the operator's browser or behind a Cloudflare Tunnel + Zero Trust gate (see `docs/wiki/deployment/remote-access.md`).

What this means in practice:

- **Anyone who can reach the portal port can drive it.** All `/api/*` endpoints (scheduler control, missions dispatch, project deletion, artifact upload, desktop window control) execute without auth.
- **Do not expose the portal directly to the public internet.** Use either firewall rules limiting access to trusted IPs, or front it with an auth gateway. Cloudflare Tunnel + Zero Trust is the recommended pattern; details in the deployment docs.
- **Project deletion via `/api/projects/delete`** validates the path is absolute, contains no `..`, contains no shell metacharacters, and is not in a protected list. Local execution uses argv form (no shell); SSH execution uses `shlex.quote` per argument. These mitigations don't substitute for perimeter security — they reduce blast radius if the perimeter fails.
- **CSRF / Origin checks are not enforced** on state-changing POSTs. A browser inside the trust perimeter that loads attacker-controlled content could be coerced into making requests to the portal. If your portal is reachable from a browser on a less-trusted network, add an origin check or fence it behind an auth proxy.

If you need authentication, the recommended path is **Cloudflare Tunnel + Zero Trust** rather than adding auth in-process: identity, MFA, audit, and revocation are all handled upstream and survive process restarts.
