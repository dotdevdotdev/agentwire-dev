"""Local state for the missions subsystem.

State files live under ``~/.agentwire/missions/state/``. State is small JSON,
written atomically via tempfile + ``os.replace`` so concurrent orchestrator
runs don't tear each other's writes.

- ``last_tick.json``: ``{component: iso_timestamp}`` heartbeats for
  dispatcher / feedback_router / gc.
- ``routed_reviews.json``: ``{pr_number_str: last_routed_review_id}`` —
  feedback-router idempotency key per PR.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".agentwire" / "missions" / "state"
LAST_TICK_PATH = STATE_DIR / "last_tick.json"
ROUTED_REVIEWS_PATH = STATE_DIR / "routed_reviews.json"


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file in same dir, then ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_last_tick() -> dict:
    """Return the dict of ``{component: iso_timestamp}``."""
    return _read_json(LAST_TICK_PATH)


def record_tick(component: str) -> None:
    """Stamp the current time for a component (``dispatcher`` / ``feedback_router`` / ``gc``)."""
    data = read_last_tick()
    data[component] = _now_iso()
    _atomic_write(LAST_TICK_PATH, data)


def read_routed_reviews() -> dict[str, int]:
    """Return ``{pr_number_str: last_routed_review_id}``."""
    raw = _read_json(ROUTED_REVIEWS_PATH)
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def write_routed_reviews(data: dict[str, int]) -> None:
    """Persist the full ``{pr_number_str: review_id}`` dict."""
    _atomic_write(ROUTED_REVIEWS_PATH, {str(k): int(v) for k, v in data.items()})


def update_routed_review(pr_number: int, review_id: int) -> None:
    """Bump a single PR's last-routed-review-id."""
    data = read_routed_reviews()
    data[str(pr_number)] = int(review_id)
    write_routed_reviews(data)


def last_routed_review(pr_number: int) -> int | None:
    """Return the last review id we routed for a PR, or ``None``."""
    return read_routed_reviews().get(str(pr_number))


def forget_pr(pr_number: int) -> None:
    """Drop a PR's review-tracking entry (called by gc when PR is reaped)."""
    data = read_routed_reviews()
    data.pop(str(pr_number), None)
    write_routed_reviews(data)
