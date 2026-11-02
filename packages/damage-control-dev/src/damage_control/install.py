"""Install, status, doctor and uninstall for the Claude Code hook registration (#1209 M5, section 5).

Every function here either does what it says or raises :class:`InstallError` having changed
nothing it could not put back. The settings file is only ever touched through
``harness_config``; ``install.json`` (under ``<config dir>/state/``) records what we wrote so
``status`` can tell drift from intent and ``uninstall`` can restore the original bytes.

Ownership: an entry is ours iff the package module name is in its ``args``. An entry carrying
a ``--profile`` belongs to whoever installed that profile (a host such as agentwire): ``init``
refuses to rewrite an entry whose profile differs from the one requested, so two installers
never fight over the same matcher.
"""

import difflib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from damage_control import __version__, engine, packs, ruleset
from damage_control import claude_code as cc
from damage_control import harness_config as hc

ENGINE_SCHEMA = 1
RECORD_SCHEMA = 1
FAIL_OPEN_KEY = "fail_open"
SELFTEST_PROBE = "rm -rf /damage-control-selftest"
DEV_SOURCES = ("editable", "vcs", "ephemeral")


class InstallError(RuntimeError):
    """A refusal: the message says why, and nothing was changed."""


_MARKS = {"ok": "ok", "info": "..", "bad": "!!"}


class Finding(NamedTuple):
    level: str  # "ok" | "info" | "bad"
    code: str
    message: str

    def line(self) -> str:
        return f"[{_MARKS[self.level]}] {self.message}"


# ----------------------------------------------------------------------------
# Paths and the install record
# ----------------------------------------------------------------------------

def config_home() -> Path:
    return engine.config_override_dir()


def state_dir() -> Path:
    return config_home() / "state"


def record_path() -> Path:
    return state_dir() / "install.json"


def backup_path(scope: str, settings: Path) -> Path:
    return state_dir() / "backups" / f"{scope}-{settings.name}.orig"


def breakglass_path() -> Path:
    return config_home() / "bin" / "damage-control-breakglass"


def harness_key(scope: str) -> str:
    return f"{cc.HARNESS}:{scope}"


def load_record() -> Dict[str, Any]:
    try:
        data = json.loads(record_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_record(record: Dict[str, Any]) -> None:
    path = record_path()
    data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    expected = hc.read_bytes(path)
    if not hc.atomic_replace(path, data, expected):
        raise InstallError(f"{path} changed while it was being written; re-run.")


def version_tuple(text: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text.split("+")[0])[:4]) or (0,)


# ----------------------------------------------------------------------------
# Provenance (minimal): where is this package running from?
# ----------------------------------------------------------------------------

def install_source() -> str:
    """``pypi`` | ``vcs`` | ``editable`` | ``ephemeral`` (a uvx-style throwaway environment)."""
    try:
        raw = importlib.metadata.distribution("damage-control-dev").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return "editable"  # imported from a source tree, not an installed distribution
    if raw:
        try:
            info = json.loads(raw)
        except ValueError:
            info = {}
        if "vcs_info" in info:
            return "vcs"
        if (info.get("dir_info") or {}).get("editable"):
            return "editable"
    prefix = sys.prefix.replace(os.sep, "/")
    if "/uv/archive-v" in prefix or "/uv/environments-v" in prefix:
        return "ephemeral"
    return "pypi"


