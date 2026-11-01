"""The golden oracle (#1209 M0): ``corpus/golden/snapshot.json`` must reproduce byte-identically.

Every later milestone (rule-id pinning, the rules split, the package extraction) must leave
the snapshot untouched. A DELIBERATE behaviour change lands as an explicit snapshot diff in
its own PR: ``scripts/golden_snapshot.py record`` and commit the result.

The engine and audit layers are fast and gate every PR; the hook layer spawns ~600
subprocesses and runs with the slow set (pushes to main).
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SNAPSHOT = REPO / "corpus" / "golden" / "snapshot.json"


@pytest.fixture(scope="module")
def golden():
    spec = importlib.util.spec_from_file_location("golden_snapshot", REPO / "scripts" / "golden_snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def committed():
    return json.loads(SNAPSHOT.read_text())


def _diff(old, new):
    keys = sorted(set(old) | set(new))
    return [(k, old.get(k, "<absent>"), new.get(k, "<absent>")) for k in keys if old.get(k) != new.get(k)]


def test_engine_and_audit_layers_reproduce_the_snapshot(golden, committed):
    fresh = golden.record(("engine", "audit"))
    bad = _diff(committed["engine"], fresh["engine"]) + _diff(committed["audit"], fresh["audit"])
    assert not bad, bad[:20]


@pytest.mark.slow
def test_hook_layer_reproduces_the_snapshot(golden, committed):
    fresh = golden.record(("hooks",))
    bad = _diff(committed["hooks"], fresh["hooks"])
    assert not bad, bad[:20]


def test_snapshot_covers_every_hook_and_every_corpus_case(golden, committed):
    """The oracle is only as strong as its inputs: a corpus case missing from it is unpinned."""
    items = golden.collect_items()
    missing = [
        (i["tool"], i["input"][:80]) for i in items
        if golden.record_key(i["tool"], i["input"], i["home"], "default") not in committed["engine"]
    ]
    hook_tools = {json.loads(k)[0] for k in committed["hooks"]}
    assert not missing, missing
    assert hook_tools == {"Bash", "Edit", "Read", "mcp"}, hook_tools
