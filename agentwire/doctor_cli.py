"""CLI for diagnostics — ``agentwire doctor`` and ``agentwire network``.

``doctor`` walks the full install (Python/deps, hooks, skills, damage control,
source-checkout drift, services, config, SSH/tunnels, voice loop, secrets,
remote machines, dead-lettered messages) and optionally auto-fixes the local
ones. ``network status`` is the read-only network-health glance.

The hook/skill drift helpers (``get_hooks_source``, ``_managed_hook_files``,
``_managed_file_state``, ``skill_drift``, ``CLAUDE_SKILLS_DIR``) are owned by
the hooks domain and live in ``hooks_cli``; doctor reads them via a
function-local deferred import to stay single-source-of-truth.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
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
        print("  [..] ffmpeg: not found (optional, needed for voice input)")
        print("     macOS: brew install ffmpeg")
        print("     Ubuntu: sudo apt install ffmpeg")

    # Check Claude Code (optional)
    claude_path = shutil.which("claude")
    if claude_path:
        print(f"  [ok] claude: {claude_path}")
    else:
        print("  [..] claude: not found (optional, use --bare sessions or other agents)")
        print("     Install: https://github.com/anthropics/claude-code")

    # Check Pi coding agent (optional, for pi-* session types)
    pi_path = shutil.which("pi")
    if pi_path:
        try:
            # Pi prints --version to stderr, so merge with stdout
            result = subprocess.run(
                [pi_path, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            pi_version = (result.stdout + result.stderr).strip()
            print(f"  [ok] pi: {pi_path} (v{pi_version})")
        except Exception:
            print(f"  [ok] pi: {pi_path}")
    else:
        print("  [..] pi: not found (optional, required for pi-* session types)")
        print("     Install: npm install -g @mariozechner/pi-coding-agent")

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
    for pname, pcfg in (raw_cfg.get("pi", {}).get("providers", {}) or {}).items():
        p_env_var = (pcfg or {}).get(
            "env_var", f"{pname.upper().replace('-', '_')}_API_KEY"
        )
        expected_keys.append((f"pi.providers.{pname}", [p_env_var]))
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
