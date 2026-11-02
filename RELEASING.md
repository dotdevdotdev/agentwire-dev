# Releasing agentwire-dev

This project publishes the CLI and web app to PyPI as `agentwire-dev`. Packaging uses Hatchling via `pyproject.toml`.

Every release does **both** a PyPI publish and a GitHub release. The **GitHub release is the changelog** — there is no `CHANGELOG.md`; release notes are built from the commits since the last tag (step 5).

## Release order (two packages)

`agentwire-dev` depends on `damage-control-dev` (its own PyPI project, engine source in `packages/damage-control-dev/`). `pyproject.toml` pins it to a range — currently `damage-control-dev>=0.1.2,<0.2` — and `tests/unit/test_damage_control_pin.py` fails if the workspace version leaves that range. A checkout resolves the engine from the workspace; a PyPI install resolves it from PyPI. Order matters, because an `agentwire-dev` that names a version PyPI doesn't have yet is uninstallable:

1. **Engine changed?** Bump `__version__` in `packages/damage-control-dev/src/damage_control/__init__.py`, merge, then build and publish it **first** (commands in step 3). Skip entirely when the engine didn't change — the existing pin keeps resolving.
2. **Pin still admits the engine?** A patch/minor bump inside the range needs no `agentwire-dev` change. A bump outside it (e.g. `0.2.0`) requires widening the range in `pyproject.toml` (`>=0.2.0,<0.3`) in the agentwire PR — the pin test enforces this.
3. **Verify against the published wheel** before releasing agentwire: in a scratch venv, install the agentwire wheel plus `damage-control-dev=={DC_VERSION}` from PyPI (not the workspace), then run `scripts/golden_snapshot.py check`, the fast suite, `agentwire doctor` and a hook subprocess smoke under a temp `HOME`. Diff the PyPI wheel's tree against `uv build --package damage-control-dev --wheel` at HEAD; code and JSON rules must be identical.
4. **Then** bump and publish `agentwire-dev` (steps below), then the GitHub release.

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

When the engine's version is new (see [Release order](#release-order-two-packages)), build and publish `damage-control-dev` **before** agentwire. Its version lives in `packages/damage-control-dev/src/damage_control/__init__.py`:

```bash
uv build --package damage-control-dev     # -> dist/damage_control_dev-{DC_VERSION}*
uv publish --token "$(grep '^PYPI_TOKEN=' ~/.agentwire/.env | cut -d= -f2-)" dist/damage_control_dev-{DC_VERSION}*
```

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
