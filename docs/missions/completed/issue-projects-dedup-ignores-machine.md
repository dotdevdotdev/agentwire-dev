# Issue: Projects list deduplicates across machines incorrectly

## Problem

When the same project exists on multiple remote machines (e.g. `~/projects/piinpoint.com` on both `eric-devbox` and `jordan-devbox`), only one appears in the portal's project list. The machine name is also not displayed.

## Root Cause

`server.py:1685-1711` — The dedup key is `normalize_path(path)` which expands `~` to the **local** `$HOME`. Since both remote machines report `~/projects/piinpoint.com`, they normalize to the same local path and the second is discarded as a duplicate.

```python
# Current: dedup by path only
normalized = normalize_path(path)
if normalized not in seen_normalized:
```

## Fix

Include the machine name in the dedup key. Projects on different machines are distinct even if paths match.

```python
# Fixed: dedup by (machine, path)
machine = project.get("machine", "local")
dedup_key = f"{machine}:{normalize_path(path)}"
if dedup_key not in seen_normalized:
```

## Affected Files

- `agentwire/server.py` lines 1697-1711 (dedup logic in `/api/projects`)

## Additional Issue

The portal UI doesn't show which machine a project is on. The JSON response includes `"machine"` but the frontend doesn't display it. Projects with the same name on different machines would be indistinguishable without a machine label (e.g. `piinpoint.com @eric-devbox`).
