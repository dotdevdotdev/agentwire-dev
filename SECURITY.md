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
