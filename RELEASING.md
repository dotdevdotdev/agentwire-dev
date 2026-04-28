# Releasing agentwire-dev

This project publishes the CLI and web app to PyPI as `agentwire-dev`. Packaging uses Hatchling via `pyproject.toml`.

Every release does **both** a PyPI publish and a GitHub release.

## Steps

### 1. Bump version

Edit `agentwire/__init__.py` and update `__version__`. Add an entry to `CHANGELOG.md`.

### 2. Commit and push

```bash
git add agentwire/__init__.py CHANGELOG.md
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

The `PYPI_TOKEN` lives in `~/.agentwire/.env`. Pass it explicitly:

```bash
source ~/.agentwire/.env && uv publish --token "$PYPI_TOKEN" dist/agentwire_dev-{VERSION}*
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
