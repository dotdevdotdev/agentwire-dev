# Releasing agentwire-dev

This project publishes the CLI and web app to PyPI as `agentwire-dev`. Packaging uses Hatchling via `pyproject.toml`.

Every release does **both** a PyPI publish and a GitHub release. The **GitHub release is the changelog** — there is no `CHANGELOG.md`; release notes are built from the commits since the last tag (step 5).

## The engine is a separate project (single source)

The damage-control engine has exactly one source: the `damage-control-dev` repo (<https://github.com/dotdevdotdev/damage-control-dev>), published on PyPI. `agentwire-dev` only *consumes* it through the range in `pyproject.toml` (currently `damage-control-dev>=0.1.5,<0.2`); a checkout and a PyPI install both resolve it from PyPI. There is no copy of the engine in this repo and nothing to release from here. `tests/unit/test_damage_control_pin.py` checks that the declared floor/ceiling parse and that the installed engine satisfies them.

To ship an engine change:

1. **Land it in the damage-control-dev repo**, tag and release it there (its own release flow). Do not tag or release from this repo.
2. **Raise the floor here** in a normal agentwire PR: bump the `>=` in `pyproject.toml` (and the `<` ceiling too if the engine crossed it, e.g. `>=0.2.0,<0.3`), run `uv lock`, and re-run `scripts/golden_snapshot.py check` and `scripts/effective_rules.py --check` — re-record the golden only if the behavior change is intended, in that same PR.
3. **Verify against the published wheel** before releasing agentwire: in a scratch venv, install the agentwire wheel (it pulls the engine from PyPI), then run the fast suite, `agentwire doctor` and a hook subprocess smoke under a temp `HOME`.
4. **Then** bump and publish `agentwire-dev` (steps below), then the GitHub release.

An `agentwire-dev` whose floor names an engine version PyPI doesn't have yet is uninstallable, so step 1 always completes (published) before step 2 merges.

## Steps

### 1. Bump version

Edit `agentwire/__init__.py` and update `__version__`.

### 2. Commit and push

```bash
git add agentwire/__init__.py
git commit -m "chore: bump version to {VERSION}"
git push
```

### 3. Build artifacts

```bash
uv build
```

Produces `dist/agentwire_dev-{VERSION}-py3-none-any.whl` and `dist/agentwire_dev-{VERSION}.tar.gz`.

Optional sanity check — confirm `agentwire/templates/` and `agentwire/static/` are bundled:

```bash
unzip -l dist/agentwire_dev-{VERSION}-py3-none-any.whl | grep -E "templates/|static/"
```

### 4. Publish to PyPI

The `PYPI_TOKEN` lives in `~/.agentwire/.env`. Don't `source` the file — it can
contain unquoted `&` in other values (e.g. a URL with query params), which makes
the shell hit a parse error before `uv publish` ever runs. Grep the token out
directly:

```bash
uv publish --token "$(grep '^PYPI_TOKEN=' ~/.agentwire/.env | cut -d= -f2-)" dist/agentwire_dev-{VERSION}*
```

### 5. Create GitHub release

`gh release create` auto-creates the git tag — no separate `git tag` call needed. Build the changelog from commits since the last release:

```bash
git log --oneline v{LAST_VERSION}..HEAD
```

```bash
gh release create v{VERSION} --title "v{VERSION}" --notes "## Highlights
- ...

## New Features
- ...

## Fixes
- ...

Built by [dotdev.dev](https://dotdev.dev)"
```

## Notes

- Package name: `agentwire-dev` (import package is `agentwire`).
- Python: >=3.10 as declared in `pyproject.toml`.
- Build backend: Hatchling; no `setup.py` required.
- TestPyPI is available via `--publish-url https://test.pypi.org/legacy/` if you want to validate before a real publish.
