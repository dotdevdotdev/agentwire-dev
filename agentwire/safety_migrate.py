"""``agentwire safety migrate``: bring a pre-package damage-control install onto the package (#1209 M6).

Before the package, ``heal`` COPIED the bundled rules and tooldefs into ``~/.agentwire`` and
registered per-kind hook SCRIPTS by path. Both are now wrong in the same way: a copied file
REPLACES the bundled file of the same name (a stale copy is not protection, it is old rules), and
a script registration runs code that no longer ships. This command classifies what is on the
machine with ``rule_baselines.json`` (the sha256 of every version we ever shipped):

* an overlay file matching a shipped hash holds nothing the bundle lacks, so it is deleted;
* any other overlay file is a human's edit, so it stays (and is named; it still overrides by name);
* pack decisions that the old always-on files implied are recorded (``packs:`` in the policy);
* registrations of the old scripts, and the scripts, are removed once the package hooks are
  installed and proven.

Human-run only (tty-gated, and in ``PROTECTED_COMMAND_PATTERNS`` like the other policy mutators).
It is one-shot: after a machine is migrated nothing reads ``rule_baselines.json`` again.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from damage_control import harness_config as hc
from damage_control import install as dc_install
from damage_control import packs
from damage_control.engine import config_override_dir, policy_file_path

from . import safety_ext

BASELINES_PATH = Path(__file__).parent / "safety" / "rule_baselines.json"

# The pre-package scripts, registered by path (#1209 M4).
LEGACY_HOOK_SUFFIX = "-tool-damage-control.py"
LEGACY_HOOK_FILES = (
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
    "read-tool-damage-control.py",
    "mcp-tool-damage-control.py",
    "audit_logger.py",
)


def load_baselines() -> Dict[str, Dict[str, List[str]]]:
    """The shipped-hash manifest: ``{"rules"|"tooldefs": {filename: [sha256]}}``."""
    try:
        data = json.loads(BASELINES_PATH.read_text())
    except (OSError, ValueError):
        return {"rules": {}, "tooldefs": {}}
    return {section: data.get(section) or {} for section in ("rules", "tooldefs")}


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _dirs() -> Dict[str, Path]:
    base = config_override_dir()
    return {"rules": base / "damage-control", "tooldefs": base / "tooldefs"}


def _legacy_dir() -> Path:
    return config_override_dir() / "hooks" / "damage-control"


def classify() -> Dict[str, Dict[str, List[str]]]:
    """``{section: {"pristine": [...], "edited": [...]}}`` for the shipped-named files on the machine."""
    out: Dict[str, Dict[str, List[str]]] = {}
    baselines = load_baselines()
    for section, live in _dirs().items():
        pristine: List[str] = []
        edited: List[str] = []
        for name, hashes in sorted(baselines[section].items()):
            target = live / name
            if target.is_file():
                (pristine if _sha256(target) in hashes else edited).append(name)
        out[section] = {"pristine": pristine, "edited": edited}
    return out


def legacy_files() -> List[Path]:
    return [p for name in LEGACY_HOOK_FILES if (p := _legacy_dir() / name).exists()]


def _is_legacy(hook: Any) -> bool:
    return isinstance(hook, dict) and str(hook.get("command", "")).endswith(LEGACY_HOOK_SUFFIX)


def read_user_settings() -> Dict[str, Any]:
    """Claude Code's user settings as a dict; ``{}`` when absent or unparseable (migrate never writes one)."""
    try:
        data = json.loads((Path.home() / ".claude" / "settings.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def legacy_registrations(settings: Dict[str, Any]) -> List[str]:
    """Matchers in ``settings`` that still register a pre-package script."""
    pre = (settings.get("hooks") or {}).get("PreToolUse") if isinstance(settings, dict) else None
    return [str(e.get("matcher")) for e in pre or []
            if isinstance(e, dict) and any(_is_legacy(h) for h in e.get("hooks") or [])]


def _drop_legacy(settings: Dict[str, Any]) -> bool:
    pre = (settings.get("hooks") or {}).get("PreToolUse")
    if not isinstance(pre, list):
        return False
    changed = False
    kept = []
    for entry in pre:
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        if isinstance(inner, list) and any(_is_legacy(h) for h in inner):
            inner = [h for h in inner if not _is_legacy(h)]
            changed = True
            if not inner:
                continue
            entry = {**entry, "hooks": inner}
        kept.append(entry)
    if changed:
        settings["hooks"]["PreToolUse"] = kept
    return changed


def _carry_forward_tmux(state: Dict[str, Any], policy: Path) -> List[str]:
    """Enable the default packs on a machine whose installed ``agentwire.yaml`` is a pristine older release.

    ``kill-server`` / ``kill-session -a`` used to ship inside ``agentwire.yaml`` (always on) and
    now live in the opt-in ``tmux`` pack, so an already-decided ``packs:`` list would silently
    lose both. The marker is the INSTALLED file being a shipped older version: that file held the
    rules, so the decision was "on". An edited file matches no shipped hash and is left alone.
    """
    wanted = [p for p in safety_ext.build_profile().default_packs if p not in (state[packs.PACKS_KEY] or [])]
    installed = _dirs()["rules"] / "agentwire.yaml"
    if not wanted or not installed.exists():
        return []
    digest = _sha256(installed)
    current = _sha256(safety_ext.RULES_DIR / "agentwire.yaml")
    shipped = load_baselines()["rules"].get("agentwire.yaml", [])
    if digest is None or digest == current or digest not in shipped:
        return []
    packs.write_state(policy, packs=sorted([*(state[packs.PACKS_KEY] or []), *wanted]))
    return wanted


def migrate_pack_state(policy: Path) -> List[str]:
    """Record the pack decision a pre-packs install implied. Returns the packs just recorded."""
    state = packs.read_state(policy)
    if state["broken"]:
        return []  # unparseable: never written over (doctor reports it)
    if state[packs.PACKS_KEY] is None:
        dirs = _dirs()
        installed = {s: {p.name for p in d.glob("*.yaml")} if d.is_dir() else set() for s, d in dirs.items()}
        migrated = packs.migrate_packs(state, installed)
        packs.write_state(policy, packs=migrated)
        return migrated
    return _carry_forward_tmux(state, policy)


def migrate(
    dry_run: bool = False,
    allow_foreign: bool = False,
    log: Callable[[str], None] = print,
) -> int:
    """Classify, install the package hooks, then retire what only the old install needed. Returns an exit code."""
    plan = classify()
    legacy = legacy_files()
    for section, groups in plan.items():
        for name in groups["pristine"]:
            log(f"{'would remove' if dry_run else 'remove'} {section[:-1]} copy {name} (the package ships it)")
        for name in groups["edited"]:
            log(f"keep {section[:-1]} {name}: edited, so it replaces the bundled file of that name")
    for path in legacy:
        log(f"{'would remove' if dry_run else 'remove'} legacy hook script {path.name}")
    if dry_run:
        log("dry run: nothing changed.")
        return 0

    policy = policy_file_path()
    recorded = migrate_pack_state(policy)
    if recorded:
        log(f"recorded already-installed packs as enabled: {', '.join(recorded)}")

    outcome = safety_ext.install(allow_foreign=allow_foreign, log=log)
    if outcome.code:
        log("the package hooks were NOT installed; the old registrations and files are left as they were.")
        return outcome.code

    for section, groups in plan.items():
        for name in groups["pristine"]:
            (_dirs()[section] / name).unlink()
    try:
        hc.edit_json_config(Path.home() / ".claude" / "settings.json", _drop_legacy,
                            backup=dc_install.backup_path("user", Path.home() / ".claude" / "settings.json"))
    except hc.ConfigError as e:
        log(f"REFUSED: {e}")
        return 1
    for path in legacy:
        path.unlink()
    try:
        _legacy_dir().rmdir()
    except OSError:
        pass  # not empty, or already gone: a file we do not own is not ours to remove
    log("migrated.")
    return 0


def cmd_safety_migrate(args) -> int:
    """CLI: ``agentwire safety migrate`` (human-only)."""
    from .tasks_cli import _host_context_ok

    if not args.dry_run and not _host_context_ok():
        print("agentwire safety migrate changes damage-control policy and registrations — human-only. "
              "Run it from your own terminal, not through an agent.", file=sys.stderr)
        return 1
    return migrate(dry_run=args.dry_run, allow_foreign=args.allow_foreign_source)
