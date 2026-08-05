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

**Prefer `--upgrade-package <name>` over a blanket `uv lock --upgrade`, and
measure the diff.** "Targeted" describes the *request*, not the result: a single
`--upgrade-package mcp` moved **32 packages** in #900, because the new `mcp`
needed `pydantic 2.13`, which unblocked the `tts` extra's whole `torch`/CUDA
chain in uv's universal resolution. Diff the lock before and after — if a
one-package request produced a thirty-package answer, that is a separate
decision, not a detail:

```bash
cp uv.lock /tmp/uv.lock.before
uv lock --upgrade-package <name>
diff <(grep -A1 '^name =' /tmp/uv.lock.before) <(grep -A1 '^name =' uv.lock)
```

Pinning the request to the **minimum fixed version**
(`--upgrade-package 'mcp==1.27.2'`) is often what keeps it genuinely targeted.

## A red audit must not render as green (#900)

The job is `continue-on-error: true` on purpose — see above. The **side effect**
was not intended: a failing audit became indistinguishable from a passing one at
the workflow level, so nobody saw it unless they opened the job. Eight
advisories accumulated that way, and were found only because a PR's check list
happened to be in front of someone.

Non-blocking and impossible-to-miss pull against each other, so the reporting is
split across three surfaces (`scripts/pip_audit_report.py`, unit-tested in
`tests/unit/test_pip_audit_report.py`):

| Surface | Where it shows | What it's for |
|---------|----------------|---------------|
| `::warning::` annotations | Run page + PR checks view, **without opening the job** | Immediate, zero conversation noise |
| Step summary table | Run page, count in the heading | "Is it clean?" at a glance |
| One tracking issue | Repo issues, opened/updated by the weekly cron | The durable one — annotations belong to a run nobody revisits |

Deliberately **not** a per-PR comment: most findings aren't caused by the PR
they appear on (#900's eight were live on `main` and surfaced on a PR that
changed zero dependency inputs), so commenting on unrelated PRs trains people to
scroll past the signal.

Properties the tests pin, because each is easy to break silently:

- **The reporter runs `if: always()`.** A crashed audit reports "UNKNOWN, which
  is not the same as clean" rather than nothing — conflating those two IS #900.
- **It always exits 0** unless `--exit-code` is passed, so adding visibility can
  never quietly convert the advisory job into a merge gate.
- **Packages *audited* are counted, not just findings.** Zero advisories over
  zero packages is not a clean bill of health: an empty `requirements.txt` (a
  silently-failed export) makes pip-audit exit 0 with `{"dependencies": []}`,
  and a report keyed only on findings calls that clean — the same conflation
  one level in. `--min-packages` sets a plausible floor (50 runtime, 100 with
  extras; the runtime export is ~124 today), below which coverage is reported
  UNKNOWN. Findings are still shown when coverage is doubted — an incomplete
  audit that also hides what it *did* find is the worst of both.
- **The tracking issue is searched with `--state all` and reopened**, not
  recreated. `--state open` cannot see the issue the workflow closed last week,
  so a recurrence would file a second one and "one issue, reused" would quietly
  become "one per close/recur cycle".
- **The issue body only promises what the workflow does.** A test ties the two:
  if the body says it reopens itself, the step must call `gh issue reopen`.
  Operator-facing text describing a mechanism the code doesn't implement is its
  own defect class — worse than silence, because the next reader trusts it.

### The reachability rationale is pinned by tests

`PYSEC-2026-3483` is ignored because agentwire never serves MCP over WebSocket.
An ignore justified by "we don't use that path" is only as strong as the path
staying unused, so `tests/unit/test_pip_audit_report.py` asserts the premise:
`mcp_server.py` runs `transport="stdio"`, and nothing under `agentwire/` imports
`mcp.server.websocket`. If either changes, the tests fail and name the ignore.
A rationale that has quietly become false is worse than none.

## Residual CVEs (ignored in the PR audit)

### `mcp` — `PYSEC-2026-3483`

| ID | Fix version |
|----|-------------|
| `PYSEC-2026-3483` | mcp >=1.28.1 |

**What it is:** the deprecated WebSocket *server* transport
(`mcp.server.websocket.websocket_server`) accepts the handshake without `Host` /
`Origin` validation.

**Why it's acceptable:** agentwire runs its MCP server on **stdio**
(`mcp_server.py`: `mcp.run(transport="stdio")`) and never imports that module —
same reachability argument as the starlette entries below.

**Why not bumped:** `mcp 1.27.2` clears the other two advisories
(`PYSEC-2026-3481`, `PYSEC-2026-3482`) as a **one-package** lock change.
`1.28.1+` requires `pydantic 2.13`, which unblocks the `tts` extra's universal
resolution and cascades **32 packages** — including `torch` 2.6 → 2.13 and the
entire CUDA stack. That is a poor trade against an advisory in a transport we
don't use.

**Revisit when:** the `torch` bump is wanted on its own merits, or `mcp` ships a
fix on the 1.27.x line. Then drop the `--ignore-vuln` and
`uv lock --upgrade-package mcp`. Keep the `mcp<2` bound either way — SDK 2.x
removed `mcp.server.fastmcp`, which `mcp_core.py` imports (#874).

### `starlette` — transitive via `mcp`

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
