# pip-audit: dependency CVE triage

> Living document. Update this, don't create new versions.

The `security` CI workflow runs `pip-audit` over the resolved dependency set. It
is **advisory** (non-blocking) by design — the damage-control bypass corpus is
the hard merge gate. This page records how the audit is scoped and why the
remaining CVEs are left in place.

## Scoping

| Trigger | Scope | Filter |
|---------|-------|--------|
| PR / push to `main` | runtime/default deps only (`uv export --no-dev`, no optional extras) | residual-CVE ignore allowlist (below) |
| Weekly cron (Mon 07:00 UTC) | everything, incl. `tts`/`stt` extras (`--all-extras`) | none — full backlog for review |

The PR audit tracks what the **default install actually ships**. The heavy
`torch` / `onnxruntime` / `gradio` chain only arrives with the `tts`/`stt`
extras (GPU machines), so it is left to the weekly cron rather than blocking or
spamming every PR.

## How CVEs are cleared

1. **Direct deps** — bump the floor in `pyproject.toml` to the fixed version
   (e.g. `requests>=2.33.0`, `python-dotenv>=1.2.2`).
2. **Transitive deps** — `uv lock --upgrade-package <name>` pulls the fixed
   version into `uv.lock` without touching `pyproject.toml`.

After any bump, regenerate the lock and run `uv sync` + the test suite. A clean
runtime-scope audit is reproduced locally with:

```bash
uv sync
uv run --with pip-audit pip-audit   # audits the synced (runtime-default) env
```

## Residual CVEs (ignored in the PR audit)

All in **`starlette`**, transitive via `mcp`:

| ID | Fix version |
|----|-------------|
| `PYSEC-2026-161` | starlette >=1.0.1 |
| `CVE-2026-48818` | starlette >=1.1.0 |
| `CVE-2026-48817` | starlette >=1.1.0 |
| `CVE-2026-54283` | starlette >=1.3.1 |
| `CVE-2026-54282` | starlette >=1.3.0 |

**Why not bumped:** the fixes require `starlette >=1.0`, but the `tts` extra
(`gradio` → `chatterbox-tts`) pins `starlette <1.0`, and uv resolves a single
universal version across all extras — so the default install is held at
`starlette 0.50.x`.

**Why it's acceptable:** these are HTTP request-handling CVEs in starlette's
server. agentwire speaks MCP over **stdio** and serves the portal with
**aiohttp**, so starlette's HTTP path is not reachable in normal operation.

**Revisit when:** `mcp` drops its starlette dependency, or `chatterbox-tts` /
`gradio` relax the `<1.0` ceiling. At that point drop the `--ignore-vuln` flags
in `.github/workflows/security.yml` and `uv lock --upgrade-package starlette`.
