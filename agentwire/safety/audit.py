"""Join damage-control decisions to what happened next (#1068 Phase 0).

The audit log holds two row kinds in the same daily JSONL files:

* decision rows from the PreToolUse hooks (``decision`` = allowed / asked /
  blocked, plus ``tool_use_id``), and
* outcome rows from ``hooks/outcome_logger.py`` (``kind`` = ``outcome``,
  ``result`` = executed / failed / denied).

``join_outcomes`` pairs them by ``tool_use_id`` and assigns each decision a
``label`` the rule tuning can count:

    allowed  + executed     ran as the rule intended
    asked    + executed     a human (or auto mode) approved the prompt
    asked    + no outcome   denied or abandoned: nothing ran
    blocked  + (none)       the rule stopped it
    any      + denied       Claude Code's own classifier refused it
    any      + failed       ran and failed

Rows without a ``tool_use_id`` (written before this existed) stay unlabelled.
"""

import glob
import json
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def load_rows(log_dir: Optional[Path] = None, days: Optional[int] = None) -> List[dict]:
    """Every parseable row from the audit log directory, oldest file first.

    ``days`` keeps only the last N daily files (filename-dated).
    """
    base = log_dir or Path(os.environ.get("AGENTWIRE_DIR", os.path.expanduser("~/.agentwire"))) / "logs" / "damage-control"
    rows: List[dict] = []
    files = sorted(glob.glob(str(base / "*.jsonl")))
    if days is not None:
        cutoff = (datetime.now().date() - timedelta(days=days - 1)).isoformat()
        files = [f for f in files if Path(f).stem >= cutoff]
    for path in files:
        with open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def _label(decision: str, result: Optional[str]) -> str:
    if result == "denied":
        return "classifier-denied"
    if result == "failed":
        return "failed"
    if decision == "blocked":
        return "rule-blocked"
    if decision == "asked":
        return "approved" if result == "executed" else "declined-or-abandoned"
    if result == "executed":
        return "ran"
    return "unknown"


def join_outcomes(rows: Iterable[dict]) -> List[dict]:
    """Decision rows with an ``outcome`` and ``label`` attached where joinable."""
    rows = list(rows)
    outcomes: Dict[str, dict] = {
        r["tool_use_id"]: r for r in rows if r.get("kind") == "outcome" and r.get("tool_use_id")
    }
    joined: List[dict] = []
    for row in rows:
        if row.get("kind") == "outcome":
            continue
        tid = row.get("tool_use_id")
        if not tid:
            joined.append({**row, "outcome": None, "label": None})
            continue
        outcome = outcomes.get(tid)
        joined.append({
            **row,
            "outcome": outcome,
            "label": _label(row.get("decision", ""), outcome.get("result") if outcome else None),
        })
    return joined


def label_counts(joined: Iterable[dict], by: str = "rule_id") -> Dict[str, Counter]:
    """``{rule_id: Counter(label)}`` over the labelled rows."""
    table: Dict[str, Counter] = {}
    for row in joined:
        if row.get("label"):
            table.setdefault(row.get(by) or "(no rule)", Counter())[row["label"]] += 1
    return table


def summarize_outcomes(days: int = 14, log_dir: Optional[Path] = None) -> Dict[str, object]:
    """Label counts over the window: overall, and per rule id."""
    joined = join_outcomes(load_rows(log_dir, days))
    labelled = [r for r in joined if r.get("label")]
    overall: Counter = Counter(r["label"] for r in labelled)
    return {
        "decisions": len(joined),
        "labelled": len(labelled),
        "unlabelled": len(joined) - len(labelled),
        "by_label": dict(overall),
        "by_rule": {rule: dict(c) for rule, c in label_counts(labelled).items()},
    }
