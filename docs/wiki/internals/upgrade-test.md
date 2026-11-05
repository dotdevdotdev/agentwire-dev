# Upgrade test (#1276)

`tests/integration/test_upgrade_from_release.py` proves an upgrade works on a real install. The 1.75 to 1.78 upgrade shipped three bugs at once (#1268 migrate ran without the AgentWire profile and retired nothing, #1269 it deleted the only copy of a rule file the bundle no longer ships, #1270 doctor's hints named fixes that do nothing); each was invisible to a single-process unit test. This is the "test a SECOND launch" lesson applied to upgrades.

## What it does

1. Fresh venv, `agentwire-dev==$AGENTWIRE_UPGRADE_FROM` from PyPI (default `1.75.0`, the oldest shape `safety migrate` must handle).
2. Under a fresh `HOME`, that release runs its own `agentwire safety install --yes`, so the legacy state is what the release really writes (copied rule and tooldef files, per-kind hook scripts, matcher registrations), not a guess.
3. Seeded on top: a pristine copy of `gws.yaml` (shipped once, unbundled since #1143; fixture from git history) and two session records with a conversation chain whose newest transcript exists.
4. `uv pip install` the checkout over it, then fresh-process: `safety migrate --dry-run` (must plan and write nothing), `doctor`, the real migrate, `safety install --refresh --yes`, `doctor` again, and a resolve of every recorded session.

## What it asserts

- dry run lists the pristine copies, keeps the unbundled `gws.yaml`, and changes no file;
- after migrate only `gws.yaml` survives in the rule overlay, legacy scripts and registrations are gone;
- nothing is written outside `.agentwire`/`.claude` (and tool caches) in the fresh HOME, and the real `~/.damage-control` is unchanged;
- every recorded session still resolves (the newest link of each chain is resumable);
- before migrate, doctor names the legacy state and the `safety migrate` fix; after, its `damage control` and `orphaned conversation history` sections have no `[!!]`. Other sections (portal, mic) depend on the machine and are deliberately not asserted.

## Running it

It is marked `slow` and `upgrade`, needs PyPI, and every agentwire process runs with `HOME` patched. Locally: `uv run pytest -m upgrade tests/integration/test_upgrade_from_release.py` (about 10 s warm). In CI it is the `upgrade` job in `pytest.yml`: pushes to main and PRs into `release*` branches, 10 minute timeout; the generic full run deselects it. `RELEASING.md` lists it in the pre-release checklist.

## Mutation record

Removing the three `safety_ext.activate()` calls in `safety_migrate.py` (#1268) turns 4 tests red; making `classify()` treat unbundled copies as pristine (#1269) turns 2 red.
