"""CLI for diagnostics — ``agentwire doctor`` and ``agentwire network``.

``doctor`` walks the full install (Python/deps, hooks, skills, damage control,
source-checkout drift, services, config, SSH/tunnels, voice loop, secrets,
remote machines, dead-lettered messages, dangling worktree sessions) and
optionally auto-fixes the local ones. ``network status`` is the read-only
network-health glance.

The hook/skill drift helpers (``get_hooks_source``, ``_managed_hook_files``,
``_managed_file_state``, ``skill_drift``, ``CLAUDE_SKILLS_DIR``) are owned by
the hooks domain and live in ``hooks_cli``; doctor reads them via a
function-local deferred import to stay single-source-of-truth.
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .core import (
    _git_behind_origin,
    _run_remote,
    _tmux_global_option,
    get_portal_session_name,
    get_source_dir,
    get_tts_session_name,
    load_config,
    tmux_session_exists,
)


def cmd_network_status(args) -> int:
    """Show complete network health at a glance."""
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health, test_ssh_connectivity

    ctx = NetworkContext.from_config()
    tm = TunnelManager()
    issues = []

    # Print header
    print("AgentWire Network Status")
    print("=" * 60)
    hostname = ctx.local_machine_id or socket.gethostname()
    print(f"\nYou are on: {hostname}")

    # Check machines (SSH connectivity)
    print("\nMachines")
    print("-" * 60)

    for machine_id, machine in ctx.machines.items():
        is_local = machine_id == ctx.local_machine_id
        host = machine.get("host", machine_id)
        user = machine.get("user")

        if is_local:
            print(f"  {machine_id:<16}(this machine)    [ok] reachable")
        else:
            latency = test_ssh_connectivity(host, user, timeout=5)
            if latency is not None:
                print(f"  {machine_id:<16}{host:<18}[ok] reachable (ssh: {latency}ms)")
            else:
                print(f"  {machine_id:<16}{host:<18}[!!] unreachable")
                issues.append({
                    "type": "machine_unreachable",
                    "machine": machine_id,
                    "host": host,
                })

    # Check services
    print("\nServices")
    print("-" * 60)

    # Resolve the ACTIVE voice tier for each subsystem and only health-check a
    # server when that tier actually uses one. A tier with no server (default
    # TTS, cloud/browser-fallback STT) reports its path, not a phantom probe —
    # and an orphaned engine server (up but unused by the tier) is flagged.
    from .config import load_config as load_config_typed
    from .voice_status import resolve_stt_status, resolve_tts_status

    typed_cfg = load_config_typed()
    for resolver in (resolve_tts_status, resolve_stt_status):
        vs = resolver(typed_cfg)
        label = vs.subsystem.upper()
        loc = vs.server_url if vs.server_url else f"{vs.tier} tier"
        if vs.ready:
            mark, note = "[ok]", (vs.path if not vs.server_url else "running")
        else:
            mark, note = "[!!]", vs.detail
            issues.append({"type": "service_down", "service": vs.subsystem,
                            "location": loc, "error": vs.detail})
        print(f"  {label:<16}{loc:<18}{mark} {note}")
        for w in vs.warnings:
            print(f"  {'':<16}{'':<18}[..] {w}")
            issues.append({"type": "voice_orphan", "service": vs.subsystem, "warning": w})

    for service_name in ["portal"]:
        service_config = getattr(ctx.config.services, service_name, None)
        if service_config is None:
            continue

        if ctx.is_local(service_name):
            location = f"localhost:{service_config.port}"
            via = "(local)"
        else:
            machine = service_config.machine
            location = f"{machine}:{service_config.port}"
            via = "(via tunnel)"

        # Test the service health
        url = ctx.get_service_url(service_name)
        health_url = f"{url}{service_config.health_endpoint}"
        is_healthy, error = test_service_health(health_url, timeout=3)

        if is_healthy:
            print(f"  {service_name.capitalize():<16}{location:<18}[ok] running {via}")
        else:
            print(f"  {service_name.capitalize():<16}{location:<18}[!!] not responding")
            issues.append({
                "type": "service_down",
                "service": service_name,
                "location": location,
                "error": error,
            })

    # Check tunnels
    required_tunnels = ctx.get_required_tunnels()
    if required_tunnels:
        print("\nTunnels (this machine)")
        print("-" * 60)

        for spec in required_tunnels:
            status = tm.check_tunnel(spec)
            target = f"localhost:{spec.local_port}"

            if status.status == "up":
                print(f"  -> {spec.remote_machine:<12}{target:<18}[ok] up (PID {status.pid})")
            else:
                print(f"  -> {spec.remote_machine:<12}{target:<18}[!!] down")
                issues.append({
                    "type": "tunnel_down",
                    "spec": spec,
                    "error": status.error,
                })

    # Check for worker sessions
    print("\nWorker Sessions")
    print("-" * 60)

    # Local sessions
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        sessions = [s for s in result.stdout.strip().split("\n") if s and not s.startswith("agentwire")]
        if sessions:
            print(f"  {hostname:<16}{len(sessions)} sessions    {', '.join(sessions[:5])}")
            if len(sessions) > 5:
                print(f"  {'':<16}... and {len(sessions) - 5} more")
        else:
            print(f"  {hostname:<16}0 sessions")
    else:
        print(f"  {hostname:<16}(no tmux server)")

    # Remote sessions
    for machine_id, machine in ctx.machines.items():
        if machine_id == ctx.local_machine_id:
            continue

        result = _run_remote(machine_id, "tmux list-sessions -F '#{session_name}' 2>/dev/null")
        if result.returncode == 0 and result.stdout.strip():
            sessions = [s for s in result.stdout.strip().split("\n") if s]
            if sessions:
                print(f"  {machine_id:<16}{len(sessions)} sessions    {', '.join(sessions[:5])}")
                if len(sessions) > 5:
                    print(f"  {'':<16}... and {len(sessions) - 5} more")
        else:
            print(f"  {machine_id:<16}0 sessions")

    # Summary
    print()
    if not issues:
        print("Everything looks good!")
    else:
        print(f"Issues detected: {len(issues)}")
        print()
        for i, issue in enumerate(issues, 1):
            if issue["type"] == "machine_unreachable":
                print(f"  {i}. Machine '{issue['machine']}' unreachable")
                print(f"     Host: {issue['host']}")
                print()
                print("     To fix:")
                print(f"       Check SSH connectivity: ssh {issue['host']}")
                print("       Verify machine is running")
                print()

            elif issue["type"] == "service_down":
                print(f"  {i}. {issue['service'].capitalize()} not responding")
                print(f"     Location: {issue['location']}")
                if issue.get("error"):
                    print(f"     Error: {issue['error']}")
                print()
                print("     To fix:")
                if issue["service"] == "portal":
                    print("       agentwire portal start")
                elif issue["service"] == "tts":
                    print("       agentwire tts start")
                print("       agentwire tunnels check  # Verify tunnel health")
                print()

            elif issue["type"] == "tunnel_down":
                spec = issue["spec"]
                print(f"  {i}. Missing tunnel")
                print(f"     Required: localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port}")
                if issue.get("error"):
                    print(f"     Error: {issue['error']}")
                print()
                print("     To fix:")
                print("       agentwire tunnels up")
                print()

        print("-" * 60)
        print()
        print("Run: agentwire doctor    # Auto-fix common issues")

    return 0 if not issues else 1


def _render_skill_section() -> int:
    """Print the global-skill drift block. Returns the count of issues found.

    Hand-placed at wiki-setup and never resynced, so a stale or missing copy was
    invisible until #475. Flagged the same way as hooks. `source-unavailable`
    (running from a checkout, where skills only live in the built wheel) is NOT a
    drift problem — there's nothing to install from — so it never bumps the count.
    """
    from .hooks_cli import CLAUDE_SKILLS_DIR, skill_drift

    issues = 0
    for name, state in sorted(skill_drift().items()):
        target = CLAUDE_SKILLS_DIR / name
        if state == "ok":
            print(f"  [ok] /{name} skill: {target}")
        elif state == "source-unavailable":
            print(f"  [..] /{name} skill: source not packaged here (running from a checkout)")
        elif state == "stale":
            print(f"  [!!] /{name} skill: STALE — installed copy differs from packaged source")
            print("     Run: agentwire hooks install")
            issues += 1
        else:
            print(f"  [!!] /{name} skill: not installed")
            print("     Run: agentwire hooks install")
            issues += 1
    return issues


def _render_damage_control_section() -> int:
    """Print the damage-control health block. Returns the count of issues found.

    Covers the four #462 blind spots plus DC hook staleness: the global kill
    switch (``safety.enabled: false``), installed-rules drift vs the bundled
    rules, PreToolUse matcher registration, and DC hook-script staleness.
    """
    issues = 0

    # Kill switch — ``enabled`` in ~/.agentwire/damagecontrol.yml gates ALL
    # damage control. A silent `false` is the loudest possible failure, so flag
    # it first and hard.
    try:
        from .safety._core import load_safety_config
        safety_enabled = load_safety_config().get("enabled", True)
    except Exception as e:
        print(f"  [..] Could not read safety config: {e}")
        safety_enabled = True
    if not safety_enabled:
        print("  [!!] Damage control is DISABLED (enabled: false)")
        print("       ALL command/path/outbound gating is off.")
        print("       Fix: set enabled: true in ~/.agentwire/damagecontrol.yml")
        issues += 1
    else:
        print("  [ok] Damage control enabled (enabled: true)")

    try:
        from . import safety_commands
    except Exception as e:
        print(f"  [..] Could not load safety module: {e}")
        return issues

    # DC hook-script staleness (bash/edit/write/mcp-tool + audit_logger).
    hook_drift = safety_commands.damage_control_hook_drift()
    stale_hooks = [f for f, s in hook_drift.items() if s == "stale"]
    missing_hooks = [f for f, s in hook_drift.items() if s == "missing"]
    if stale_hooks or missing_hooks:
        if missing_hooks:
            print(f"  [!!] DC hook scripts missing: {', '.join(sorted(missing_hooks))}")
        if stale_hooks:
            print(f"  [!!] DC hook scripts STALE: {', '.join(sorted(stale_hooks))}")
        print("       Fix: agentwire safety install --yes")
        issues += 1
    else:
        print("  [ok] DC hook scripts current")

    # Installed-rules drift vs bundled rules (the incident's missing files).
    rule_drift = safety_commands.rules_drift()
    missing_rules = [f for f, s in rule_drift.items() if s == "missing"]
    stale_rules = [f for f, s in rule_drift.items() if s == "stale"]
    if missing_rules:
        print(f"  [!!] Damage-control rules NOT installed: {', '.join(sorted(missing_rules))}")
        print("       Fix: agentwire safety install --yes")
        issues += 1
    elif stale_rules:
        # Stale = installed copy differs from bundled. Could be an intentional
        # customization, so warn (not error) and don't auto-overwrite.
        print(f"  [..] Damage-control rules differ from bundled: {', '.join(sorted(stale_rules))}")
        print("       (customized? `agentwire safety install --yes` leaves these untouched)")
    else:
        print("  [ok] Damage-control rules installed and match bundled")

    # Matcher presence in ~/.claude/settings.json.
    missing_matchers = safety_commands.missing_damage_control_matchers()
    if missing_matchers:
        print(f"  [!!] PreToolUse matchers not registered: {', '.join(missing_matchers)}")
        print("       Fix: agentwire safety install --yes")
        issues += 1
    else:
        print("  [ok] PreToolUse damage-control matchers registered")

    return issues


def _render_voice_loop_section(config, ctx) -> int:
    """Print the voice-loop (push-to-talk) preflight. Returns failures found.

    Read-only pass/fail per stage with an actionable fix line when red — the
    existing infra sections keep the auto-fix prompts.
    """
    from .doctor_voice import run_voice_loop_checks

    print("\nChecking voice loop (push-to-talk)...")
    failures = 0
    for stage in run_voice_loop_checks(config, ctx):
        if stage.status == "ok":
            print(f"  [ok] {stage.name}: {stage.detail}")
        elif stage.status == "info":
            print(f"  [..] {stage.name}: {stage.detail}")
        else:
            print(f"  [!!] {stage.name}: {stage.detail}")
            for fix in stage.fixes:
                print(f"       Fix: {fix}")
            failures += 1
    return failures


def _find_unmigrated_task_projects() -> list[str]:
    """Local projects with an inline ``.agentwire.yml`` ``tasks:`` block but no
    promoted ``.agentwire.tasks.yml`` (#736).

    This is the concrete #720/#721 regression: the task-split relocated where
    tasks are READ from (``.agentwire.tasks.yml``) without migrating existing
    task DATA, so these projects' scheduled/ensure tasks silently fail (exit 6)
    while their config still *looks* task-bearing. Returns the affected project
    names.
    """
    import yaml

    from . import projects as projects_mod
    from .tasks import TASKS_FILENAME

    unmigrated: list[str] = []
    for proj in projects_mod.get_projects("local"):
        proj_dir = Path(proj["path"])
        cfg_file = proj_dir / ".agentwire.yml"
        try:
            cfg = yaml.safe_load(cfg_file.read_text()) or {}
        except Exception:
            continue
        if cfg.get("tasks") and not (proj_dir / TASKS_FILENAME).exists():
            unmigrated.append(proj["name"])
    return unmigrated


def _render_task_migration_section() -> int:
    """Doctor section: flag projects whose inline tasks never migrated (#736)."""
    unmigrated = _find_unmigrated_task_projects()
    if not unmigrated:
        print("  [ok] No projects with un-migrated inline tasks")
        return 0
    for name in unmigrated:
        print(
            f"  [!!] Project {name}: tasks defined in .agentwire.yml but never "
            "migrated to .agentwire.tasks.yml — they will NOT run under "
            "ensure/scheduler."
        )
    print(
        "     Run `agentwire tasks migrate` then `agentwire tasks promote` "
        "in each affected project."
    )
    return len(unmigrated)


def _find_dead_managed_shims() -> list[dict]:
    """Managed voice shims whose tmux session is alive but ``/health`` is dead.

    The #734 failure mode: a wedged Kokoro (:8102) or Moonshine STT (:8101) shim
    inside a live ``agentwire-kokoro``/``agentwire-stt`` session — say/transcribe
    then silently fall back to browser voice/recognition with no error, and the
    old session-existence-only ``ensure`` never relaunches it. Only the default
    tier runs a managed shim, so we check that tier only, and reuse the same
    ``_shim_session_state`` liveness predicate as ``start`` (SSOT).
    """
    from .config import load_config as load_config_typed
    from .core import (
        get_kokoro_session_name,
        get_stt_session_name,
        tmux_session_exists,
    )
    from .tts_cli import _shim_session_state

    cfg = load_config_typed()
    checks: list[tuple[str, str, int, str, str]] = []

    if getattr(getattr(cfg, "tts", None), "backend", "default") == "default":
        checks.append((
            "Kokoro TTS", get_kokoro_session_name(), 8102,
            "agentwire kokoro start", "say falls back to browser voice",
        ))

    if getattr(getattr(cfg, "stt", None), "backend", "default") == "default":
        # Only the Moonshine path runs a shim; without it the default tier
        # transcribes in the browser and there is nothing to probe.
        from .stt import moonshine_importable

        if moonshine_importable():
            checks.append((
                "Moonshine STT", get_stt_session_name(), 8101,
                "agentwire stt start",
                "transcription falls back to browser recognition",
            ))

    dead: list[dict] = []
    for label, session, port, fix, impact in checks:
        if not tmux_session_exists(session):
            continue
        live, status = _shim_session_state(session, port)
        if not live:
            dead.append({
                "label": label, "session": session, "port": port,
                "status": status, "fix": fix, "impact": impact,
            })
    return dead


def _render_shim_liveness_section() -> int:
    """Doctor section: managed shim session alive but ``/health`` dead (#734)."""
    dead = _find_dead_managed_shims()
    if not dead:
        print("  [ok] No dead-but-present voice shims")
        return 0
    for d in dead:
        print(
            f"  [!!] {d['label']} shim session '{d['session']}' is alive but "
            f":{d['port']}/health is not serving (state: "
            f"{d['status'] or 'no response'}) — {d['impact']}."
        )
    print(
        f"     Self-heal: re-run `{dead[0]['fix']}` — it is now health-aware and "
        "reaps a dead session before relaunching."
    )
    return len(dead)


def find_orphaned_worktrees(rows: list[dict]) -> list[dict]:
    """Registered worktrees still on disk whose owning session is gone (#837).

    ``rows`` are registry entries (``worktree_registry.all_entries()``). An
    orphan is a directory (and usually a branch) that outlived the session
    that made it, so nothing is left to notice it — the failure mode
    ``agentwire spawn --branch`` used to guarantee by never registering at
    all. Read-only: reports, never removes (``worktree --prune``/``--remove``
    own that, with the #756 merged-branch guards).

    A "pane"-topology entry keys on its OWNING session, so it is only an
    orphan once that whole session is gone — an idle-reaped worker pane
    inside a still-live session is normal, not an orphan.
    """
    orphans = []
    for r in rows:
        wt_path = r.get("worktree_path") or ""
        if not wt_path or not Path(wt_path).exists():
            continue  # stale entry, not an orphan — `--prune` sweeps those
        if tmux_session_exists(r.get("session", "")):
            continue
        orphans.append({
            "session": r.get("session"), "branch": r.get("branch"),
            "project": r.get("project"), "worktree_path": wt_path,
            "topology": r.get("topology") or "worktree",
        })
    return orphans


def _render_orphaned_worktrees_section() -> int:
    """Doctor section: on-disk worktrees whose session is dead (#837)."""
    from . import worktree_registry

    orphans = find_orphaned_worktrees(worktree_registry.all_entries())
    if not orphans:
        print("  [ok] No orphaned worktrees found")
        return 0
    print(f"  [!!] {len(orphans)} registered worktree(s) on disk with no live session:")
    for o in orphans:
        tag = " [pane worker]" if o["topology"] == "pane" else ""
        print(f"       - {o['session']} branch={o['branch']}{tag}")
        print(f"         {o['worktree_path']}")
    print("       Review with `agentwire worktree --list --all`, then tear down with "
          "`agentwire worktree --remove <name> -p <repo>` (merged-branch guards apply).")
    return len(orphans)


def scan_orphaned_history(sessions: list[str] | None = None) -> list[dict]:
    """Recorded sessions whose conversation is intact but unreachable (#871).

    Claude keys conversation history by cwd (``~/.claude/projects/<encoded-cwd>/``),
    so a session whose directory MOVED has a transcript that still exists and
    can no longer be found: ``--resume`` from the new location reports
    ``No conversation found with session ID``. That is a distinct state from
    "history gone", and only this one is recoverable — by migrating the
    history dir alongside the worktree.

    Two ways in, one rule. The key we compare against is where the session
    RUNS: its live pane cwd when it's up (that's what a moved directory
    changes, and tmux is the authority on it — the same ask-don't-assume rule
    #837 put on worktree paths), else the recorded ``cwd_at_launch``.

    **This SURVEYS the whole chain; ``restart`` stops at the first hit.** They
    share ``history.locate_conversation`` — one predicate — but they are asking
    different questions, and collapsing them was a real bug: restart wants
    "the newest resumable id" (first match wins, correct for it), and the
    moment a restart created a fresh conversation with a transcript, an older
    orphaned link became unreachable to a first-match scan. Doctor went quiet
    one turn after the restart, with both orphaned transcripts still on disk —
    silence for exactly the user who did the natural thing. So every link in
    ``conversation_ids`` is probed here, and any orphan among them is reported
    whether or not a later link resumes.

    Returns both non-resumable states, tagged by ``status``, because only one
    of them is a fault:

    - ``orphaned`` — the fault. Reported and counted. ``current`` says whether
      the session's newest conversation is the affected one (nothing resumes)
      or whether it is an earlier link (the session works; stranded history is
      sitting next to it).
    - ``gone`` — no history anywhere, and this cannot say WHY: a session that
      was never prompted has no transcript because none was ever written
      (lazy creation), and Claude also evicts from its own cache
      (``~/.claude/projects/`` was observed dropping 563 -> 544 in ~25min with
      ``cleanupPeriodDays`` unset, cause undetermined). Reported for LIVE
      sessions only, as information, never as an issue — a live session is one
      you might actually restart, whereas counting every dead record would
      flag hundreds of them, most of which nobody will ever relaunch.
    """
    from .core import load_session_metadata, recorded_sessions, tmux_session_cwd
    from .history import locate_conversation

    findings: list[dict] = []
    for name in sessions if sessions is not None else recorded_sessions():
        metadata = load_session_metadata(name)
        ids = list(metadata.get("conversation_ids") or [])
        recorded_cwd = metadata.get("cwd_at_launch")
        if not ids or not recorded_cwd:
            continue  # nothing recorded to check (pre-#871 session record)

        live = tmux_session_exists(name)
        running_in = (tmux_session_cwd(name) if live else None) or recorded_cwd

        # Newest first, mirroring the order restart walks — so "the current
        # conversation" is the first link, and the orphans read in the order
        # anyone would care about them.
        located = [locate_conversation(cid, running_in) for cid in reversed(ids)]
        orphans = [loc for loc in located if loc.status == "orphaned"]
        resumable = next((loc for loc in located if loc.resumable), None)

        common = {
            "session": name,
            "running_in": running_in,
            "recorded_cwd": recorded_cwd,
            "moved": str(Path(running_in)) != str(Path(recorded_cwd)),
            "live": live,
        }

        if orphans:
            findings.append({
                **common,
                "status": "orphaned",
                "conversation_id": orphans[0].conversation_id,
                "expected_dir": str(orphans[0].expected_dir),
                "found_at": str(orphans[0].elsewhere[0]),
                "orphaned_ids": [loc.conversation_id for loc in orphans],
                # Does the session's CURRENT conversation resume? An earlier
                # orphaned link is stranded history next to a working session;
                # an orphaned newest link is a session that can't come back.
                "current": resumable is None,
            })
        elif resumable is None and live:
            findings.append({
                **common,
                "status": "gone",
                "conversation_id": located[0].conversation_id,
                "expected_dir": str(located[0].expected_dir),
                "found_at": None,
                "orphaned_ids": [],
                "current": True,
            })
    return findings


def _render_orphaned_history_section() -> int:
    """Doctor section: conversations a restart could not bring back (#871).

    Counts the orphans only; the ``gone`` lines are stated, not scored (see
    :func:`scan_orphaned_history`).
    """
    findings = scan_orphaned_history()
    orphaned = [f for f in findings if f["status"] == "orphaned"]
    gone = [f for f in findings if f["status"] == "gone"]

    if not orphaned:
        print("  [ok] No sessions with orphaned conversation history")
    else:
        print(f"  [!!] {len(orphaned)} session(s) whose conversation history is orphaned:")
        for f in orphaned:
            state = "live" if f["live"] else "not running"
            scope = "current conversation" if f["current"] else "earlier in the chain"
            extra = len(f["orphaned_ids"]) - 1
            print(f"       - {f['session']} ({state}) conversation "
                  f"{f['conversation_id'][:8]} — {scope}"
                  + (f", +{extra} more orphaned" if extra > 0 else ""))
            print(f"         runs in:  {f['running_in']}"
                  + ("   [moved since launch]" if f["moved"] else ""))
            print(f"         history:  {f['found_at']}")
            print(f"         expected: {f['expected_dir']}/")
        print("       Recover with `agentwire history migrate` — the transcripts are "
              "intact, just keyed to the old path. Until then `agentwire restart` on a "
              "'current conversation' one starts FRESH (role intact) rather than "
              "failing, and never silences this line.")

    if gone:
        print(f"  [..] {len(gone)} live session(s) with no conversation history on disk "
              "(never prompted, or evicted by Claude — not determinable here):")
        for f in gone:
            print(f"       - {f['session']} conversation {f['conversation_id'][:8]} "
                  f"(expected {f['expected_dir']}/)")
        print("       Not a fault. Restarting these keeps the role and starts a fresh "
              "conversation.")

    return len(orphaned)


def _render_pending_messages_section() -> int:
    """Doctor section: load-bearing messages queued too long (#879).

    The dead-letter section only sees messages that BURNED OUT. A penalty-free
    defer (``target_parked``, ``target_busy``, …) never dead-letters, so it
    reaches neither that section nor the owner email it triggers. #872 made that
    gap load-bearing by admitting ``target_parked`` — the one defer reason that
    legitimately lasts hours — so a worker's ``done`` can now sit in a parked
    parent's queue indefinitely with nothing announcing it. This is that surface.

    Returns 1 if anything is stale (one issue, not one per message — a parked
    parent strands its whole cohort at once, and that's a single situation).
    """
    from . import inbox

    stale = inbox.stale_pending()
    if not stale:
        print("  [ok] No report-backs pending past the staleness threshold")
        return 0

    hours = inbox.STALE_PENDING_MS / 3_600_000
    now_ms = time.time() * 1000
    print(f"  [!!] {len(stale)} load-bearing message(s) pending over {hours:g}h:")
    for session, m in stale:
        waited = (now_ms - m.ts) / 3_600_000
        print(f"       - To: {session}, Kind: {m.kind}, Sender: {m.sender}, "
              f"Waiting: {waited:.1f}h, Reason: {m.reason or 'not yet attempted'}")
    parked = sorted({s for s, m in stale if m.reason == "target_parked"})
    if parked:
        print(f"       Parked recipient(s): {', '.join(parked)} — these deliver on "
              "their own once the usage limit resets (`agentwire limits status`), "
              "so this is FYI, not a failure.")
    print("       Inspect: `agentwire msg inbox -s <session>`   "
          "Force: `agentwire msg flush -s <session> --force`")
    return 1


def _scheduler_daemon_started_at() -> float | None:
    """Epoch seconds the scheduler daemon process started, or ``None`` if not running.

    Reads the daemon's own self-reported ``started_at`` from the live-state
    file it already writes every loop tick (``scheduler/loop.py`` ->
    ``_write_live_state`` -> ``read_live_state()``) rather than reimplementing
    process-start-time discovery via ``ps`` elapsed-time parsing, which is
    platform-fragile. ``live_daemon_state`` verifies the recorded PID is a live
    scheduler process, so a leftover file from a since-stopped daemon doesn't
    produce a false "stale" reading — and unlike the ``tmux_session_exists``
    gate it replaced (#873), it also sees a daemon supervised outside tmux,
    where skipping this check silently disabled the most useful diagnostic
    exactly when it was needed.
    """
    from .scheduler import live_daemon_state

    state = live_daemon_state()
    if not state:
        return None
    started_at = state.get("started_at")
    if not started_at:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(started_at).timestamp()
    except (ValueError, TypeError):
        return None


def _scheduler_daemon_predates_pid_stamp() -> bool:
    """True when a tmux-hosted daemon is up but its live state records no PID.

    The one transitional state #873 leaves behind: a daemon started before the
    PID stamp existed is alive but unverifiable. tmux is evidence enough to say
    "something is running" here — it just isn't evidence anyone should keep
    inferring liveness from, which is why it lives in this diagnostic rather
    than back in ``live_daemon_state``.
    """
    from .scheduler import SCHEDULER_SESSION, read_live_state

    state = read_live_state()
    if not state or isinstance(state.get("pid"), int):
        return False
    return tmux_session_exists(SCHEDULER_SESSION)


def _newest_installed_source_mtime(pkg_dir: Path | None = None) -> float:
    """Newest mtime among the installed ``agentwire`` package's ``.py`` files."""
    if pkg_dir is None:
        pkg_dir = Path(__file__).resolve().parent
    try:
        return max((f.stat().st_mtime for f in pkg_dir.rglob("*.py")), default=0.0)
    except Exception:
        return 0.0


def _render_scheduler_staleness_section() -> int:
    """Doctor section: flag a scheduler daemon older than the installed code (#803).

    ``agentwire scheduler serve`` is a long-running Python process — like the
    MCP server, it imports its modules once at start and never re-reads them.
    ``agentwire rebuild`` updates the on-disk package but can't touch an
    already-running interpreter's loaded bytecode, so a daemon that has been
    up since before the last rebuild is silently executing stale dispatch
    logic: any bug fixed since then stays live in production until the
    daemon is restarted.

    Also flags the one state where liveness genuinely cannot be determined: a
    daemon started before #873 writes no ``pid``, so nothing can verify it.
    That is itself a stale daemon, and saying so beats reporting it as stopped.
    """
    started_at = _scheduler_daemon_started_at()
    if started_at is None:
        if _scheduler_daemon_predates_pid_stamp():
            print("  [!!] Scheduler daemon records no PID — it predates the "
                  "PID-based liveness check (#873)")
            print("       Liveness and the staleness check below can't be verified "
                  "until it restarts.")
            print("       Fix: agentwire scheduler stop && agentwire scheduler start")
            return 1
        print("  [..] Scheduler daemon not running — skipping staleness check")
        return 0

    newest_src = _newest_installed_source_mtime()
    if not newest_src or newest_src <= started_at:
        print("  [ok] Scheduler daemon is current with the installed package")
        return 0

    age_days = (time.time() - started_at) / 86400
    print(f"  [!!] Scheduler daemon has been running {age_days:.1f}d — "
          "predates the most recent `agentwire rebuild`")
    print("       Any dispatch bug fixed since then is still live in the running daemon.")
    print("       Fix: agentwire scheduler stop && agentwire scheduler start")
    return 1


def _render_role_prompt_store_section(
    *, auto_confirm: bool = False, dry_run: bool = False,
) -> tuple[int, int]:
    """Doctor section: the role-prompt store's size and its aged-out tail (#884).

    ``~/.agentwire/role-prompts/`` grows one file per agent launch, forever —
    and ``spawn`` (the highest-frequency launch path) writes files nothing will
    ever reference again, since a pane has no session-scoped record to name its
    conversation in. See :mod:`agentwire.role_prompts` for why the rule is
    "reachable is forever, unreachable ages out" and not "delete on exit".

    Only the aged-out tail counts as an ISSUE. Unreachable-but-young files are
    the normal steady state (every live pane has one). A tail that has survived
    the TTL means the once-a-day watchdog sweep isn't running, which is the
    thing actually worth reporting — the disk usage is a symptom.

    Returns ``(issues_found, issues_fixed)``.
    """
    from . import core, role_prompts

    store = core.role_prompts_dir()
    sessions_dir = core.CONFIG_DIR / "sessions"
    s = role_prompts.status(store, sessions_dir)

    if not s["exists"]:
        print("  [ok] Role-prompt store not created yet (no agent launched here)")
        return 0, 0

    print(f"  [ok] Role-prompt store: {s['total']} file(s), {s['bytes'] / 1024:.0f} KB "
          f"({s['reachable']} reachable, {s['unreachable']} unreferenced)")
    if s["unrecognized"]:
        print(f"  [..] {len(s['unrecognized'])} unrecognized entr(ies) in the store — "
              "never swept, never deleted: " + ", ".join(s["unrecognized"][:5]))

    if not s["expired"]:
        return 0, 0

    print(f"  [!!] {s['expired']} role prompt(s) unreferenced and older than "
          f"{s['max_age_days']:g}d ({s['expired_bytes'] / 1024:.0f} KB) — the daily "
          "sweep does not appear to be running")
    print("       Fix: agentwire limits install   (the watchdog owns this sweep)")
    if dry_run:
        print("       -> Would sweep them now (dry-run)")
        return 1, 0
    if not (auto_confirm or _confirm("     Sweep them now?")):
        return 1, 0
    result = role_prompts.sweep(store, sessions_dir)
    print(f"       -> swept {len(result['deleted'])} file(s), "
          f"{result['bytes_freed'] / 1024:.0f} KB freed")
    if result["failed"]:
        print(f"       -> {len(result['failed'])} could not be removed: "
              + "; ".join(result["failed"][:3]))
        return 1, 0
    return 1, 1


def _render_blocked_prompt_section() -> int:
    """Doctor section: agent alive, but the pane sits on an unanswered menu (#905).

    This state is invisible to every other surface we have, and that is the
    whole point of the check. The agent process is running, so
    ``pane_current_command`` reports the agent and every liveness probe —
    ``worktree --list``, the idle handler, the fleet roll-up — calls the
    session healthy. It is doing nothing, and ``safe_deliver`` correctly
    refuses to paste into a live menu, so every message queues behind it.
    Four sessions sat like that for hours (one about four) and the owner
    reported "13 sessions recovered" on exactly that basis.

    A blocked pane is NOT automatically a problem: a prompt routed to a parent
    a minute ago is the system working. Only the ones nobody can act on count
    as issues — never routed at all (so the sweep isn't running), or waiting
    past :data:`prompt_router.STUCK_PROMPT_AFTER`.

    Read-only: detects and reports, never answers a dialog or writes a marker.
    """
    from . import prompt_router

    blocked = prompt_router.blocked_panes()
    if blocked is None:
        print("  [..] tmux not reachable — cannot inspect panes")
        return 0
    if not blocked:
        print("  [ok] No session is sitting on an unanswered prompt")
        return 0

    stuck = [b for b in blocked if b["stuck"]]
    for b in blocked:
        where = f"{b['session']} pane {b['pane']}"
        waited = (
            f"{b['waiting_minutes']}m"
            if b["waiting_minutes"] is not None else "unknown"
        )
        if not b["stuck"]:
            print(f"  [ok] {where}: {b['kind']} prompt routed to "
                  f"{b['parent']} ({b['status']}, {waited})")
            continue
        if b["status"] == "detector_error":
            # A crashing detector must never read as a healthy pane — that is
            # the blind spot this whole section exists to close, so the check
            # reports its own.
            print(f"  [!!] {where}: the prompt detector RAISED on this pane — "
                  "its state is unknown, and the sweep cannot route it")
            print(f"       {b['error']}")
            print("       Grep the sweep's record: "
                  "grep detect_failed ~/.agentwire/prompt-router-events.jsonl")
            print(f"       agentwire output -s '{b['session']}'   # read it yourself")
            continue
        print(f"  [!!] {where}: blocked on a {b['kind']} prompt for {waited} "
              f"— {b['status']}")
        print(f"       {b['question']}" + (f"  ({b['summary']})" if b["summary"] else ""))
        if b["status"] == "unrouted":
            print("       Nobody has been notified. Fix: agentwire limits install "
                  "(the watchdog runs the prompt sweep)"
                  + ("  [session is in prompt_router.exclude_sessions]"
                     if b["excluded"] else ""))
        elif b["status"] == "no_parent":
            print("       Root session — no parent to route to; the owner was "
                  "emailed. Answer it yourself:")
        else:
            print(f"       Routed to {b['parent']}, still unanswered. Answer it "
                  "yourself:")
        print(f"       agentwire output -s '{b['session']}'   # inspect first")
        print(f"       agentwire prompts answer -s '{b['session']}' "
              f"--pane {b['pane']} --expect <hash> <key>")

    return len(stuck)


def _render_mcp_import_section() -> int:
    """Doctor section: does the MCP server entrypoint actually import? (#874)

    ``agentwire mcp`` is launched by Claude Code, not by agentwire, so an
    import-time failure never surfaces as an agentwire error — the client
    reports a connection close and the agent in the session just finds its
    ``mcp__agentwire__*`` tools missing. #874 was exactly this: an unbounded
    ``mcp>=1.2.0`` let a rebuild resolve SDK 2.x, which dropped the
    ``mcp.server.fastmcp`` module ``mcp_core`` imports, and ``rebuild`` still
    printed success. Import the real entrypoint chain in a subprocess so a
    broken server is reported by the tool that is supposed to notice.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import agentwire.mcp_server"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode == 0:
        print("  [ok] MCP server entrypoint imports cleanly")
        return 0

    err = (proc.stderr or "").strip().splitlines()
    print("  [!!] MCP server entrypoint fails to import — every mcp__agentwire__* "
          "tool is missing from every session")
    if err:
        print(f"       {err[-1]}")
    print("       Verify with: claude mcp list   (expect 'agentwire: ✘ Failed to connect')")
    print("       Usually a dependency resolution: check the bounds in pyproject.toml, "
          "then `agentwire rebuild`.")
    return 1


@dataclass
class _SecretPath:
    """An agentwire path that must never be readable by anyone but its owner."""
    path: Path
    mode: int          # what it is right now (permission bits only)
    want: int          # what it should be
    why: str           # what leaks if it stays wide


def _owner_only_paths() -> list[tuple[Path, int, str]]:
    """(path, required mode, what it holds) for every owner-only path.

    Resolved at CALL time from ``core.CONFIG_DIR`` / ``security.TOKEN_FILE``
    rather than captured at import, so the single source of truth for each
    location stays the module that owns it.
    """
    from .core import CONFIG_DIR
    from .security import TOKEN_FILE

    return [
        (CONFIG_DIR, 0o700,
         "every file below it — a readable dir leaks the filenames"),
        (CONFIG_DIR / ".env", 0o600,
         "every API key (docs/wiki/security/secrets.md)"),
        (TOKEN_FILE, 0o600,
         "the portal auth token — full access to every session"),
        (CONFIG_DIR / "machines.json", 0o600,
         "remote hosts, users and paths"),
    ]


def _overly_permissive_secret_paths() -> list[_SecretPath]:
    """The owner-only paths that are currently readable by group or world.

    Only GROUP/OTHER bits are judged. A path that is TIGHTER than required
    (0400, say) is deliberately not reported: this check exists to close a
    file, never to open one.
    """
    found = []
    for path, want, why in _owner_only_paths():
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue  # absent (or unreadable) — nothing to judge
        if mode & 0o077:
            found.append(_SecretPath(path=path, mode=mode, want=want, why=why))
    return found


def _render_secrets_permissions_section(
    *, auto_confirm: bool = False, dry_run: bool = False,
) -> tuple[int, int]:
    """Doctor section: is the secrets file actually 0600? (#887)

    ``chmod 600 ~/.agentwire/.env`` was documented in two places and enforced
    in none — a manual step a human was expected to remember. The machine this
    check was written on had it at 0644, world-readable, holding every API key,
    and no diagnostic anywhere said so. Doctor already flags stale hooks,
    drifted rules and a disabled kill switch; a world-readable key file belongs
    in the same list.

    Healing is opt-in (``--yes`` / the interactive prompt) rather than
    automatic, unlike the forced ``chmod`` on agentwire's OWN writes: tightening
    a file is safe in a way loosening never is, but these are the operator's
    files on the operator's machine. Returns ``(issues_found, issues_fixed)``.
    """
    issues = _overly_permissive_secret_paths()
    if not issues:
        print("  [ok] Secrets and registry files are owner-only (0600/0700)")
        return 0, 0

    fixed = 0
    for item in issues:
        kind = "dir" if item.path.is_dir() else "file"
        print(f"  [!!] {item.path} is {item.mode:04o} — readable beyond its "
              f"owner ({kind} holds {item.why})")
        print(f"       Fix: chmod {item.want:03o} {item.path}")
        if dry_run:
            print(f"       -> Would chmod {item.want:03o} (dry-run)")
            continue
        if not (auto_confirm or _confirm(f"     Tighten to {item.want:03o}?")):
            continue
        try:
            os.chmod(item.path, item.want)
        except OSError as e:
            print(f"       -> chmod failed: {e}")
            continue
        print(f"       -> tightened to {item.want:03o}")
        fixed += 1
    return len(issues), fixed


def cmd_doctor(args) -> int:
    """Auto-diagnose and fix common issues."""
    from .hooks_cli import _managed_file_state, _managed_hook_files, get_hooks_source
    from .network import NetworkContext
    from .tunnels import TunnelManager, test_service_health, test_ssh_connectivity
    from .validation import validate_config

    dry_run = getattr(args, 'dry_run', False)
    auto_confirm = getattr(args, 'yes', False)
    voice_only = getattr(args, 'voice', False)

    print("AgentWire Doctor")
    print("=" * 60)

    issues_found = 0
    issues_fixed = 0

    # --voice: run ONLY the end-to-end push-to-talk preflight (fast, no SSH
    # waits) — for tight first-run / break-a-dependency verification loops.
    if voice_only:
        try:
            from .config import load_config as load_config_typed
            config = load_config_typed()
        except Exception as e:
            print(f"\n  [!!] Config file error: {e}")
            print("     Run: agentwire init")
            return 1
        try:
            ctx = NetworkContext.from_config()
        except Exception:
            ctx = None
        issues_found = _render_voice_loop_section(config, ctx)
        print()
        print("-" * 60)
        print("Voice loop ready!" if issues_found == 0
              else f"Voice loop: {issues_found} stage(s) need attention")
        return 0 if issues_found == 0 else 1

    # 1. Check Python version
    print("\nChecking Python version...")
    py_version = sys.version_info
    version_str = f"{py_version.major}.{py_version.minor}.{py_version.micro}"
    if py_version >= (3, 10):
        print(f"  [ok] Python {version_str} (>=3.10 required)")
    else:
        print(f"  [!!] Python {version_str} (>=3.10 required)")
        print("     macOS: pyenv install 3.12.0 && pyenv global 3.12.0")
        print("     Ubuntu: sudo apt update && sudo apt install python3.12")
        issues_found += 1

    # 2. Check system dependencies
    print("\nChecking system dependencies...")

    # Check tmux (required)
    tmux_path = shutil.which("tmux")
    if tmux_path:
        print(f"  [ok] tmux: {tmux_path}")
        # Server-side options that make or break the agent UX (warn-only —
        # config is user preference, not a broken install). Only checkable
        # when a tmux server is running.
        for opt, why in (
            ("focus-events", "Claude Code shows a setup tip on every session start"),
            ("mouse", "no mouse scroll or text selection in agent panes"),
        ):
            val = _tmux_global_option(opt)
            if val is None:
                break  # no running tmux server — nothing to inspect
            if val == "on":
                print(f"  [ok] tmux {opt}: on")
            else:
                print(f"  [..] tmux {opt}: {val} — {why}")
                print("     Recommended config: agentwire init (tmux step), or see")
                print("     docs/wiki/quickstart.md#recommended-tmux-config")
    else:
        print("  [!!] tmux: not found (required)")
        print("     macOS: brew install tmux")
        print("     Ubuntu: sudo apt install tmux")
        issues_found += 1

    # Check ffmpeg (optional)
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        print(f"  [ok] ffmpeg: {ffmpeg_path}")
    else:
        print("  [..] ffmpeg: not found (optional — needed for host-mic push-to-talk; browser voice input works without it)")
        print("     macOS: brew install ffmpeg")
        print("     Ubuntu: sudo apt install ffmpeg")

    # Check Claude Code (optional)
    claude_path = shutil.which("claude")
    if claude_path:
        print(f"  [ok] claude: {claude_path}")
    else:
        print("  [..] claude: not found (optional, use --bare sessions)")
        print("     Install: https://github.com/anthropics/claude-code")

    # 3. Check AgentWire scripts
    print("\nChecking AgentWire scripts...")

    say_path = shutil.which("say")
    if say_path:
        print(f"  [ok] say: {say_path}")
    else:
        print("  [..] say: not found (optional, use 'agentwire say' directly)")

    # 4. Check agentwire-owned hook files (existence AND drift from packaged source)
    print("\nChecking AgentWire hooks...")

    hook_meta = {
        "agentwire-permission.sh": ("Permission hook", False,
                                    "optional for prompted sessions"),
        "idle-handler.sh": ("Idle notification hook", True,
                            "required for worker notifications and scheduled tasks"),
        "queue-processor.sh": ("Queue processor", True,
                               "required for notification queuing"),
    }
    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        hooks_source = None

    for hook_name, target_dir, _event in _managed_hook_files():
        label, required, why = hook_meta[hook_name]
        target = target_dir / hook_name
        source = hooks_source / hook_name if hooks_source else None
        state = _managed_file_state(target, source) if source and source.exists() \
            else ("ok" if target.exists() else "missing")

        if state == "ok":
            print(f"  [ok] {label}: {target}")
        elif state == "stale":
            print(f"  [!!] {label}: STALE — installed copy differs from packaged source")
            print("     Run: agentwire hooks install")
            issues_found += 1
        elif required:
            print(f"  [!!] {label}: not found ({why})")
            print("     Run: agentwire hooks install")
            issues_found += 1
        else:
            print(f"  [..] {label}: not found ({why})")
            print("     Run: agentwire hooks install")

    # 4a-bis. Global skills (currently just /wiki). Hand-placed at wiki-setup and
    # never resynced, so a stale or missing copy was invisible until #475. Flag
    # the same way as hooks — drift-aware against the packaged source.
    print("\nChecking AgentWire global skills...")
    issues_found += _render_skill_section()

    # 4b. Damage control (safety) — the kill switch, install drift, and the
    # PreToolUse matcher registration. The #462 incident: a global disable and
    # missing rule files were both invisible to every diagnostic.
    print("\nChecking damage control (safety)...")
    issues_found += _render_damage_control_section()

    # 4c. Local checkout vs origin/main — rebuild reinstalls whatever is checked
    # out, so a never-pulled main silently ships stale code. Today only worktree
    # creation fetches; surface the drift here too.
    print("\nChecking source checkout...")
    src_root = Path(__file__).parent.parent
    if not (src_root / "pyproject.toml").exists():
        try:
            src_root = get_source_dir()
        except Exception:
            src_root = None
    if src_root is None:
        print("  [..] Could not locate source checkout — skipping git-drift check")
    else:
        behind, err = _git_behind_origin(src_root)
        if err:
            print(f"  [..] Source checkout: skipped git-drift check ({err})")
        elif behind and behind > 0:
            print(f"  [!!] Local main is {behind} commit(s) behind origin/main")
            print(f"       {src_root}")
            print("       Fix: git pull --ff-only  (then agentwire rebuild)")
            issues_found += 1
        else:
            print("  [ok] Local main up to date with origin/main")

    # 4d. Scheduler daemon staleness — the daemon is a long-running process
    # that never re-imports its code, so it can silently run a dispatch bug
    # that's since been fixed on disk (#803). Distinct from the git-drift
    # check above: that flags an out-of-date CHECKOUT, this flags a
    # currently-running PROCESS that predates the last install.
    print("\nChecking scheduler daemon freshness...")
    issues_found += _render_scheduler_staleness_section()

    # 4e. MCP server entrypoint — a dependency resolution can break it on a
    # rebuild and the failure only ever shows up client-side (#874).
    print("\nChecking MCP server entrypoint (#874)...")
    issues_found += _render_mcp_import_section()

    # Check custom services (registry-driven: built-in notifications bridge
    # + user-defined services from services.custom)
    print("\nChecking custom services...")
    from . import services as services_mod
    from .config import load_config as _load_config_typed
    try:
        _svc_cfg = _load_config_typed()
        _svc_disabled = services_mod.load_disabled()
        for svc in services_mod.registry(_svc_cfg):
            healthy, detail = services_mod.run_healthcheck(svc)
            if healthy:
                print(f"  [ok] Service {svc.name}: {detail}")
            elif svc.name in _svc_disabled:
                print(f"  [..] Service {svc.name}: stopped via 'services down' ({detail})")
            elif not svc.autostart:
                print(f"  [..] Service {svc.name}: not running (autostart off, {detail})")
            else:
                print(f"  [!!] Service {svc.name}: unhealthy — {detail}")
                print(f"     Run: agentwire services up {svc.name}")
                issues_found += 1
    except Exception as e:
        print(f"  [..] Could not check custom services: {e}")

    # 5. Validate config
    print("\nChecking configuration...")
    try:
        from .config import load_config as load_config_typed
        config = load_config_typed()
        print("  [ok] Config file valid")
    except Exception as e:
        print(f"  [!!] Config file error: {e}")
        print("     Run: agentwire init")
        issues_found += 1
        return 1  # Can't proceed without valid config

    machines_file = config.machines.file
    warnings, errors = validate_config(config, machines_file)

    if not errors:
        print("  [ok] Machines.json valid")
    else:
        for err in errors:
            print(f"  [!!] {err.message}")
            issues_found += 1

    if not warnings:
        print("  [ok] All config checks passed")
    else:
        for warn in warnings:
            print(f"  [..] {warn.message}")

    # `agentwire config set` allowlist must never contain execution-plane
    # keys — that would reopen the #466 confused-deputy hole (#670).
    from .config_cli import execution_plane_violations
    _cfg_violations = execution_plane_violations()
    if not _cfg_violations:
        print("  [ok] config-set allowlist contains no execution-plane keys")
    else:
        for _bad in _cfg_violations:
            print(f"  [!!] config-set allowlist contains execution-plane key: {_bad}")
        print("     Fix: remove the key from EDITABLE_KEYS in agentwire/config_cli.py")
        issues_found += len(_cfg_violations)

    # 6. Check SSH connectivity
    print("\nChecking SSH connectivity...")
    ctx = NetworkContext.from_config()

    for machine_id, machine in ctx.machines.items():
        if machine_id == ctx.local_machine_id:
            continue

        host = machine.get("host", machine_id)
        user = machine.get("user")

        latency = test_ssh_connectivity(host, user, timeout=5)
        if latency is not None:
            print(f"  [ok] {machine_id}: reachable ({latency}ms)")
        else:
            print(f"  [!!] {machine_id}: unreachable")
            issues_found += 1

    # 7. Check/create tunnels
    print("\nChecking tunnels...")
    tm = TunnelManager()
    required_tunnels = ctx.get_required_tunnels()

    if not required_tunnels:
        print("  [ok] No tunnels required (services are local)")
    else:
        for spec in required_tunnels:
            status = tm.check_tunnel(spec)

            if status.status == "up":
                print(f"  [ok] localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port} (PID {status.pid})")
            else:
                print(f"  [!!] Missing: localhost:{spec.local_port} -> {spec.remote_machine}:{spec.remote_port}")
                issues_found += 1

                if not dry_run:
                    if auto_confirm or _confirm("     Create tunnel?"):
                        print("     -> Creating tunnel...", end=" ", flush=True)
                        result = tm.create_tunnel(spec, ctx)
                        if result.status == "up":
                            print(f"[ok] created (PID {result.pid})")
                            issues_fixed += 1
                        else:
                            print(f"[!!] failed: {result.error}")
                else:
                    print("     -> Would create tunnel (dry-run)")

    # 8. Check services
    print("\nChecking services...")

    # Default-tier TTS has no service to health-check
    tts_config = load_config().get("tts", {})
    tts_backend = tts_config.get("backend", "default")

    for service_name in ["portal", "tts"]:
        if service_name == "tts" and tts_backend != "custom":
            print("  [ok] Tts: default tier (browser/OS voice, no local service needed)")
            continue

        service_config = getattr(ctx.config.services, service_name, None)
        if service_config is None:
            continue

        url = ctx.get_service_url(service_name)
        health_url = f"{url}{service_config.health_endpoint}"
        is_healthy, error = test_service_health(health_url, timeout=3)

        if is_healthy:
            print(f"  [ok] {service_name.capitalize()}: responding on {url}")
        else:
            print(f"  [!!] {service_name.capitalize()}: not responding on {url}")
            if error:
                print(f"       Error: {error}")
            issues_found += 1

            # Only try to fix if service is local
            if ctx.is_local(service_name):
                if not dry_run:
                    if auto_confirm or _confirm(f"     Start {service_name}?"):
                        print(f"     -> Starting {service_name}...", end=" ", flush=True)

                        if service_name == "portal":
                            session_name = get_portal_session_name()
                            if tmux_session_exists(session_name):
                                print("[ok] already running in tmux")
                            else:
                                subprocess.run(
                                    ["tmux", "new-session", "-d", "-s", session_name],
                                    capture_output=True,
                                )
                                subprocess.run(
                                    ["tmux", "send-keys", "-t", session_name, "agentwire portal serve", "Enter"],
                                    capture_output=True,
                                )
                                print("[ok] started")
                                issues_fixed += 1

                        elif service_name == "tts":
                            session_name = get_tts_session_name()
                            if tmux_session_exists(session_name):
                                print("[ok] already running in tmux")
                            else:
                                subprocess.run(
                                    ["tmux", "new-session", "-d", "-s", session_name],
                                    capture_output=True,
                                )
                                subprocess.run(
                                    ["tmux", "send-keys", "-t", session_name, "agentwire tts serve", "Enter"],
                                    capture_output=True,
                                )
                                print("[ok] started")
                                issues_fixed += 1
                else:
                    print(f"     -> Would start {service_name} (dry-run)")
            else:
                print(f"     -> Service is remote, start it on {service_config.machine}")

    # 8b. Voice loop (push-to-talk) — walk the live PTT path end to end and
    # report each stage pass/fail with a fix line when red: mic/audio capture,
    # the STT shim (incl. the default-tier Moonshine :8101 shim, invisible to
    # the services loop), portal/tunnel reachability, and tmux+PTT wiring. This
    # subsumes the old standalone STT probe — the STT stage covers all tiers.
    issues_found += _render_voice_loop_section(config, ctx)

    # 8c. Secrets — for each configured feature that needs a key, report
    # whether its env var is present. Names only, never values. Keys live in
    # ~/.agentwire/.env (docs/wiki/security/secrets.md), loaded on every
    # entry point, so os.environ is the authoritative view here.
    raw_cfg = load_config()
    expected_keys: list[tuple[str, list[str]]] = []
    channels_cfg = raw_cfg.get("channels", {}) or {}
    if channels_cfg.get("email"):
        expected_keys.append(("channels.email (Resend)", ["RESEND_API_KEY"]))
    if channels_cfg.get("quo"):
        expected_keys.append(("channels.quo (OpenPhone)", ["QUO_API_KEY"]))
    if expected_keys:
        print("\nChecking secrets (~/.agentwire/.env)...")
        for feature, candidates in expected_keys:
            found = next((v for v in candidates if os.environ.get(v)), None)
            if found:
                print(f"  [ok] {feature}: {found} is set")
            else:
                print(f"  [!!] {feature}: {' / '.join(candidates)} not set")
                print(f"       Fix: add {candidates[0]}=... to ~/.agentwire/.env")
                issues_found += 1

    # 8d. Secrets-file PERMISSIONS (#887) — the keys being present says nothing
    # about who else can read them. Unconditional (unlike the key-presence
    # block above, which only runs for configured features): the config dir and
    # its registry exist on every install.
    print("\nChecking secrets file permissions...")
    _perm_found, _perm_fixed = _render_secrets_permissions_section(
        auto_confirm=auto_confirm, dry_run=dry_run)
    issues_found += _perm_found
    issues_fixed += _perm_fixed

    # 9. Validate remote machines
    print("\nChecking remote machines...")
    remote_machines = {mid: m for mid, m in ctx.machines.items() if mid != ctx.local_machine_id}

    if not remote_machines:
        print("  [ok] No remote machines configured")
    else:
        for machine_id, machine in remote_machines.items():
            host = machine.get("host", machine_id)
            user = machine.get("user")
            target = f"{user}@{host}" if user else host

            print(f"\n  {machine_id}:")

            # Check SSH connectivity (already done above, but include latency here)
            latency = test_ssh_connectivity(host, user, timeout=5)
            if latency is not None:
                print(f"    [ok] SSH connectivity ({latency}ms)")
            else:
                print("    [!!] SSH connectivity failed")
                print(f"         Fix: ssh {target}")
                issues_found += 1
                continue  # Can't check further if SSH fails

            # Check if agentwire is installed
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "agentwire --version"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    version = result.stdout.strip()
                    print(f"    [ok] agentwire installed ({version})")
                else:
                    print("    [!!] agentwire not installed")
                    print(f"         Fix: ssh {target} 'pip install agentwire-dev'")
                    issues_found += 1
                    continue
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [!!] agentwire not installed")
                print(f"         Fix: ssh {target} 'pip install agentwire-dev'")
                issues_found += 1
                continue

            # Check portal_url file
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "cat ~/.agentwire/portal_url"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    portal_url = result.stdout.strip()
                    print(f"    [ok] portal_url set ({portal_url})")
                else:
                    print("    [!!] portal_url not set")
                    print(f"         Fix: ssh {target} 'echo \"https://localhost:8765\" > ~/.agentwire/portal_url'")
                    issues_found += 1
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [!!] portal_url not set")
                print(f"         Fix: ssh {target} 'echo \"https://localhost:8765\" > ~/.agentwire/portal_url'")
                issues_found += 1

            # Test say command (optional)
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, "which say"],
                    capture_output=True,
                    text=True,
                    timeout=7,
                )
                if result.returncode == 0:
                    print("    [ok] say command available")
                else:
                    print("    [..] say: not found (optional, use 'agentwire say' directly)")
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                print("    [..] say: not found (optional, use 'agentwire say' directly)")

    # 10. Check for dead-lettered messages
    print("\nChecking for dead-lettered messages...")
    from agentwire import inbox
    try:
        dead_sess = inbox.dead_sessions()
        done_or_esc_found = False
        if not dead_sess:
            print("  [ok] No dead-lettered messages found")
        else:
            for ds in dead_sess:
                dead_msgs = inbox.list_dead(ds)
                done_or_esc = [m for m in dead_msgs if m.kind in ("done", "escalation")]
                if done_or_esc:
                    done_or_esc_found = True
                    print(f"  [!!] Session '{ds}' has {len(done_or_esc)} dead-lettered report-back/escalation message(s):")
                    for m in done_or_esc:
                        print(f"       - Kind: {m.kind}, Sender: {m.sender}, Text: {m.text}, Reason: {m.reason}")
                    issues_found += 1
            if not done_or_esc_found:
                print("  [ok] No dead-lettered done/escalation messages found (any dead-letters are informational)")
    except Exception as e:
        print(f"  [..] Could not check dead-lettered messages: {e}")

    # 10b. Long-pending report-backs (#879) — see _render_pending_messages_section.
    print("\nChecking for long-pending report-backs...")
    issues_found += _render_pending_messages_section()

    # 11. Check for dangling worktree sessions (live, open PR, no live parent
    # to review/merge it — #716's concrete failure mode: a rootless-but-
    # still-subordinate session that correctly refuses to self-merge).
    print("\nChecking for dangling worktree sessions...")
    try:
        from . import worktree_registry
        from .session_cli import scan_dangling_worktrees

        dangling = scan_dangling_worktrees(worktree_registry.all_entries())
        if not dangling:
            print("  [ok] No dangling worktree sessions found")
        else:
            issues_found += 1
            print(f"  [!!] {len(dangling)} live worktree session(s) with an open PR and no live parent:")
            for d in dangling:
                print(f"       - {d['session']} branch={d['branch']} {d.get('pr_url', '')} ({d['reason']})")
            print("       Assign a parent (agentwire msg send / --created-by) or merge/close the PR yourself.")
    except Exception as e:
        print(f"  [..] Could not check for dangling worktree sessions: {e}")

    # 11b. Registered worktrees whose owning session is dead but whose
    # directory (and branch) survive on disk (#837). Every creation site now
    # registers, so this sweep finally sees ALL of them — including the worker
    # worktrees `agentwire spawn --branch` used to create invisibly.
    print("\nChecking for orphaned worktrees (#837)...")
    try:
        issues_found += _render_orphaned_worktrees_section()
    except Exception as e:
        print(f"  [..] Could not check for orphaned worktrees: {e}")

    # 11c. Sessions whose Claude conversation is intact but keyed to a cwd they
    # no longer run in (#871) — a moved directory strands the transcript where
    # `--resume` will never look. Distinct from "history gone" (a cache Claude
    # owns and evicts), which this deliberately stays quiet about.
    print("\nChecking for orphaned conversation history (#871)...")
    try:
        issues_found += _render_orphaned_history_section()
    except Exception as e:
        print(f"  [..] Could not check for orphaned conversation history: {e}")

    # 12. Projects whose inline .agentwire.yml tasks were never migrated to the
    # promoted .agentwire.tasks.yml (#736). The #720/#721 task-split moved where
    # tasks are read from without migrating the data, so these tasks silently
    # fail (exit 6) under ensure/scheduler — the exact regression that went
    # undetected because nothing surfaced it. Loud here so it can't hide again.
    print("\nChecking project task migration (#736)...")
    try:
        issues_found += _render_task_migration_section()
    except Exception as e:
        print(f"  [..] Could not check project task migration: {e}")

    # 12b. Role-prompt store retention (#884) — one file per agent launch,
    # forever, with panes writing guaranteed orphans. Reports size and flags
    # only the aged-out tail (which means the watchdog sweep isn't running).
    print("\nChecking role-prompt store retention (#884)...")
    try:
        _rp_found, _rp_fixed = _render_role_prompt_store_section(
            auto_confirm=auto_confirm, dry_run=dry_run)
        issues_found += _rp_found
        issues_fixed += _rp_fixed
    except Exception as e:
        print(f"  [..] Could not check the role-prompt store: {e}")

    # 12c. Sessions blocked on an unanswered dialog (#905). The one state every
    # other check calls healthy: the agent process is running, so liveness
    # passes, while the pane sits on a menu and does nothing.
    print("\nChecking for sessions blocked on a prompt (#905)...")
    try:
        issues_found += _render_blocked_prompt_section()
    except Exception as e:
        print(f"  [..] Could not check for blocked sessions: {e}")

    # 13. Managed voice shims (Kokoro :8102, Moonshine STT :8101) whose tmux
    # session is alive but whose /health is dead (#734). The old
    # session-existence-only idempotency masked a wedged engine forever, so
    # say/transcribe silently fell back to browser voice. Surfaced distinctly
    # here with the now-health-aware self-heal command.
    print("\nChecking managed voice shim liveness (#734)...")
    try:
        issues_found += _render_shim_liveness_section()
    except Exception as e:
        print(f"  [..] Could not check managed voice shim liveness: {e}")

    # 14. Zombie scheduler sessions (#739): a worktree dispatch whose launch
    # crashed before the agent started (e.g. the worktree dir went missing
    # between `agentwire new` reporting success and the pane's `cd`) drops to
    # a bare shell the idle-reaper never touches. `agentwire limits tick`
    # self-heals these every minute, so a live one here means the watchdog
    # isn't installed/running, not that reaping is broken.
    print("\nChecking for zombie scheduler sessions (#739)...")
    try:
        from .scheduler import scan_zombie_sessions

        zombie_sessions = scan_zombie_sessions()
        if not zombie_sessions:
            print("  [ok] No zombie scheduler sessions found")
        else:
            issues_found += 1
            print(f"  [!!] {len(zombie_sessions)} scheduler session(s) stuck at a bare shell:")
            for z in zombie_sessions:
                print(f"       - {z['session']} ({z['command']}, {z['age_seconds']}s old)")
            print("       `agentwire limits tick` reaps these automatically — "
                  "run it now, or check the usage-limit watchdog is installed "
                  "(`agentwire limits install`).")
    except Exception as e:
        print(f"  [..] Could not check for zombie scheduler sessions: {e}")

    # Summary
    print()
    print("-" * 60)
    if issues_found == 0:
        print("All checks passed!")
    elif issues_fixed == issues_found:
        print(f"All issues resolved! ({issues_fixed} fixed)")
    elif issues_fixed > 0:
        print(f"Fixed {issues_fixed} of {issues_found} issues")
    else:
        print(f"Found {issues_found} issues")

    return 0 if issues_found == issues_fixed else 1


def _confirm(prompt: str) -> bool:
    """Ask for user confirmation."""
    try:
        response = input(f"{prompt} [y/N] ").strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def register_doctor_parser(subparsers) -> None:
    # === network command group ===
    network_parser = subparsers.add_parser(
        "network", help="Network diagnostics and status"
    )
    network_subparsers = network_parser.add_subparsers(dest="network_command")

    # network status
    network_status = network_subparsers.add_parser(
        "status", help="Show complete network health at a glance"
    )
    network_status.set_defaults(func=cmd_network_status)

    # === doctor command (top-level) ===
    doctor_parser = subparsers.add_parser(
        "doctor", help="Auto-diagnose and fix common issues"
    )
    doctor_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without making changes"
    )
    doctor_parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Auto-confirm all fixes without prompting"
    )
    doctor_parser.add_argument(
        "--voice", action="store_true",
        help="Run only the voice loop (push-to-talk) preflight — fast, no SSH waits"
    )
    doctor_parser.set_defaults(func=cmd_doctor)