def interpreter_runs(interpreter: str) -> bool:
    try:
        return subprocess.run([interpreter, "-I", "-c", "import damage_control"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ----------------------------------------------------------------------------
# Self-test: run the registered command the way Claude Code does
# ----------------------------------------------------------------------------

def selftest(interpreter: str, profile: Optional[str] = None) -> List[str]:
    """What failed when each hook kind was run as registered (empty = all good).

    Every kind must start and exit 0 for a tool it does not gate; the shell hook must also
    BLOCK (exit 2) a destructive command, which proves the rules actually loaded.
    """
    probes = [(kind, {"tool_name": "DamageControlSelftest", "tool_input": {}}, 0)
              for kind in dict.fromkeys(cc.MATCHERS.values())]
    probes += [("bash", {"tool_name": tool, "tool_input": {"command": SELFTEST_PROBE}}, 2)
               for tool in ("Bash", "PowerShell")]
    failures = []
    for kind, payload, want in probes:
        try:
            got = subprocess.run(cc.selftest_command(interpreter, kind, profile), input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=engine.HOOK_TIMEOUT_SECONDS).returncode
        except (OSError, subprocess.SubprocessError) as e:
            failures.append(f"{kind}: {type(e).__name__}: {e}")
            continue
        if got != want:
            failures.append(f"{kind}: exit {got}, expected {want}")
    return failures


# ----------------------------------------------------------------------------
# Structural edits of the settings dict
# ----------------------------------------------------------------------------

def _owned_under(pre: List[Any], matcher: str, kind: str) -> List[Dict[str, Any]]:
    owned: List[Dict[str, Any]] = []
    for entry in pre:
        inner = entry.get("hooks") if isinstance(entry, dict) and entry.get("matcher") == matcher else None
        if isinstance(inner, list):
            owned.extend(h for h in inner if cc.is_ours(h) and cc.kind_of(h) == kind)
    return owned


def register(settings: Dict[str, Any], desired: Dict[str, Dict[str, Any]], created: List[str]) -> bool:
    """Make ``settings`` carry exactly ``desired`` (matcher -> hook) as OUR entries. True if it changed.

    Idempotent. Adds what is missing, rewrites an entry of ours whose interpreter, arguments,
    ``timeout`` or ``onFailure`` drifted, drops duplicates and entries of ours under a matcher
    that is not ours to register. Every entry that is not ours is left exactly as it was.
    Raises ConfigError on a malformed ``hooks`` tree (rewriting it would destroy what is there).
    """
    changed = False
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = settings["hooks"] = {}
        created.append("hooks")
    elif not isinstance(hooks, dict):
        raise hc.ConfigError("settings has a malformed `hooks` (not an object); damage control not registered")
    pre = hooks.get("PreToolUse")
    if pre is None:
        pre = hooks["PreToolUse"] = []
        created.append("hooks.PreToolUse")
    elif not isinstance(pre, list):
        raise hc.ConfigError("settings has a malformed `hooks.PreToolUse` (not a list); damage control not registered")

    # Entries of ours that sit under a matcher/kind pair we do not register.
    for entry in list(pre):
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(inner, list):
            continue
        matcher = entry.get("matcher")
        expected_kind = cc.MATCHERS.get(matcher) if isinstance(matcher, str) else None
        stray = [h for h in inner if cc.is_ours(h) and expected_kind != cc.kind_of(h)]
        if stray:
            changed = True
            entry["hooks"] = [h for h in inner if h not in stray]
            if not entry["hooks"]:
                pre.remove(entry)

    for matcher, wanted in desired.items():
        kind = cc.MATCHERS[matcher]
        owned = _owned_under(pre, matcher, kind)
        if not owned:
            pre.append({"matcher": matcher, "hooks": [dict(wanted)]})
            changed = True
            continue
        first, extras = owned[0], owned[1:]
        if first != wanted:
            first.clear()
            first.update(wanted)
            changed = True
        for dup in extras:
            for entry in list(pre):
                inner = entry.get("hooks") if isinstance(entry, dict) else None
                if isinstance(inner, list) and any(h is dup for h in inner):
                    entry["hooks"] = [h for h in inner if h is not dup]
                    if not entry["hooks"]:
                        pre.remove(entry)
                    changed = True
    return changed


def unregister(settings: Dict[str, Any], created: List[str]) -> bool:
    """Remove exactly our entries; prune containers we created and nothing else. True if it changed."""
    pre = cc.pre_tool_use(settings)
    if pre is None or not cc.owned_entries(settings):
        return False
    for entry in list(pre):
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(inner, list) or not any(cc.is_ours(h) for h in inner):
            continue
        remaining = [h for h in inner if not cc.is_ours(h)]
        if remaining:
            entry["hooks"] = remaining
        else:
            pre.remove(entry)
    if not pre and "hooks.PreToolUse" in created:
        del settings["hooks"]["PreToolUse"]
        if not settings["hooks"] and "hooks" in created:
            del settings["hooks"]
    return True


# ----------------------------------------------------------------------------
# init
# ----------------------------------------------------------------------------

def _resolve_settings(scope: str, settings: Optional[str], cwd: Optional[Path]) -> Path:
    if scope not in cc.SCOPES:
        raise InstallError(f"unknown scope {scope!r}; expected one of {', '.join(cc.SCOPES)}")
    return Path(settings) if settings else cc.settings_path(scope, cwd)


def _unified_diff(path: Path, before: Optional[bytes], after: bytes) -> List[str]:
    old = (before or b"").decode("utf-8", "replace").splitlines()
    new = after.decode("utf-8", "replace").splitlines()
    return list(difflib.unified_diff(old, new, f"{path} (current)", f"{path} (after init)", lineterm=""))


def init(
    scope: str = "user",
    settings: Optional[str] = None,
    dry_run: bool = False,
    refresh: bool = False,
    force: bool = False,
    fail_open: bool = False,
    allow_dev_source: bool = False,
    apply: bool = False,
    profile: Optional[str] = None,
    cwd: Optional[Path] = None,
    log: Callable[[str], None] = print,
) -> int:
    """Register the hooks, prove they run, record what was done. Idempotent. Returns 0."""
    path = _resolve_settings(scope, settings, cwd)
    if scope == "managed" and not apply:
        dry_run = True
        log("managed scope is a dry run unless --apply is given (it needs elevation and applies to every user).")

    source = install_source()
    if source in DEV_SOURCES and not allow_dev_source and not dry_run:
        raise InstallError(f"running from a {source} install; machine-global state may be written from one "
                           "only with --allow-dev-source (a throwaway checkout or environment can vanish "
                           "and, with onFailure=block, take every tool call with it).")

    record = load_record()
    recorded = version_tuple(str(record.get("package_version", "0")))
    if recorded > version_tuple(__version__) and not force:
        raise InstallError(f"installed record is from damage-control-dev {record['package_version']}, newer than "
                           f"this {__version__}; refusing to downgrade the registration (--force to override).")

    version = cc.detect_version()
    supported = cc.supports_fail_closed(version)
    if supported is not True and not fail_open:
        why = ("could not detect Claude Code (`claude --version` failed)" if version is None
               else f"Claude Code {cc.version_text(version)} is older than {cc.version_text(cc.ONFAILURE_MIN_VERSION)}")
        raise InstallError(f"{why}: `onFailure: block` is unavailable, so a hook crash or a missing interpreter "
                           "would let tool calls run. Upgrade Claude Code, or pass --fail-open to register the "
                           "weaker config explicitly (recorded in the protected policy).")
    fail_closed = not fail_open
    interpreter = cc.absolute_interpreter()

    raw, current = hc.read_config(path)  # unparseable => ConfigError, nothing touched
    for owned in cc.owned_entries(current):
        if cc.profile_of(owned.hook) != profile:
            raise InstallError(f"{path} already registers damage control for profile "
                               f"{cc.profile_of(owned.hook) or '(none)'}, not {profile or '(none)'}: another "
                               "installer owns those entries. Use that installer, or uninstall it first.")

    desired = {m: cc.wanted_hook(interpreter, k, engine.HOOK_TIMEOUT_SECONDS, fail_closed, profile)
               for m, k in cc.MATCHERS.items()}
    created: List[str] = []
    preview = json.loads(json.dumps(current))
    would_change = register(preview, desired, [])

    if dry_run:
        log(f"dry run: {'would change' if would_change else 'no change to'} {path}")
        for line in _unified_diff(path, raw, hc.render(preview, hc.detect_style(raw))):
            log(line)
        return 0

    failures = selftest(interpreter, profile)
    if failures:
        raise InstallError("self-test failed; nothing registered:\n  " + "\n  ".join(failures))

    key = harness_key(scope)
    previous = (record.get("harnesses") or {}).get(key, {})

    def mutate(data: Dict[str, Any]) -> bool:
        return register(data, desired, created)

    result = hc.edit_json_config(path, mutate, backup=backup_path(scope, path))

    config = engine.load_effective_config()
    digest = ruleset.digest(config)
    grants = sorted(engine.PROFILE.default_unattended_allow)
    old_digest, old_grants = record.get("rules"), record.get("default_grants", [])
    first_install = old_digest is None
    if not first_install:
        changes = ruleset.diff(old_digest, digest, old_grants, grants)
        if changes and not refresh:
            total, loose = ruleset.summary(changes)
            log(f"note: the ruleset changed since last acknowledged ({total} changes, {loose} loosening); "
                "run `init --refresh` to see and acknowledge them.")
            digest, grants = old_digest, old_grants
        elif changes:
            log(f"ruleset diff ({len(changes)} changes):")
            for line in ruleset.render(changes):
                log(f"  {line}")
            if ruleset.has_loosening(changes):
                log("!! this refresh WIDENS what is permitted (lines marked !!); review them.")

    policy = engine.policy_file_path()
    if fail_open:
        packs.write_flag(policy, FAIL_OPEN_KEY, True)
    elif packs.read_flag(policy, FAIL_OPEN_KEY):
        packs.write_flag(policy, FAIL_OPEN_KEY, False)

    install_breakglass()
    # Byte-exact uninstall is valid only while the file is exactly what OUR writes produced.
    prev_sha = previous.get("written_sha256")
    if not previous:
        written = result.sha256 if result.changed else None
    elif result.changed:
        untouched = prev_sha and raw is not None and hc.sha256_hex(raw) == prev_sha
        written = result.sha256 if untouched else None
    else:
        written = prev_sha
    backup = backup_path(scope, path)
    harnesses = dict(record.get("harnesses") or {})
    harnesses[key] = {
        "harness": cc.HARNESS, "scope": scope, "config_path": str(hc.resolve(path)),
        "entry_markers": [cc.HOOK_MODULE], "profile": profile,
        "backup": str(backup) if backup.exists() else None,
        "harness_version": cc.version_text(version) if version else None,
        "installed_at": previous.get("installed_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
        "fail_mode": "closed" if fail_closed else "open",
        "created_file": bool(previous.get("created_file") or (not previous and result.created and result.changed)),
        "created_keys": previous.get("created_keys") if previous else created,
        "written_sha256": written,
    }
    save_record({
        "schema": RECORD_SCHEMA, "package_version": __version__, "engine_schema": ENGINE_SCHEMA,
        "ruleset_version": f"{__version__}+{ruleset_id(digest)}", "interpreter": interpreter,
        "source": source, "rules": digest, "default_grants": grants, "harnesses": harnesses,
    })
    log(f"{'registered' if result.changed else 'already registered'}: {len(desired)} matchers in {path} "
        f"(scope {scope}, {'fail-closed' if fail_closed else 'FAIL-OPEN'}, interpreter {interpreter})")
    return 0


def ruleset_id(digest: Dict[str, str]) -> str:
    return ruleset._fingerprint(digest)


def install_breakglass() -> Path:
    """Copy the stdlib-only break-glass script out of the venv, to where a broken venv cannot hide it."""
    source = Path(__file__).with_name("breakglass.py")
    target = breakglass_path()
    data = source.read_bytes()
    if hc.read_bytes(target) != data:
        hc.atomic_replace(target, data, hc.read_bytes(target))
        target.chmod(0o755)
    return target


# ----------------------------------------------------------------------------
# status / doctor
# ----------------------------------------------------------------------------

def _mode_findings(paths: List[Path]) -> List[Finding]:
    bad = []
    for p in paths:
        try:
            mode = p.stat().st_mode
        except OSError:
            continue
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            bad.append(f"{p} is writable by group/other ({stat.filemode(mode)})")
    return [Finding("bad", "perms", m) for m in bad] or [Finding("ok", "perms", "protected paths are owner-writable only")]


def _overlay_findings() -> List[Finding]:
    rules, tooldefs = engine.overlay_dirs()
    findings: List[Finding] = []
    count = 0
    for directory in (rules, tooldefs):
        for file in sorted(directory.iterdir()) if directory else []:
            if file.suffix not in (".json", ".yaml", ".yml"):
                continue
            count += 1
            try:
                if file.suffix == ".json":
                    json.loads(file.read_text())
                else:
                    import yaml

                    yaml.safe_load(file.read_text())
            except ImportError:
                findings.append(Finding("info", "overlay-yaml", f"PyYAML not installed: {file.name} cannot be read"))
            except Exception as e:
                findings.append(Finding("bad", "overlay-parse", f"overlay file {file} does not parse: {type(e).__name__}"))
    if count and not findings:
        findings.append(Finding("info", "overlay", f"{count} local overlay file(s) in effect"))
    return findings


def _drift(owned: List[cc.Owned], interpreter: str, profile: Optional[str], fail_closed: bool) -> List[Finding]:
    findings: List[Finding] = []
    claimed: List[int] = []
    missing = []
    for matcher, kind in cc.MATCHERS.items():
        hooks = [o.hook for o in owned if o.matcher == matcher and cc.kind_of(o.hook) == kind]
        if not hooks:
            missing.append(matcher)
            continue
        claimed.extend(id(h) for h in hooks)
        hook = hooks[0]
        if len(hooks) > 1:
            findings.append(Finding("info", "duplicate", f"{matcher}: {len(hooks)} entries of ours (one is enough)"))
        if hook.get("command") != interpreter:
            findings.append(Finding("bad", "command", f"{matcher}: command is {hook.get('command')!r}, expected {interpreter!r}"))
        if hook.get("args") != cc.hook_args(kind, profile):
            findings.append(Finding("bad", "args", f"{matcher}: args are {hook.get('args')!r}, expected {cc.hook_args(kind, profile)!r}"))
        if hook.get("timeout") != engine.HOOK_TIMEOUT_SECONDS:
            findings.append(Finding("bad", "timeout", f"{matcher}: timeout is {hook.get('timeout')!r}, expected {engine.HOOK_TIMEOUT_SECONDS} (lost)"))
        if fail_closed and hook.get("onFailure") != "block":
            findings.append(Finding("bad", "onFailure", f"{matcher}: onFailure is {hook.get('onFailure')!r}, expected 'block' (lost)"))
    if missing:
        findings.append(Finding("bad", "missing", "missing entry for matcher(s): " + ", ".join(missing)))
    for o in owned:
        if id(o.hook) not in claimed:
            findings.append(Finding("bad", "matcher", f"wrong matcher: {o.matcher!r} runs the {cc.kind_of(o.hook)!r} hook "
                                    f"(expected {[m for m, k in cc.MATCHERS.items() if k == cc.kind_of(o.hook)] or 'none'})"))
    return findings


def inspect(scope: str = "user", settings: Optional[str] = None, deep: bool = False,
            cwd: Optional[Path] = None, profile_hint: Optional[str] = None) -> List[Finding]:
    """Findings for one registration. ``deep`` (doctor) adds the self-test and a live Claude Code check."""
    path = _resolve_settings(scope, settings, cwd)
    record = load_record()
    hrec = (record.get("harnesses") or {}).get(harness_key(scope), {})
    findings: List[Finding] = []
    try:
        _, current = hc.read_config(path)
    except hc.ConfigError as e:
        return [Finding("bad", "unreadable", str(e))]
    owned = cc.owned_entries(current)
    if not owned and not hrec:
        return [Finding("bad", "not-installed", f"damage control is not registered in {path}; run `damage-control-dev init`")]

    profile = hrec.get("profile") if hrec else (profile_hint or (cc.profile_of(owned[0].hook) if owned else None))
    interpreter = record.get("interpreter") or (owned[0].hook.get("command") if owned else cc.absolute_interpreter())
    fail_closed = (hrec.get("fail_mode", "closed") == "closed") if hrec else True
    if not hrec:
        findings.append(Finding("info", "no-record", "no install record for this scope (registered by a host installer?)"))
        fail_closed = all(o.hook.get("onFailure") == "block" for o in owned)

    drift = _drift(owned, interpreter, profile, fail_closed)
    findings += drift or [Finding("ok", "registration", f"{len(cc.MATCHERS)} matchers registered correctly in {path}")]

    for command in sorted({o.hook.get("command", "") for o in owned}):
        alive = interpreter_runs(command) if command else False
        findings.append(Finding("ok" if alive else "bad", "interpreter",
                                f"interpreter {command} runs and imports damage_control" if alive
                                else f"interpreter {command!r} is dead or cannot import damage_control: "
                                     "every tool call is being blocked; run the break-glass script, then repair"))

    if record:
        running, recorded = version_tuple(__version__), version_tuple(str(record.get("package_version", "0")))
        if recorded > running:
            findings.append(Finding("bad", "downgrade", f"running {__version__} is older than the recorded {record['package_version']}"))
        elif recorded < running:
            findings.append(Finding("info", "upgraded", f"package {__version__} is newer than the recorded {record['package_version']}"))
        if record.get("rules") is not None:
            changes = ruleset.diff(record["rules"], ruleset.digest(engine.load_effective_config()),
                                   record.get("default_grants", []), sorted(engine.PROFILE.default_unattended_allow))
            if changes:
                total, loose = ruleset.summary(changes)
                findings.append(Finding("bad" if loose else "info", "stale-ruleset",
                                        f"ruleset changed since last acknowledged ({total} changes, {loose} loosening); "
                                        "run `damage-control-dev init --refresh`"))
            else:
                findings.append(Finding("ok", "ruleset", "ruleset matches the last acknowledged one"))

    foreign = cc.foreign_entry_count(current)
    if foreign:
        findings.append(Finding("info", "foreign-hooks", f"{foreign} PreToolUse hook(s) in {path} are not ours (left alone)"))
    findings += _overlay_findings()
    protected = [config_home(), engine.policy_file_path(), record_path()]
    findings += _mode_findings([p for p in protected if p.exists()])

    if deep:
        version = cc.detect_version()
        supported = cc.supports_fail_closed(version)
        if fail_closed and supported is False:
            findings.append(Finding("bad", "harness-version", f"Claude Code {cc.version_text(version)} predates `onFailure`; fail-closed is not honored"))
        elif not fail_closed and supported:
            findings.append(Finding("info", "harness-version", f"Claude Code {cc.version_text(version)} supports fail-closed; re-run `init` without --fail-open"))
        elif version is not None:
            findings.append(Finding("ok", "harness-version", f"Claude Code {cc.version_text(version)}"))
        failures = selftest(interpreter, profile)
        findings += ([Finding("bad", "selftest", "self-test failed: " + "; ".join(failures))] if failures
                     else [Finding("ok", "selftest", "self-test passed (every hook starts, destructive command blocked)")])
    return findings


def exit_code(findings: List[Finding]) -> int:
    return 1 if any(f.level == "bad" for f in findings) else 0


# ----------------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------------

def uninstall(scope: str = "user", settings: Optional[str] = None, purge: bool = False,
              cwd: Optional[Path] = None, log: Callable[[str], None] = print) -> int:
    """Remove exactly our entries. Byte-exact when the file is still what init wrote."""
    path = _resolve_settings(scope, settings, cwd)
    record = load_record()
    key = harness_key(scope)
    hrec = (record.get("harnesses") or {}).get(key, {})
    raw, current = hc.read_config(path)

    if not cc.owned_entries(current):
        log(f"nothing to remove: no damage-control entries in {path}")
    else:
        restored = False
        backup = Path(hrec["backup"]) if hrec.get("backup") else None
        sha = hrec.get("written_sha256")
        if raw is not None and sha and hc.sha256_hex(raw) == sha:
            if hrec.get("created_file"):
                restored = hc.restore_bytes(path, None, sha)
            elif backup and backup.exists():
                restored = hc.restore_bytes(path, backup.read_bytes(), sha)
        if restored:
            log(f"restored {path} byte-for-byte to its state before init")
        else:
            hc.edit_json_config(path, lambda data: unregister(data, hrec.get("created_keys") or []))
            log(f"removed our entries from {path}; every other key left as it was")

    harnesses = {k: v for k, v in (record.get("harnesses") or {}).items() if k != key}
    if record and harnesses:
        save_record(dict(record, harnesses=harnesses))
    elif record_path().exists():
        record_path().unlink()
    if not harnesses:
        for leftover in (breakglass_path(),):
            leftover.unlink(missing_ok=True)
    log("DAMAGE CONTROL IS NOW OFF for this scope." if not purge else "damage control is off and its local state is purged.")
    if purge:
        purge_local_state(log)
    return 0


def purge_local_state(log: Callable[[str], None]) -> None:
    """Delete the policy, overlays and state we keep (never the settings file)."""
    import shutil

    rules, tooldefs = engine.overlay_dirs()
    for directory in (rules, tooldefs, state_dir(), config_home() / "bin"):
        if directory and directory.exists():
            shutil.rmtree(directory)
            log(f"purged {directory}")
    policy = engine.policy_file_path()
    if policy.exists():
        policy.unlink()
        log(f"purged {policy}")
