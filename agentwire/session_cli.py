"""CLI for session lifecycle — ``agentwire new / fork / worktree / recreate / session-defaults``.

Part of #495 (Phase 1, Wave 1): these five top-level commands were extracted
verbatim from ``agentwire/__main__.py``. Shared stateless helpers live in
``agentwire.core``; the domain-private worktree-registry helpers move here with
the commands. Pure relocation — the CLI surface is unchanged.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import chrome_tabs, pane_manager, services, worktree_registry
from .core import (
    _check_tmux_installed,
    _display_parent,
    _get_machine_config,
    _graceful_kill,
    _launch_tmux_session,
    _notify_portal_sessions_changed,
    _output_json,
    _output_result,
    _record_session_creator,
    _record_session_role,
    _resolve_posture_from_args,
    _resolve_posture_or_config,
    _run_remote,
    _set_session_name_env,
    build_agent_command,
    load_config,
    notify_portal_session_created,
    parse_env_args,
    resolve_default_created_by,
    tmux_session_exists,
)
from .project_config import (
    DEFAULT_POSTURE,
    POSTURES,
    ProjectConfig,
    WorktreeOverrides,
    load_project_config,
    resolve_posture,
    save_project_config,
)
from .roles import (
    RoleConfig,
    derive_session_kind,
    inject_soul,
    load_roles,
    resolve_roles,
)
from .worktree import (
    apply_naming,
    create_and_register_worktree,
    default_base_branch,
    find_git_worktree,
    git_root,
    is_registered_worktree,
    is_valid_branch_name,
    linked_git_worktrees,
    parse_session_name,
    register_worktree,
    remove_worktree,
    worktree_status,
)


def cmd_session_defaults(args) -> int:
    """Resolve what a new session would get — backs the portal resolver endpoint.

    Given a spawn verb's KIND (default: orchestrator, i.e. `agentwire new`)
    and optional posture, returns the composed session type and the intrinsic
    etiquette roles. The portal reads this instead of hardcoding — one
    resolver, one source of truth.
    """
    kind = getattr(args, 'kind', None) or 'orchestrator'
    posture = getattr(args, 'posture', None)
    # Every spawn defaults to bypass now (workers included) — kind no longer
    # affects the posture, only the intrinsic role etiquette below.
    try:
        resolved = resolve_posture(posture or DEFAULT_POSTURE)
    except ValueError as e:
        return _output_result(False, True, str(e))
    _output_json({
        "success": True,
        "kind": kind,
        "posture": posture or DEFAULT_POSTURE,
        "resolved_posture": resolved,  # validated posture, for the resolver display
        "roles": resolve_roles(kind),  # intrinsic etiquette (chips); user roles layer on top
        "postures": list(POSTURES),
    })
    return 0


def _shared_dir_conflicts(panes_output: str, session_name: str, target: str) -> set[str]:
    """Sessions (excluding *session_name* and infra services) whose cwd is *target*.

    *panes_output* is `tmux list-panes -a -F '#{session_name}\t#{pane_current_path}'`.
    Infrastructure service sessions (portal/tts/stt/kokoro/scheduler/notifications)
    run from the repo root but never edit the tree, so they co-reside with an agent
    without conflict and are excluded via the SSOT classifier. Non-service agent
    sessions (including the hardcoded `agentwire` dev session) still count.
    """
    conflicting: set[str] = set()
    for line in panes_output.splitlines():
        if "\t" not in line:
            continue
        sess, p = line.split("\t", 1)
        if sess == session_name:
            continue
        if services.is_service_session(sess):
            continue
        try:
            if str(Path(p).resolve()) == target:
                conflicting.add(sess)
        except (OSError, RuntimeError):
            continue
    return conflicting


def cmd_new(args) -> int:
    """Create a new agent session.

    Supports:
    - "project" -> simple session in ~/projects/project/
    - "project/branch" -> worktree session in ~/projects/project-worktrees/branch/
    - "project@machine" -> remote session
    - "project/branch@machine" -> remote worktree session
    """
    json_mode = getattr(args, 'json', False)

    if not _check_tmux_installed():
        return 1 if not json_mode else _output_result(False, json_mode, "tmux is required but not installed")

    name = args.session
    path = args.path

    if not name:
        return _output_result(False, json_mode, "Usage: agentwire new -s <name> [-p path] [-f]")

    # Parse session name: project, branch, machine. Done early so the session
    # KIND can be derived from whether a worktree is being created.
    project, branch, machine_id = parse_session_name(name)

    # --- Role resolution (two distinct phases) -----------------------------
    # Phase 1 — resolve. The session's ROLE is derived from the spawn verb:
    # cmd_worktree passes an explicit kind (default "worker"); for `new` we
    # derive it — a project/branch name (or any worktree dispatch from the
    # scheduler / portal) is a worker on worktree topology, a plain name is
    # an orchestrator. The safety-rail kind (worker) STACKS its intrinsic
    # etiquette under user roles — which etiquette file depends on topology
    # (worktree_topology below); orchestrator is a replaceable persona. All
    # precedence lives in resolve_roles — one greppable function.
    kind = derive_session_kind(bool(branch), getattr(args, 'kind', None))
    # cmd_worktree/_launch_session passes an explicit worktree_topology=True —
    # it MUST NOT be re-derived from bool(branch) here, since its session_name
    # is a flat `{project}-{name}` (no slash), which parse_session_name would
    # otherwise misread as branchless. A direct `agentwire new -s proj/branch`
    # has no such override, so bool(branch) is the right signal for it.
    worktree_topology = getattr(args, 'worktree_topology', None)
    if worktree_topology is None:
        worktree_topology = bool(branch)
    roles_arg = getattr(args, 'roles', None)
    cli_roles = [r.strip() for r in roles_arg.split(",") if r.strip()] if roles_arg else None

    project_path_for_config = Path(path).expanduser().resolve() if path else None
    project_roles = None
    if project_path_for_config:
        existing = load_project_config(project_path_for_config)
        if existing and existing.roles:
            project_roles = existing.roles

    role_names = resolve_roles(kind, worktree_topology=worktree_topology, cli_roles=cli_roles, project_roles=project_roles)

    # Phase 2 — auto-append: the soul personality role rides last (recency
    # weight). Kept visibly separate from resolution above.
    role_names = inject_soul(role_names, load_config(), no_soul=getattr(args, 'no_soul', False))

    # Load and validate roles
    roles: list[RoleConfig] = []
    if role_names:
        roles, missing = load_roles(role_names, project_path_for_config)
        if missing:
            return _output_result(False, json_mode, f"Roles not found: {', '.join(missing)}")

    # Flag-relevance guard: --base / --pull-first only mean something when
    # `new` is creating a worktree (a project/branch name). Error loudly
    # rather than silently ignoring them on a plain session. For a custom
    # base on a standalone branch, use the `worktree` verb.
    if not branch:
        if getattr(args, 'base', None) is not None:
            return _output_result(False, json_mode, "--base only applies to worktree sessions (use a project/branch name, or the `worktree` verb)")
        if getattr(args, 'pull_first', None) is not None:
            return _output_result(False, json_mode, "--pull-first/--no-pull-first only apply to worktree sessions (a project/branch name)")

    first_message = getattr(args, 'first_message', None)
    if first_message and machine_id:
        return _output_result(False, json_mode, "--first-message is local-only (readiness capture doesn't span SSH)")

    # Build the tmux session name (convert dots to underscores, preserve slashes)
    if branch:
        session_name = f"{project}/{branch}".replace(".", "_")
    else:
        session_name = project.replace(".", "_")

    # Load config
    config = load_config()
    projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser()
    worktrees_config = config.get("projects", {}).get("worktrees", {})
    worktrees_enabled = worktrees_config.get("enabled", True)
    worktree_suffix = worktrees_config.get("suffix", "-worktrees")
    auto_create_branch = worktrees_config.get("auto_create_branch", True)

    # Handle remote session
    if machine_id:
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        remote_projects_dir = machine.get("projects_dir", "~/projects")

        # Build remote path
        if path:
            remote_path = path
        elif branch:
            remote_path = f"{remote_projects_dir}/{project}{worktree_suffix}/{branch}"
        else:
            remote_path = f"{remote_projects_dir}/{project}"

        # If branch specified, create worktree on remote
        if branch:
            # Create worktree on remote
            project_path = f"{remote_projects_dir}/{project}"
            worktree_path = remote_path

            # Check if worktree already exists
            check_cmd = f"test -d {shlex.quote(worktree_path)}"
            result = _run_remote(machine_id, check_cmd)

            if result.returncode != 0:
                # Create worktree
                # First check if branch exists
                branch_check = f"cd {shlex.quote(project_path)} && git rev-parse --verify refs/heads/{shlex.quote(branch)} 2>/dev/null"
                result = _run_remote(machine_id, branch_check)

                if result.returncode == 0:
                    # Branch exists, create worktree
                    create_cmd = f"cd {shlex.quote(project_path)} && mkdir -p $(dirname {shlex.quote(worktree_path)}) && git worktree add {shlex.quote(worktree_path)} {shlex.quote(branch)}"
                elif auto_create_branch:
                    # Create branch with worktree
                    create_cmd = f"cd {shlex.quote(project_path)} && mkdir -p $(dirname {shlex.quote(worktree_path)}) && git worktree add -b {shlex.quote(branch)} {shlex.quote(worktree_path)}"
                else:
                    return _output_result(False, json_mode, f"Branch '{branch}' does not exist and auto_create_branch is disabled")

                result = _run_remote(machine_id, create_cmd)
                if result.returncode != 0:
                    return _output_result(False, json_mode, f"Failed to create remote worktree: {result.stderr}")

        # Check if remote session already exists
        check_cmd = f"tmux has-session -t ={shlex.quote(session_name)} 2>/dev/null"
        result = _run_remote(machine_id, check_cmd)
        if result.returncode == 0:
            if args.force:
                # Kill existing session
                _graceful_kill(session_name, machine_id)
            else:
                return _output_result(False, json_mode, f"Session '{session_name}' already exists on {machine_id}. Use -f to replace.")

        # Create remote tmux session — resolve posture (shared core)
        posture, st_err = _resolve_posture_from_args(args)
        if st_err:
            return _output_result(False, json_mode, st_err)

        # Build agent command
        agent = build_agent_command(posture, roles if roles else None, model=getattr(args, 'model', None))
        agent.env.update(parse_env_args(getattr(args, 'env', None)))

        agent_cmd = agent.command

        # If agent command uses a local temp file, write content to remote
        if agent.temp_file and agent_cmd:
            try:
                with open(agent.temp_file, 'r') as f:
                    content = f.read()
                # Create remote temp file with same content
                remote_temp = f"/tmp/agentwire-prompt-{session_name.replace('/', '-')}.txt"
                write_cmd = f"cat > {shlex.quote(remote_temp)} << 'AGENTWIRE_EOF'\n{content}\nAGENTWIRE_EOF"
                result = _run_remote(machine_id, write_cmd)
                if result.returncode == 0:
                    # Replace local path with remote path in command
                    agent_cmd = agent_cmd.replace(agent.temp_file, remote_temp)
            except Exception as e:
                print(f"Warning: Failed to write system prompt to remote: {e}", file=sys.stderr)

        # Create session - Agent starts immediately if not bare (env is
        # injected at creation time; see _launch_tmux_session).
        result = _launch_tmux_session(session_name, remote_path, agent.env, agent_cmd, machine_id)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create remote session: {result.stderr}")

        if json_mode:
            _output_json({
                "success": True,
                "session": f"{session_name}@{machine_id}",
                "path": remote_path,
                "branch": branch,
                "machine": machine_id,
            })
        else:
            print(f"Created session '{session_name}' on {machine_id} in {remote_path}")
            print(f"Attach via portal or: ssh {machine.get('host', machine_id)} -t tmux attach -t {session_name}")

        _notify_portal_sessions_changed()
        return 0

    # Local session
    # Resolve path
    base_branch = getattr(args, 'base', None) or 'main'
    _pull_first = getattr(args, 'pull_first', None)
    pull_first = True if _pull_first is None else _pull_first

    def _spawn_worktree(project_path: Path, session_path: Path) -> tuple[bool, str | None]:
        """Fetch origin/<base> if requested, then create + REGISTER the worktree.

        Routed through ``create_and_register_worktree`` (#837) so a worktree
        session created by `agentwire new -s project/branch` — including every
        scheduler worktree dispatch, which shells out to exactly this — is
        visible to `worktree --list`/`--dangling`/`--prune`/`--remove` instead
        of being an orphan by construction.
        """
        worktree_commit: str | None = None
        if pull_first:
            fetch = subprocess.run(
                ["git", "fetch", "origin", base_branch],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                stderr = (fetch.stderr or fetch.stdout or "").strip()
                return False, f"git fetch origin {base_branch} failed: {stderr}"
            worktree_commit = f"origin/{base_branch}"
        ok, err = create_and_register_worktree(
            project_path,
            branch=branch,
            worktree_path=session_path,
            session=session_name,
            base=base_branch,
            kind=kind,
            auto_create_branch=auto_create_branch,
            commit=worktree_commit,
        )
        return (True, None) if ok else (False, err)

    if path and branch and worktrees_enabled:
        # Path + branch: use provided path as main repo, create worktree from it
        project_path = Path(path).expanduser().resolve()
        session_path = project_path.parent / f"{project_path.name}{worktree_suffix}" / branch

        # Ensure worktree exists
        if not session_path.exists():
            if not project_path.exists():
                return _output_result(False, json_mode, f"Project path does not exist: {project_path}")
            ok, err = _spawn_worktree(project_path, session_path)
            if not ok:
                return _output_result(False, json_mode, err or "worktree creation failed")
    elif path:
        session_path = Path(path).expanduser().resolve()
    elif branch and worktrees_enabled:
        # Worktree session: ~/projects/project-worktrees/branch/
        project_path = projects_dir / project
        session_path = projects_dir / f"{project}{worktree_suffix}" / branch

        # Ensure worktree exists
        if not session_path.exists():
            if not project_path.exists():
                return _output_result(False, json_mode, f"Project path does not exist: {project_path}")
            ok, err = _spawn_worktree(project_path, session_path)
            if not ok:
                return _output_result(False, json_mode, err or "worktree creation failed")
    else:
        # Simple session: ~/projects/project/
        session_path = projects_dir / project

    # Safety: refuse to attach a new session to a path that is already the working
    # directory of another active session. Two agents sharing the same working tree
    # is the dangerous footgun (one's dirty state visible to the other, branches
    # mixing). Worktree sessions (project/branch) get unique paths and won't trip
    # this. --force overrides; --allow-shared-dir bypasses ONLY this guard
    # (without --force's kill-replace of an existing same-name session — service
    # respawns must never destroy a concurrently-created healthy instance).
    if not args.force and not getattr(args, "allow_shared_dir", False):
        target = str(session_path.resolve()) if session_path.exists() else str(session_path)
        panes_result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"],
            capture_output=True, text=True,
        )
        if panes_result.returncode == 0:
            conflicting = _shared_dir_conflicts(panes_result.stdout, session_name, target)
            if conflicting:
                others = ", ".join(sorted(conflicting))
                hint = (
                    f"Refusing to attach session '{session_name}' to {session_path}: "
                    f"already the working directory of active session(s): {others}. "
                    "Use a 'project/branch' name to create an isolated worktree, "
                    "pick a different path, or pass --force to override."
                )
                return _output_result(False, json_mode, hint)

    if not session_path.exists():
        if branch:
            # #739: a worktree session's path is only ever meant to come from
            # `_spawn_worktree`/`ensure_worktree` above. Reaching here with it
            # still missing means that creation silently didn't happen (or
            # the dir vanished right after) — mkdir-ing an empty placeholder
            # would launch the agent into a directory with no git checkout at
            # all. Fail loud instead of returning a `path` that doesn't back
            # a real worktree.
            return _output_result(False, json_mode,
                                   f"Worktree path does not exist after creation: {session_path}")
        elif args.force or path:
            # Auto-create directory with -f flag or when custom path explicitly provided
            session_path.mkdir(parents=True, exist_ok=True)
        else:
            return _output_result(False, json_mode, f"Path does not exist: {session_path}")

    # Check if session already exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={session_name}"],
        capture_output=True
    )
    if result.returncode == 0:
        if args.force:
            # Kill existing session
            _graceful_kill(session_name)
        else:
            return _output_result(False, json_mode, f"Session '{session_name}' already exists. Use -f to replace.")

    # Resolve the session's posture BEFORE creating the tmux session, so we can
    # inject secrets via `tmux new-session -e K=V` (the only way to make the
    # initial pane's shell see them — post-hoc `tmux set-environment` only
    # affects shells spawned AFTER the call).

    # Determine posture. Precedence: explicit --posture / internal booleans
    # (shared core) → existing .agentwire.yml posture → default (bypass).
    persist = getattr(args, 'persist', False)
    posture_explicit = bool(
        getattr(args, 'posture', None)
        or getattr(args, 'bare', False)
        or getattr(args, 'prompted', False)
    )
    if posture_explicit:
        posture, st_err = _resolve_posture_from_args(args)
        if st_err:
            return _output_result(False, json_mode, st_err)
    else:
        existing_config = load_project_config(session_path)
        if existing_config and existing_config.posture:
            posture = resolve_posture(existing_config.posture)
        else:
            # Kind default posture (no global default-role lookup).
            posture, _ = _resolve_posture_from_args(args)

    # Record the creating session as parent for prompt routing. --created-by
    # overrides (including '' to opt out); otherwise default to the caller
    # ONLY when the new session is in the caller's own project — a
    # cross-project spawn gets its own root instead of flattening into the
    # caller's subtree (#715). --caller-session is the candidate caller for
    # that check (MCP forwards it; direct CLI use falls back to the current
    # tmux session). Resolved before building the agent env (#743 needs the
    # real parent, if any, in AGENTWIRE_CREATED_BY BEFORE `tmux new-session
    # -e` fires) and before seeding so a failed seed can name its sender.
    #
    # Joint default with role (#716): an EXPLICITLY requested orchestrator
    # roots by default instead — a durable orchestrator shouldn't answer to
    # whoever happened to spawn it. Gated on the explicit --kind flag (not
    # the resolved `kind`, which is "orchestrator" by default for any
    # branchless name) so the ubiquitous plain `agentwire new` case keeps
    # its existing same-project-inherit behavior untouched. This is the one
    # place this default lives — cmd_worktree's `_launch_session` forwards
    # its own `--kind` straight through as `kind` here, so `agentwire
    # worktree --kind orchestrator` / `agentwire orchestrator` compose with
    # it for free.
    #
    # `reviewer` (#827) deliberately does NOT join this rule — it's an
    # exact-string match against 'orchestrator', so `--kind reviewer` falls
    # through to the elif below and gets the normal same-project inherit
    # behavior. A reviewer is scoped to reviewing a specific sibling's PR, so
    # it should nest under whoever spawned it (for sidebar/prompt-routing),
    # unlike a durable orchestrator that outlives any one spawner.
    #
    # The caller is resolved UNCONDITIONALLY, separately from the rooting
    # decision, because cohort membership (#852) must not inherit rooting's
    # cross-project rule: the 2026-08-01 fan-out was three cross-project
    # children (rooted standalone, no parent recorded) and one same-project
    # one, and a cohort derived from `created_by` would have protected exactly
    # that one child while the parent was reaped out from under the other
    # three. Rooting answers "who has authority"; the cohort answers "who owns
    # this session's lifecycle" — same caller, different questions.
    created_by = getattr(args, 'created_by', None)
    caller = getattr(args, 'caller_session', None) or pane_manager.get_current_session()
    if created_by is None and getattr(args, 'kind', None) == 'orchestrator':
        created_by = ''
    elif created_by is None:
        created_by = resolve_default_created_by(caller, session_path)

    # Build agent command
    model_override = getattr(args, 'model', None)
    agent = build_agent_command(posture, roles if roles else None, model=model_override)
    agent.env.update(parse_env_args(getattr(args, 'env', None)))
    _set_session_name_env(agent, session_name, created_by=created_by)

    agent_cmd = agent.command

    # #739: re-verify right before launch — closes the window between the
    # earlier worktree-creation check and this point (shared-dir probe,
    # posture/agent-command resolution) during which an external actor could
    # still remove the directory. `agentwire new --json` must never report
    # success with a `path` that doesn't exist on disk.
    if not session_path.exists():
        return _output_result(False, json_mode,
                               f"Session path vanished before launch: {session_path}")

    # Create new tmux session with env vars injected at creation time, cd
    # into place, and start the agent (skipped when bare).
    _launch_tmux_session(session_name, session_path, agent.env, agent_cmd)

    # Record creator/role and push the enriched session_created event (#747)
    # as early as possible — before the potentially slow first-message wait
    # below — so the portal broadcast carries real parent/role instead of
    # racing the metadata write against the async global tmux hook.
    _record_session_creator(session_name, created_by, via="new")
    _record_session_role(session_name, kind)
    notify_portal_session_created(session_name, created_by, kind)

    # Enroll in the caller's fan-out cohort (#852) so the caller can be told
    # "your children are still working" and can join on them later. Automatic
    # by design — the failure this prevents is silent and unattended, so it
    # must not depend on a task author remembering to register anything.
    # Skipped for an explicit orchestrator (durable by definition, and rooted
    # for the same reason) and for --no-cohort's genuinely fire-and-forget
    # spawn. Enrollment is bookkeeping only: nothing kills a child until the
    # caller joins, the cohort deadline passes, or the caller itself dies.
    if (
        caller
        and caller != session_name
        and not getattr(args, 'no_cohort', False)
        and getattr(args, 'kind', None) != 'orchestrator'
    ):
        from agentwire import cohort
        cohort.enroll(
            caller, session_name,
            topology="worktree" if worktree_topology else "main",
        )

    # Update project config (.agentwire.yml) - only if --persist is given.
    # Persist USER roles (--roles) only — the intrinsic etiquette and soul are
    # derived/auto-appended each run, never written into project config.
    if persist:
        existing_config = load_project_config(session_path)
        if existing_config:
            # Preserve existing settings if not overridden by CLI
            project_config = ProjectConfig(
                posture=posture,
                roles=cli_roles if cli_roles else existing_config.roles,
                voice=existing_config.voice,
                parent=existing_config.parent,
            )
        else:
            # Create new config
            project_config = ProjectConfig(
                posture=posture,
                roles=cli_roles if cli_roles else [],
                voice=None,
            )
        save_project_config(project_config, session_path)

    # Deliver the first message once the agent is ready, through the SAME
    # guarded send path every other sender uses (#840). Seeding used to paste
    # bare and, on an unverified confirm, call recover_failed_seed directly —
    # the one send path that skipped the marker-based already-delivered check
    # (#839) and the foreign-draft pre-flight (#845), so a seed that actually
    # landed could be enqueued a second time and a human's half-typed draft
    # could be erased to make room for it. Delivery failure still doesn't fail
    # the command — the session exists — but it must be LOUD (#695).
    first_message_delivered = None
    first_message_fallback = None
    if first_message and agent_cmd:
        from agentwire.send_cli import _foreign_draft_block, _recover_unverified_send
        from agentwire.session_ready import (
            new_delivery_marker,
            recover_failed_seed,
            send_verified,
            tag_message,
            wait_for_session_ready,
        )

        # created_by can be None for a legitimate cross-project standalone
        # spawn (#715) even though a real caller made the call — fall back to
        # that caller for the recovery message's sender rather than the generic
        # "agentwire" so the fallback attribution stays accurate.
        seed_sender = created_by or caller or "agentwire"
        if not json_mode:
            print("Waiting for agent to deliver first message...")

        # marker stays None until we actually paste. That distinction is
        # load-bearing for the recovery below: "already delivered" is only a
        # fact when a per-attempt marker could have reached scrollback.
        marker = None
        blocked = False
        verified = False
        if wait_for_session_ready(session_name, timeout=60):
            # #845 — a fresh session's box is normally empty, but boot can take
            # the full 60s above, which is ample room for a human to attach and
            # start typing. Pasting on top of that garbles both, and the
            # recovery below would then erase their draft as if it were the
            # wreckage of our own paste. Look before touching it.
            blocked, first_message_fallback = _foreign_draft_block(
                session_name, first_message, seed_sender
            )
            if not blocked:
                marker = new_delivery_marker()
                verified = send_verified(
                    session_name, tag_message(first_message, marker), marker=marker
                )

        if verified:
            first_message_delivered = True
        elif blocked:
            first_message_delivered = False
        else:
            first_message_fallback = (
                # We pasted and the confirm was ambiguous — the shared guard
                # checks scrollback for THIS attempt's marker before enqueuing,
                # so a seed that really landed is a no-op instead of a duplicate.
                _recover_unverified_send(
                    session_name, first_message, seed_sender, marker=marker
                )
                if marker
                # The agent never became ready, so nothing was ever pasted:
                # there is no marker to match and a bare-text scrollback check
                # could only produce a false "already delivered" that DROPS the
                # seed. Go straight to the durable enqueue.
                else recover_failed_seed(
                    session_name, first_message, sender=seed_sender
                )
            )
            # A marker on scrollback proves this attempt's paste submitted; the
            # confirm read was just ambiguous. That is delivered, not failed —
            # reporting otherwise sends the caller chasing a message that landed.
            first_message_delivered = first_message_fallback == "already_delivered"

        if not json_mode and not first_message_delivered:
            if first_message_fallback == "inbox":
                print(
                    f"WARNING: first message NOT delivered to '{session_name}' — "
                    "queued to its msg inbox for delivery once the input box is ready",
                    file=sys.stderr,
                )
            elif first_message_fallback == "inbox_stuck":
                print(
                    f"WARNING: first message NOT delivered to '{session_name}' — "
                    "queued to its msg inbox, but the stuck draft in the input box "
                    "could not be confirmed cleared; check the pane manually",
                    file=sys.stderr,
                )
            elif first_message_fallback == "inbox_blocked":
                print(
                    f"WARNING: first message NOT pasted into '{session_name}' — its "
                    "input box already held an unrelated unsent draft, which was left "
                    "untouched; the prompt was queued to its msg inbox and the drain "
                    "delivers it once the box goes idle",
                    file=sys.stderr,
                )
            else:
                print(
                    f"WARNING: first message NOT delivered to '{session_name}' "
                    "and could not be queued — paste it manually",
                    file=sys.stderr,
                )

    if json_mode:
        result = {
            "success": True,
            "session": session_name,
            "path": str(session_path),
            "branch": branch,
            "machine": None,
        }
        if first_message:
            result["first_message_delivered"] = bool(first_message_delivered)
            # Present whenever the straight verified paste didn't carry the
            # seed on its own: "inbox" (queued, box confirmed cleared),
            # "inbox_stuck" (queued, but the stale draft in the box couldn't be
            # confirmed cleared — #843), "inbox_blocked" (queued, a foreign
            # draft was left untouched — #845), "already_delivered" (the paste
            # DID land, only the confirm read was ambiguous — #840, and the one
            # value that pairs with delivered=True), or None (fallback failed
            # too — the prompt is gone; caller must redeliver).
            if not first_message_delivered or first_message_fallback:
                result["first_message_fallback"] = first_message_fallback
        _output_json(result)
    else:
        print(f"Created session '{session_name}' in {session_path}")
        print(f"Attach with: tmux attach -t {session_name}")

    _notify_portal_sessions_changed()
    return 0


def _resolve_and_soul_worktree_roles(has_branch: bool, project_roles: list[str] | None = None) -> list[str]:
    """Shared by cmd_recreate/cmd_fork (5 call sites): derive kind from
    branch presence, resolve the topology-aware intrinsic etiquette, then
    append soul — the same two-phase resolve/soul sequence cmd_new uses.
    Neither command accepts --roles, so cli_roles is never a factor here.
    """
    kind = derive_session_kind(has_branch)
    role_names = resolve_roles(kind, worktree_topology=has_branch, project_roles=project_roles)
    return inject_soul(role_names, load_config())


def cmd_recreate(args) -> int:
    """Destroy and recreate a session with fresh worktree.

    Steps:
    1. Kill existing session (local or remote)
    2. Remove worktree
    3. Pull latest on main repo
    4. Create new worktree with timestamp branch
    5. Create new session

    Supports remote sessions with session@machine format.
    """
    session_full = args.session
    json_mode = getattr(args, 'json', False)

    if not session_full:
        return _output_result(False, json_mode, "Usage: agentwire recreate -s <session>")

    # Parse session name
    project, branch, machine_id = parse_session_name(session_full)

    # Load config
    config = load_config()
    projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser()
    worktrees_config = config.get("projects", {}).get("worktrees", {})
    worktree_suffix = worktrees_config.get("suffix", "-worktrees")

    # Build session name for tmux (preserve slashes, convert dots to underscores)
    if branch:
        session_name = f"{project}/{branch}".replace(".", "_")
    else:
        session_name = project.replace(".", "_")

    if machine_id:
        # Remote recreate
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        remote_projects_dir = machine.get("projects_dir", "~/projects")

        # Step 1: Kill existing session
        _graceful_kill(session_name, machine_id)

        # Determine paths
        project_path = f"{remote_projects_dir}/{project}"
        if branch:
            worktree_path = f"{remote_projects_dir}/{project}{worktree_suffix}/{branch}"
        else:
            worktree_path = project_path

        # Step 2: Remove worktree (if branch session)
        if branch:
            remove_cmd = f"cd {shlex.quote(project_path)} && git worktree remove {shlex.quote(worktree_path)} --force 2>/dev/null; rm -rf {shlex.quote(worktree_path)}"
            _run_remote(machine_id, remove_cmd)

        # Step 3: Pull latest on main repo
        pull_cmd = f"cd {shlex.quote(project_path)} && git pull origin main 2>/dev/null || git pull origin master 2>/dev/null || true"
        _run_remote(machine_id, pull_cmd)

        # Step 4: Create new worktree with timestamp branch
        if branch:
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            new_branch = f"{branch}-{timestamp}"

            create_wt_cmd = f"cd {shlex.quote(project_path)} && mkdir -p $(dirname {shlex.quote(worktree_path)}) && git worktree add -b {shlex.quote(new_branch)} {shlex.quote(worktree_path)}"
            result = _run_remote(machine_id, create_wt_cmd)
            if result.returncode != 0:
                return _output_result(False, json_mode, f"Failed to create worktree: {result.stderr}")

        # Step 5: Create new session
        session_path = worktree_path if branch else project_path

        # Determine posture from --posture flag, else default bypass (the
        # remote project_config isn't readable here).
        posture, st_err = _resolve_posture_or_config(args, None)
        if st_err:
            return _output_result(False, json_mode, st_err)

        # Inject the kind's intrinsic etiquette — a remote project/branch
        # recreate is still a worker on worktree topology and must carry the
        # safety contract. The remote project_config isn't readable here, so
        # we resolve from the derived kind alone; the bundled etiquette roles
        # resolve locally and are baked into the agent command.
        role_names = _resolve_and_soul_worktree_roles(bool(branch))
        roles = None
        if role_names:
            roles, _ = load_roles(role_names)

        # Build agent command using the standard function
        agent = build_agent_command(posture, roles)
        agent.env.update(parse_env_args(getattr(args, 'env', None)))
        agent_cmd = agent.command

        result = _launch_tmux_session(session_name, session_path, agent.env, agent_cmd, machine_id)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create session: {result.stderr}")

        if json_mode:
            _output_json({
                "success": True,
                "session": session_name,
                "path": session_path,
                "branch": new_branch if branch else None,
                "machine": machine_id,
            })
        else:
            print(f"Recreated session '{session_name}' on {machine_id} in {session_path}")

        return 0

    # Local recreate
    # Step 1: Kill existing session
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={session_name}"],
        capture_output=True
    )
    if result.returncode == 0:
        _graceful_kill(session_name)

    # Determine paths
    project_path = projects_dir / project
    if branch:
        worktree_path = projects_dir / f"{project}{worktree_suffix}" / branch
    else:
        worktree_path = project_path

    # Step 2: Remove worktree (if branch session)
    if branch and worktree_path.exists():
        remove_worktree(project_path, worktree_path)
        # Force remove if git worktree remove still left it on disk
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
        # Drop the old registry entry — Step 4 re-registers under the fresh
        # timestamped branch, and a leftover entry pointing at the discarded
        # branch would show up in --list/--dangling as a live worktree.
        worktree_registry.unregister(project_path, worktree_path=worktree_path)

    # Step 3: Pull latest on main repo
    if project_path.exists():
        subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=project_path,
            capture_output=True
        )

    # Step 4: Create new worktree with timestamp branch
    if branch:
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        new_branch = f"{branch}-{timestamp}"

        ok, err = create_and_register_worktree(
            project_path,
            branch=new_branch,
            worktree_path=worktree_path,
            session=session_name,
            base=getattr(args, 'base', None),
            kind=derive_session_kind(True, getattr(args, 'kind', None)),
            auto_create_branch=True,
        )
        if not ok:
            return _output_result(False, json_mode, err)

    session_path = worktree_path if branch else project_path

    if not session_path.exists():
        return _output_result(False, json_mode, f"Path does not exist: {session_path}")

    # Determine posture from --posture flag or existing config (session-level
    # only, not saved).
    project_config = load_project_config(session_path)
    posture, st_err = _resolve_posture_or_config(args, project_config)
    if st_err:
        return _output_result(False, json_mode, st_err)

    # Route through resolve_roles with a derived kind so the recreated session
    # carries its kind's intrinsic etiquette — recreating a project/branch
    # (worktree) session re-injects the worker-worktree safety contract
    # (isolation / verify / draft-PR / notify), which a raw project_config.roles
    # copy would silently drop. Same contract as cmd_new/cmd_worktree.
    project_roles = list(project_config.roles) if project_config and project_config.roles else None
    role_names = _resolve_and_soul_worktree_roles(bool(branch), project_roles=project_roles)
    roles = None
    if role_names:
        roles, _ = load_roles(role_names, session_path)

    # Build agent command
    agent = build_agent_command(posture, roles)
    agent.env.update(parse_env_args(getattr(args, 'env', None)))

    agent_cmd = agent.command

    # Step 5: Create new session (env injected at creation time, cd, agent
    # start — see _launch_tmux_session).
    _launch_tmux_session(session_name, session_path, agent.env, agent_cmd)

    if json_mode:
        _output_json({
            "success": True,
            "session": session_name,
            "path": str(session_path),
            "branch": new_branch if branch else None,
            "machine": None,
        })
    else:
        print(f"Recreated session '{session_name}' in {session_path}")
        print(f"Attach with: tmux attach -t {session_name}")

    return 0


def _resolve_project_arg(project_arg: str, projects_dir: Path) -> Path:
    """Resolve ``--project`` to an absolute repo path, independent of cwd.

    An explicit path (absolute, or one that actually exists relative to cwd —
    ``.``, ``../sibling``) is honored as given. Otherwise ``project_arg`` is
    treated as a bare project NAME and resolved against the configured
    projects dir, the same way ``cmd_new`` resolves a bare project name from
    ``project/branch`` session syntax — never by blindly joining it onto
    whatever directory the command happens to run from. That naive join is
    what path-doubled ``<cwd>/<project>`` into a nonexistent dir when cwd was
    already the project's own directory (#740).
    """
    expanded = Path(project_arg).expanduser()
    if expanded.is_absolute() or expanded.exists():
        return expanded.resolve()
    return (projects_dir / project_arg).resolve()


def cmd_worktree(args) -> int:
    """Create a git worktree + agentwire session in one command.

    Three modes:
    - Default: new branch from base. `agentwire worktree fix-bug`
    - --existing: checkout existing branch. `agentwire worktree feature/auth --existing`
    - --ref: detached at a ref. `agentwire worktree review-v2 --ref v2.0.0`

    If the worktree already exists, reattaches to the existing session.

    ROLE and TOPOLOGY are independent axes — this verb picks worktree
    topology; role defaults to "worker" (--kind orchestrator overrides it,
    for a durable branch-pinned project window — see the `orchestrator`
    sugar verb; --kind reviewer overrides it for a PR-review station). Kind
    "worker" is a safety-rail kind: its intrinsic etiquette (isolation, no
    live-tool mutation, in-worktree verification, draft-PR + notify-back) is
    auto-injected by resolve_roles for every dispatch and is non-overridable:
    it's always present, no opt-out, no templating. `--roles` /
    `.agentwire.yml roles:` ADD to it, they never replace it. Kind "reviewer"
    is the same non-overridable shape, inverted — never opens/merges its own
    PR, reports a verdict via notify_parent instead — and stays parented like
    worker (no rooting override). Kind "orchestrator" is a replaceable
    persona instead — and, unless --created-by says otherwise, roots itself
    (no parent) rather than inheriting the caller, since a durable
    orchestrator shouldn't be a subordinate of whoever happened to spawn it.
    """
    json_mode = getattr(args, 'json', False)
    name = getattr(args, 'name', None)
    use_existing = getattr(args, 'existing', False)
    ref = getattr(args, 'ref', None)

    from .config import load_config as load_config_typed
    full_config = load_config_typed()
    wt_config = full_config.worktree

    # Resolve the repo: explicit --project → config default → git root of cwd
    # (so a worktree session can be spawned from any subdir of a monorepo).
    project_arg = getattr(args, 'project', None)
    if project_arg:
        project_base = _resolve_project_arg(project_arg, full_config.projects.dir)
    elif wt_config.default_project:
        project_base = Path(wt_config.default_project).expanduser().resolve()
    else:
        project_base = Path(os.getcwd())
    project_path = git_root(project_base) or project_base

    # Per-project overrides (#705): the repo's .agentwire.yml `worktree:` block
    # (dir/base) beats the global config for THIS project. Precedence:
    # per-invocation flag → project .agentwire.yml → global config.yaml →
    # built-ins. Resolved BEFORE the subcommand dispatch so create / list /
    # status / remove / prune all scope to the same dir — remove/prune must
    # find what create made. (Registry entries record the resolved path, so
    # already-created worktrees survive an override change regardless.)
    project_config = load_project_config(project_path)
    project_wt = project_config.worktree if project_config else WorktreeOverrides()

    worktree_dir = project_wt.dir or wt_config.worktree_dir

    # Registry management flags (name is optional for these).
    if getattr(args, 'list', False):
        return _worktree_list(args, project_path, json_mode)
    if getattr(args, 'watch', False):
        return _worktree_watch(args, project_path, json_mode)
    if getattr(args, 'prune', False) or getattr(args, 'gc_merged', False):
        return _worktree_prune(args, project_path, worktree_dir, json_mode)
    if getattr(args, 'dangling', False):
        return _worktree_dangling(args, project_path, json_mode)
    if getattr(args, 'status', False):
        return _worktree_status(args, project_path, worktree_dir, json_mode)
    if getattr(args, 'remove', False):
        return _worktree_remove(args, project_path, worktree_dir, json_mode)

    if not name:
        return _output_result(False, json_mode, "Usage: agentwire worktree <name> (or --list / --remove <name> / --prune)")

    if not _check_tmux_installed():
        return _output_result(False, json_mode, "tmux is required but not installed")

    if not (project_path / ".git").exists():
        return _output_result(False, json_mode, f"Not a git repo: {project_path}")

    project_name = project_path.name
    # Session/worktree-dir key stays the raw CLI name made tmux-safe; the git
    # branch may be templated separately via worktree.naming. The worktree
    # nests per project — ~/worktrees/<project>/<name>/ — mirroring
    # ~/projects/<project>/; the tmux session name stays flat {project}-{name}.
    safe_name = re.sub(r"[\s/:.]+", "-", name).strip("-") or "wt"
    session_name = f"{project_name}-{safe_name}"
    worktree_path = worktree_dir / project_name / safe_name

    # --base wins; else project override; else global config default; else the
    # repo's actual default branch (origin/HEAD, not a hardcoded 'main').
    # --current handled per-mode below.
    default_base = getattr(args, 'base', None) or project_wt.base or wt_config.default_base

    # The branch created in default mode honors the naming template.
    templated_branch = apply_naming(wt_config.naming, name)

    # kind defaults to "worker" (zero behavior change for existing callers)
    # — worker-worktree etiquette is injected intrinsically by cmd_new's
    # resolver, since this session is on worktree topology. --kind
    # orchestrator overrides for a durable branch-pinned window; cmd_new
    # itself applies the joint rooting default for that case (SSOT — see
    # its own created_by resolution), so this is just the plain kind pick.
    effective_kind = getattr(args, 'kind', None) or 'worker'

    def _launch_session():
        return cmd_new(type('Args', (), {
            'session': session_name, 'path': str(worktree_path), 'json': json_mode,
            'force': False, 'bare': False, 'prompted': False,
            'kind': effective_kind,
            # session_name is a flat `{project}-{name}` (no slash) — cmd_new's
            # own bool(branch) would misread it as branchless, so this is
            # forced explicitly rather than left to be re-derived.
            'worktree_topology': True,
            'posture': getattr(args, 'posture', None),
            'model': getattr(args, 'model', None),
            'roles': getattr(args, 'roles', None),
            'instructions': None, 'persist': False,
            'first_message': getattr(args, 'prompt', None),
            'env': getattr(args, 'env', None),
            'created_by': getattr(args, 'created_by', None),
            'caller_session': getattr(args, 'caller_session', None),
            'no_cohort': getattr(args, 'no_cohort', False),
        })())

    # If worktree already exists, reattach (and heal the registry entry).
    if worktree_path.exists():
        # --ref is a detached ref, not a branch → record null. --existing
        # checks out a real branch (name). Default mode → the templated branch.
        if ref:
            reattach_branch = None
        elif use_existing:
            reattach_branch = name
        else:
            reattach_branch = templated_branch
        register_worktree(
            project_path, branch=reattach_branch,
            session=session_name, base=default_base,
            worktree_path=worktree_path, kind=effective_kind,
        )
        if tmux_session_exists(session_name):
            # Already live. For automation/json, report and return (never paste
            # into a live session); for a human, attach interactively.
            if json_mode:
                _output_json({"success": True, "session": session_name,
                              "path": str(worktree_path), "reattached": True})
                return 0
            print(f"Worktree exists: {worktree_path}")
            subprocess.run(["tmux", "attach-session", "-t", session_name])
            return 0
        # Worktree dir exists but no live session → relaunch (cmd_new emits the
        # single authoritative json; don't print a second block here).
        if not json_mode:
            print(f"Worktree exists: {worktree_path}")
        return _launch_session()

    # Resolve the git ref to create the worktree from
    base_branch = None
    if ref:
        # --ref mode: detached at a specific ref (tag, commit, branch)
        git_ref = ref
        new_branch = None
        mode_label = f"detached at {ref}"
    elif use_existing:
        # --existing mode: checkout an existing branch
        git_ref = name
        new_branch = None
        mode_label = f"existing branch {name}"
    else:
        # Default mode: new branch from base. Precedence: explicit --base >
        # --current > project .agentwire.yml > global config > repo-derived.
        explicit_base = getattr(args, 'base', None)
        if explicit_base:
            base_branch = explicit_base
        elif getattr(args, 'current', False):
            result = subprocess.run(
                ["git", "-C", str(project_path), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return _output_result(False, json_mode, f"Failed to detect current branch in {project_path}")
            base_branch = result.stdout.strip()
        elif project_wt.base:
            base_branch = project_wt.base
        elif wt_config.default_base:
            base_branch = wt_config.default_base
        else:
            base_branch = default_base_branch(project_path)
        git_ref = f"origin/{base_branch}"
        new_branch = templated_branch

        # Validate the generated branch name BEFORE touching the working tree,
        # so a bad name (spaces, '..', leading '-', from the verbatim default
        # or a naming template) fails cleanly instead of leaving an orphaned
        # detached worktree on disk after `git worktree add` succeeds.
        if not is_valid_branch_name(new_branch, project_path):
            via = f" (from naming template '{wt_config.naming}')" if wt_config.naming else ""
            return _output_result(
                False, json_mode,
                f"Invalid git branch name '{new_branch}'{via}: not a valid ref. "
                f"Use a name without spaces, '..', or a leading '-', or set worktree.naming.",
            )
        mode_label = f"new branch {new_branch} from {git_ref}"

        # Fetch the base branch
        if not json_mode:
            print(f"Fetching origin/{base_branch}...")
        fetch_result = subprocess.run(
            ["git", "-C", str(project_path), "fetch", "origin", base_branch, "--quiet"],
            capture_output=True, text=True,
        )
        if fetch_result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to fetch origin/{base_branch}: {fetch_result.stderr.strip()}")

    # Create worktree
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    wt_cmd = ["git", "-C", str(project_path), "worktree", "add"]
    if new_branch:
        wt_cmd += [str(worktree_path), git_ref, "--detach", "--quiet"]
    elif use_existing:
        wt_cmd += [str(worktree_path), git_ref, "--quiet"]
    else:
        wt_cmd += [str(worktree_path), git_ref, "--detach", "--quiet"]

    wt_result = subprocess.run(wt_cmd, capture_output=True, text=True)
    if wt_result.returncode != 0:
        return _output_result(False, json_mode, f"Failed to create worktree: {wt_result.stderr.strip()}")

    # Create new branch if in default mode
    if new_branch:
        branch_result = subprocess.run(
            ["git", "-C", str(worktree_path), "checkout", "-b", new_branch, "--quiet"],
            capture_output=True, text=True,
        )
        if branch_result.returncode != 0:
            # Don't leave an orphaned detached worktree behind (belt-and-braces;
            # is_valid_branch_name above should already have caught bad names).
            remove_worktree(project_path, worktree_path)
            if worktree_path.exists():
                subprocess.run(
                    ["git", "-C", str(project_path), "worktree", "remove", str(worktree_path), "--force"],
                    capture_output=True,
                )
            _cleanup_empty_project_dir(worktree_path, worktree_dir)
            return _output_result(False, json_mode, f"Failed to create branch '{new_branch}': {branch_result.stderr.strip()}")

    # Record the branch↔session association in the local registry, at the path
    # GIT reports for it (#855) — this flow builds the worktree itself (detach
    # at the base ref, then `checkout -b`), so it registers directly rather
    # than through create_and_register_worktree.
    register_worktree(
        project_path,
        branch=new_branch if new_branch else (name if use_existing else None),
        session=session_name,
        base=base_branch,
        worktree_path=worktree_path,
        kind=effective_kind,
    )

    # cmd_new (via _launch_session) emits the single authoritative json
    # (session, path, branch, first_message_delivered) — don't print a second
    # block here or run_agentwire_cmd's json.loads chokes on two objects.
    if not json_mode:
        print(f"Created worktree: {worktree_path}")
        print(f"Mode: {mode_label}")

    return _launch_session()


def cmd_orchestrator(args) -> int:
    """Sugar for `agentwire worktree --kind orchestrator`.

    Stands up a durable, root project window on its own worktree/branch: a
    replaceable persona (not a safety-railed subordinate) that, unless
    --created-by says otherwise, roots itself instead of inheriting whoever
    ran the command. Everything else (base/existing/ref/roles/posture/
    prompt/registry-management flags) is identical to `worktree`.
    """
    args.kind = 'orchestrator'
    return cmd_worktree(args)


def _format_worktree_row(r: dict) -> str:
    """Two-line human rendering of one registry entry — shared by --list/--watch.

    A ``pane`` entry (#837) is tagged, because its ``session`` column names the
    OWNING session rather than a session that IS this worktree, and its "live"
    badge therefore reads on that owner.
    """
    live = "live" if r["alive"] else ("orphan" if r["exists"] else "stale")
    dead_warn = " !! DEAD-LETTERED REPORT-BACK !!" if r.get("dead_reports") else ""
    tag = "  [pane worker]" if r.get("topology") == "pane" else ""
    return (f"  {r.get('session'):<32} {live:<7} branch={r.get('branch')} "
            f"base={r.get('base')}  {_git_badge(r.get('git'))}{tag}{dead_warn}\n"
            f"      {r.get('worktree_path')}")


def _worktree_list(args, project_path: Path, json_mode: bool) -> int:
    """List worktree registry entries (--list)."""
    show_all = getattr(args, 'all', False)
    from agentwire import inbox
    if show_all:
        rows = worktree_registry.all_entries()
    else:
        rows = [dict(e, project=str(project_path)) for e in worktree_registry.entries(project_path)]

    for r in rows:
        r["alive"] = tmux_session_exists(r.get("session", ""))
        r["exists"] = Path(r.get("worktree_path", "")).exists()
        # Fold in read-only git status so the portal can badge the whole list
        # without N round-trips. Local git only (no network) — cheap per entry.
        if r["exists"]:
            r["git"] = worktree_status(Path(r.get("worktree_path", "")))
        dead_msgs = inbox.list_dead(r.get("session", ""))
        r["dead_reports"] = [m.to_dict() for m in dead_msgs if m.kind in ("done", "escalation")]
        # Recorded creator/parent (persists after the session dies) — lets a
        # dead orphan entry still be placed in its family and re-adopted with
        # the same reporting relationship it had before (#781 ghost cards).
        r["created_by"] = _display_parent(r.get("session", ""), r.get("worktree_path", ""))

    if json_mode:
        _output_json({"success": True, "entries": rows})
        return 0

    if not rows:
        scope = "any repo" if show_all else str(project_path)
        print(f"No worktree sessions registered for {scope}.")
        return 0

    for r in rows:
        print(_format_worktree_row(r))
    return 0


def _worktree_watch(args, project_path: Path, json_mode: bool) -> int:
    """Watch registered worktree sessions in a loop and report status."""
    import datetime
    import sys
    import time

    from agentwire import inbox
    try:
        while True:
            show_all = getattr(args, 'all', False)
            if show_all:
                rows = worktree_registry.all_entries()
            else:
                rows = [dict(e, project=str(project_path)) for e in worktree_registry.entries(project_path)]

            for r in rows:
                r["alive"] = tmux_session_exists(r.get("session", ""))
                r["exists"] = Path(r.get("worktree_path", "")).exists()
                if r["exists"]:
                    r["git"] = worktree_status(Path(r.get("worktree_path", "")))
                dead_msgs = inbox.list_dead(r.get("session", ""))
                r["dead_reports"] = [m.to_dict() for m in dead_msgs if m.kind in ("done", "escalation")]

            if json_mode:
                _output_json({"success": True, "entries": rows})
                sys.stdout.flush()
            else:
                # Clear screen: \033[H\033[2J
                print("\033[H\033[2J", end="")
                scope = "all repos" if show_all else project_path.name
                print(f"=== Watching Worktree Sessions ({scope}) ===")
                print(f"Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                if not rows:
                    print("No worktree sessions registered.")
                for r in rows:
                    print(_format_worktree_row(r))
                print("\nPress Ctrl+C to stop.")
                sys.stdout.flush()
            time.sleep(5)
    except KeyboardInterrupt:
        if not json_mode:
            print("\nStopped watching.")
        return 0


def _git_badge(git: dict | None) -> str:
    """One-line human summary of a worktree's git status (dirty/ahead/behind)."""
    if not git or not git.get("exists"):
        return ""
    parts = []
    if git.get("dirty"):
        parts.append(f"dirty(+{git['staged']}/~{git['unstaged']}/?{git['untracked']})")
    else:
        parts.append("clean")
    if not git.get("upstream"):
        parts.append("no-upstream")
    else:
        if git.get("ahead"):
            parts.append(f"↑{git['ahead']}")
        if git.get("behind"):
            parts.append(f"↓{git['behind']}")
        if git.get("pushed") and not git.get("ahead"):
            parts.append("pushed")
    return " ".join(parts)


@dataclass
class WorktreeRef:
    """A resolved worktree target, plus how confident we are that it's real.

    ``source`` is the whole point (#855):

    - ``"registry"`` — an agentwire registry entry, cross-checked against git.
    - ``"git"``      — git itself knows this worktree (no registry entry, or
                       the entry's recorded path was stale).
    - ``"convention"`` — **nothing real was found.** The path is a guess from
                       the documented layout. Read-only callers may show it;
                       any caller that mutates MUST refuse, because acting on
                       a guessed path is exactly how a teardown "succeeds"
                       while removing nothing.
    """

    session: str
    path: Path
    branch: str | None = None
    base: str | None = None
    source: str = "convention"
    entry: dict | None = None
    topology: str = "worktree"

    @property
    def found(self) -> bool:
        """True when git or the registry actually knows this worktree."""
        return self.source != "convention"


def _sessions_by_path() -> dict[str, str]:
    """Map resolved pane working directory → tmux session name.

    Lets a worktree path find *its* session regardless of naming convention —
    ``{project}-{name}`` and ``{project}/{name}`` are both live in the wild,
    and #855's false success came partly from guessing the wrong one.
    """
    out: dict[str, str] = {}
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return out
    for line in result.stdout.splitlines():
        sess, _, pane_path = line.partition("\t")
        if not sess or not pane_path:
            continue
        try:
            out.setdefault(str(Path(pane_path).resolve()), sess)
        except (OSError, RuntimeError):
            continue
    return out


def _session_for_worktree(worktree_path: Path, project_name: str, name: str) -> str:
    """Best available session name for a worktree, most authoritative first.

    1. A live tmux session whose pane cwd IS the worktree (convention-blind).
    2. A live session among the known naming conventions.
    3. The flat ``{project}-{name}`` default — nothing live to act on anyway.
    """
    try:
        resolved = str(worktree_path.resolve())
    except (OSError, RuntimeError):
        resolved = str(worktree_path)
    by_path = _sessions_by_path().get(resolved)
    if by_path:
        return by_path

    flat = f"{project_name}-{name}"
    for candidate in (flat, f"{project_name}/{name}", name):
        if tmux_session_exists(candidate):
            return candidate
    return flat


def _resolve_worktree_entry(name: str, project_path: Path, worktree_dir: Path) -> WorktreeRef:
    """Resolve a name/session/branch to a :class:`WorktreeRef` by asking GIT.

    Order: registry entry (verified against git) → git's own worktree list →
    the documented convention as a last, explicitly-flagged guess.

    A registry entry whose recorded ``worktree_path`` no longer matches what
    git reports is *healed* to git's path rather than trusted — the registry
    is agentwire's bookkeeping, git is the ground truth (#855).
    """
    project_name = project_path.name
    candidates = {name, f"{project_name}-{name}", f"{project_name}/{name}"}

    entry = None
    for e in worktree_registry.entries(project_path):
        if (e.get("session") in candidates or e.get("branch") == name
                or Path(e.get("worktree_path", "")).name in candidates):
            entry = e
            break

    if entry is not None:
        recorded = Path(entry.get("worktree_path", ""))
        found = find_git_worktree(
            project_path, path=recorded, branch=entry.get("branch"),
        )
        path = found["path"] if found else recorded
        return WorktreeRef(
            session=entry.get("session") or _session_for_worktree(path, project_name, name),
            path=path,
            branch=entry.get("branch") or (found or {}).get("branch"),
            base=entry.get("base"),
            source="registry",
            entry=entry,
            topology=entry.get("topology") or "worktree",
        )

    # No registry entry — ask git directly. Covers hand-created worktrees,
    # pre-registry ones, and any layout that isn't the documented default.
    safe_name = re.sub(r"[\s/:.]+", "-", name).strip("-") or "wt"
    if safe_name.startswith(f"{project_name}-"):
        safe_name = safe_name[len(project_name) + 1:]

    found = find_git_worktree(project_path, branch=name, name=name)
    if found is None and safe_name != name:
        found = find_git_worktree(project_path, branch=safe_name, name=safe_name)
    if found is not None:
        return WorktreeRef(
            session=_session_for_worktree(found["path"], project_name, safe_name),
            path=found["path"], branch=found["branch"], source="git",
        )

    return WorktreeRef(
        session=f"{project_name}-{safe_name}",
        path=worktree_dir / project_name / safe_name,
        source="convention",
    )


def _cleanup_empty_project_dir(worktree_path: Path, worktree_dir: Path) -> None:
    """Remove now-empty per-project dirs under ``worktree_dir``.

    Removing a project's last worktree should not leave an empty
    ``~/worktrees/<project>/`` behind. Walks up from the worktree's parent,
    rmdir'ing while empty, stopping at ``worktree_dir`` itself.
    """
    parent = worktree_path.parent
    while parent != worktree_dir and parent.is_relative_to(worktree_dir):
        try:
            parent.rmdir()  # only succeeds when empty
        except OSError:
            break
        parent = parent.parent


def _worktree_status(args, project_path: Path, worktree_dir: Path, json_mode: bool) -> int:
    """Read-only git status for one worktree session (--status). Never mutates."""
    name = getattr(args, 'name', None)
    if not name:
        return _output_result(False, json_mode, "Usage: agentwire worktree --status <name|session>")

    ref = _resolve_worktree_entry(name, project_path, worktree_dir)
    git = worktree_status(ref.path)

    if json_mode:
        _output_json({"success": True, "session": ref.session,
                      "worktree_path": str(ref.path), "resolved_by": ref.source,
                      "alive": tmux_session_exists(ref.session), **git})
        return 0

    if not git.get("exists"):
        # Say plainly that nothing was found, rather than reporting a guessed
        # path as if it were this worktree's real (merely missing) location.
        if not ref.found:
            print(f"No worktree found for '{name}' in {project_path} "
                  f"(not in the registry or `git worktree list`)")
        else:
            print(f"Worktree path missing: {ref.path}")
        return 0
    print(f"{ref.session}  branch={git.get('branch')}  {_git_badge(git)}")
    return 0


def _worktree_prune(args, project_path: Path, worktree_dir: Path, json_mode: bool) -> int:
    """Drop registry entries whose worktree path is gone; git worktree prune.

    With --gc-merged, also tears down (session + worktree + branch, via the
    same atomic path as --remove) any STILL-PRESENT registered entry whose
    branch is confirmed merged — the "GC orphaned ~/worktrees/* dirs whose
    branch is already merged" sweep from #717. Off by default: this command
    is normally a safe, read-mostly cleanup and shouldn't kill a live,
    in-flight session just because its branch happens to look merged.

    ``--gc-merged`` alone (no ``--prune``) also dispatches here and runs the
    same sweep — cmd_worktree treats it as implying --prune (#740), so
    ``--help`` and behavior agree instead of --gc-merged falling through to
    the usage error.
    """
    removed = []
    gc_merged_out = []
    gc_orphaned_tabs = []
    gc_merged = getattr(args, 'gc_merged', False)
    for e in list(worktree_registry.entries(project_path)):
        wt_path = Path(e.get("worktree_path", ""))
        if not wt_path.exists():
            worktree_registry.unregister(project_path, session=e.get("session"))
            _cleanup_empty_project_dir(wt_path, worktree_dir)
            removed.append(e.get("session"))
            continue
        if gc_merged and e.get("branch"):
            if _branch_merge_state(project_path, e["branch"], e.get("base")) == "merged":
                # force_branch=True here does NOT mean "delete even if unmerged" —
                # merge state was JUST confirmed above by this same sweep, so it
                # just tells _teardown_entry to skip re-running the (redundant)
                # merge check itself.
                result = _teardown_entry(
                    project_path, worktree_dir, e["session"], wt_path,
                    e["branch"], e.get("base"), force_branch=True,
                    # A pane entry's session is the OWNING orchestrator (#837) —
                    # GC'ing a merged worker branch must not kill it.
                    kill_session=e.get("topology") != "pane",
                )
                if result["success"]:
                    gc_merged_out.append(e["session"])
                    for t in result.get("orphaned_tabs") or []:
                        gc_orphaned_tabs.append({**t, "session": e["session"]})
    subprocess.run(["git", "-C", str(project_path), "worktree", "prune"],
                   capture_output=True)
    if json_mode:
        _output_json({"success": True, "pruned": removed, "gc_merged": gc_merged_out,
                      "orphaned_tabs": gc_orphaned_tabs})
    else:
        if removed:
            print(f"Pruned {len(removed)} stale registry entr{'y' if len(removed) == 1 else 'ies'}: {', '.join(removed)}")
        if gc_orphaned_tabs:
            ids = ", ".join(f"{t['session']}:{t.get('tab_id', '?')}" for t in gc_orphaned_tabs)
            print(f"WARNING: {len(gc_orphaned_tabs)} claude-in-chrome tab(s) never closed by GC'd sessions: "
                  f"{ids} — close via tabs_close_mcp")
        if gc_merged_out:
            print(f"GC'd {len(gc_merged_out)} merged worktree{'s' if len(gc_merged_out) != 1 else ''}: {', '.join(gc_merged_out)}")
        if not removed and not gc_merged_out:
            print("Nothing to prune.")
    return 0


def scan_dangling_worktrees(rows: list[dict]) -> list[dict]:
    """Flag LIVE worker-kind registry entries with an OPEN PR and no live
    recorded parent — a PR nobody is positioned to review/merge (the
    concrete failure mode from #716: a rootless-but-still-subordinate
    session that correctly refuses to self-merge, so its green PR just
    dangles).

    ``rows`` are registry entries (as from ``worktree_registry.entries()`` /
    ``all_entries()``), each carrying at least "session", "branch",
    "project", "kind". Shared by ``agentwire worktree --dangling`` and
    ``doctor``.

    Orchestrator-kind entries are SKIPPED — a durable orchestrator roots by
    design (#716's joint default) and is itself the reviewer/merger, so a
    parentless orchestrator with an open PR is its normal, healthy
    lifecycle, not a dangling failure. Entries registered before this field
    existed carry ``kind=None`` and are treated as worker-like (the
    pre-#716 default for a worktree), which only widens the scan, never
    narrows it, for old entries.

    This is NOT the same "orphan" as --list's state badge (a dead session
    whose worktree dir is still on disk) — this flags a LIVE session with
    nothing above it positioned to act on its work. Deliberately a shallow
    "has any live recorded parent" check (recorded creator, or the
    `.agentwire.yml parent:` fallback — the same precedence
    `_display_parent`/prompt-routing already use), not a full
    orchestrator-role verification of that parent (that's not durably
    stored anywhere today and is out of scope — see #716's deferred
    merge-authority-per-edge north star).

    `reviewer`-kind entries need no equivalent skip (#827): a reviewer never
    opens a PR at all, so the `gh pr view <branch>` check below already comes
    back empty-handed for it — and its pane/main-topology default means it's
    typically never even in this registry (only worktree-topology sessions
    are registered here); a reviewer on worktree topology just falls through
    the same PR-existence check as any other never-PRing entry.

    ``topology == "pane"`` entries (#837) are SKIPPED too: a worker pane's
    parent is pane 0 of the very session recorded on the entry, so the
    liveness check below already proves the parent is live whenever the
    entry qualifies at all — it can never be dangling by this definition.
    """
    if not shutil.which("gh"):
        return []
    dangling = []
    for r in rows:
        if r.get("kind") == "orchestrator" or r.get("topology") == "pane":
            continue
        session = r.get("session", "")
        branch = r.get("branch")
        if not branch or not tmux_session_exists(session):
            continue
        result = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "state,url"],
            cwd=r.get("project"), capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue
        try:
            pr = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if str(pr.get("state", "")).upper() != "OPEN":
            continue
        parent = _display_parent(session, r.get("worktree_path", ""))
        if parent and tmux_session_exists(parent):
            continue
        dangling.append({
            "session": session, "branch": branch, "project": r.get("project"),
            "pr_url": pr.get("url"), "created_by": parent or None,
            "reason": "no recorded parent" if not parent else "parent not live",
        })
    return dangling


def _worktree_dangling(args, project_path: Path, json_mode: bool) -> int:
    """Scan for live worktree sessions with a dangling (open, unparented) PR (--dangling)."""
    show_all = getattr(args, 'all', False)
    if show_all:
        rows = worktree_registry.all_entries()
    else:
        rows = [dict(e, project=str(project_path)) for e in worktree_registry.entries(project_path)]
    dangling = scan_dangling_worktrees(rows)
    if json_mode:
        _output_json({"success": True, "dangling": dangling})
        return 0
    if not dangling:
        scope = "any repo" if show_all else str(project_path)
        print(f"No dangling worktree sessions for {scope}.")
        return 0
    print(f"{len(dangling)} dangling worktree session(s) — open PR, no live parent to act on it:")
    for d in dangling:
        print(f"  {d['session']:<32} branch={d['branch']}  {d.get('pr_url', '')}  ({d['reason']})")
    return 0


def _branch_merge_state(project_path: Path, branch: str, base: str | None) -> str:
    """Best-effort "is `branch` safe to delete" check. Returns "merged" / "open" / "unknown".

    Prefers `gh pr view` — a squash/rebase merge on GitHub creates a NEW commit
    that a plain ancestor check won't recognize, so pure git can false-negative
    on the single most common merge workflow. Falls back to a git merge-base
    ancestor check against `origin/<base>` when gh is unavailable/unauthenticated
    or no PR was ever opened for the branch (base is required for the fallback).
    """
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "state,headRefOid"],
            cwd=project_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            try:
                pr = json.loads(result.stdout)
            except json.JSONDecodeError:
                pr = {}
            state = str(pr.get("state", "")).upper()
            if state == "MERGED":
                # `gh pr view <branch>` resolves by head-branch NAME, not by
                # ref identity — a long-merged PR whose remote branch was
                # since deleted can still be the match if a LATER branch
                # reuses the same name (common here: generic worktree names
                # like "fix-bug" recur). Only trust MERGED when the PR's
                # headRefOid still matches this branch's current tip; a
                # mismatch means it's a stale namesake PR, not this branch —
                # fall through to the git-ancestor check instead of deleting
                # real unmerged work.
                tip = subprocess.run(
                    ["git", "-C", str(project_path), "rev-parse", branch],
                    capture_output=True, text=True,
                )
                if tip.returncode == 0 and tip.stdout.strip() and tip.stdout.strip() == pr.get("headRefOid"):
                    return "merged"
            elif state in ("OPEN", "CLOSED"):
                return "open"
    if base:
        result = subprocess.run(
            ["git", "-C", str(project_path), "merge-base", "--is-ancestor", branch, f"origin/{base}"],
            capture_output=True,
        )
        if result.returncode == 0:
            return "merged"
    return "unknown"


def _open_pr_number(project_path: Path, branch: str) -> int | None:
    """Best-effort: the OPEN PR number for `branch`, or None if there isn't one
    (or `gh` is unavailable/unauthenticated) — never hard-fails, matching
    `_branch_merge_state`'s best-effort posture.
    """
    if not shutil.which("gh"):
        return None
    result = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "state,number"],
        cwd=project_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        pr = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if str(pr.get("state", "")).upper() == "OPEN":
        return pr.get("number")
    return None


def _delete_branch_if_safe(
    project_path: Path, branch: str, base: str | None, force: bool = False, close_pr_branch: bool = False,
) -> tuple[bool, str]:
    """Best-effort local + remote branch cleanup. Never touches an unmerged branch
    unless `force` — deleting an unmerged branch would silently drop real work.

    `force` bypasses the merge-state check but NOT an open PR: deleting the
    remote head branch of an OPEN PR silently closes it (GitHub loses the head
    ref), which is a different, more surprising destruction than "dropping
    unmerged local work" — so it needs its own explicit override,
    `close_pr_branch` (#756).

    Returns (deleted, note) — note explains a skip, or names the confirmed state
    on delete.
    """
    if not force:
        state = _branch_merge_state(project_path, branch, base)
        if state != "merged":
            return False, f"not confirmed merged (state={state}); kept — pass --force-delete-branch to override"
    elif not close_pr_branch:
        open_pr = _open_pr_number(project_path, branch)
        if open_pr is not None:
            return False, (
                f"branch {branch} has OPEN PR #{open_pr} — force-deleting closes it. "
                "Merge/close the PR first, or pass --close-pr-branch to override."
            )

    local = subprocess.run(
        ["git", "-C", str(project_path), "branch", "-D", branch],
        capture_output=True, text=True,
    )
    if local.returncode != 0 and "not found" not in local.stderr.lower():
        return False, f"local branch delete failed: {local.stderr.strip()}"

    # Best-effort — the remote ref may already be gone (e.g. GitHub's own
    # "delete branch" button after merge); a failure here doesn't undo the
    # local delete above.
    subprocess.run(
        ["git", "-C", str(project_path), "push", "origin", "--delete", branch],
        capture_output=True, text=True,
    )
    return True, "merged" if not force else "forced"


def _teardown_entry(
    project_path: Path, worktree_dir: Path, session_name: str, worktree_path: Path,
    branch: str | None, base: str | None, *, keep_branch: bool = False, force_branch: bool = False,
    close_pr_branch: bool = False, kill_session: bool = True,
) -> dict:
    """Core atomic teardown: force-remove the worktree + prune, kill session,
    unregister, best-effort branch cleanup. Shared by --remove and
    --prune --gc-merged so both paths go through the exact same steps (#717).

    Never reports worktree_removed=True while the dir is still on disk: if
    `git worktree remove --force` can't clear it, this fails LOUDLY
    (success: False) and leaves the registry entry in place, instead of
    silently "unregistering" an orphan — UNLESS git *already had no record of
    the path before we touched it* (see `is_registered_worktree`), in which
    case there's no registered worktree left to protect and the leftover
    directory (a stale build-tool cache, typically) is hard-deleted instead.

    That pre-check matters: `git worktree prune` below runs unconditionally
    and clears any entry git marks "prunable" (e.g. one whose linked `.git`
    file was deleted) as a side effect of THIS call — checking registration
    after prune would misread a worktree that was real and registered right
    up until this attempt as a pre-existing orphan, and hard-delete content
    the fail-loud path exists to protect.

    The worktree removal is attempted BEFORE the session is killed, and the
    session is only killed once removal is confirmed — never the reverse.
    Killing first would leave a live session's worktree+branch orphaned on
    disk with no session left to notice, on any removal failure (bad
    project_path, a stuck worktree, ...) (#740).

    ``kill_session=False`` for a "pane"-topology entry (#837): its
    ``session_name`` is the OWNING session, whose pane 0 is an unrelated
    orchestrator — killing it would take down the wrong thing entirely.

    ``worktree_existed`` in the result distinguishes "removed it" from
    "there was nothing at that path", so no caller can print a removal that
    didn't happen (#855).
    """
    was_registered = is_registered_worktree(project_path, worktree_path)
    worktree_existed = worktree_path.exists()

    # Force-remove the git worktree, then prune admin files either way.
    _, remove_error = remove_worktree(project_path, worktree_path, force=True)
    subprocess.run(["git", "-C", str(project_path), "worktree", "prune"], capture_output=True)

    hard_deleted_orphan = False
    if worktree_path.exists() and not was_registered:
        shutil.rmtree(worktree_path, ignore_errors=True)
        hard_deleted_orphan = not worktree_path.exists()

    if worktree_path.exists():
        reason = remove_error or "worktree directory still present after `git worktree remove --force`"
        return {"success": False, "session": session_name, "path": str(worktree_path),
                "killed": False, "worktree_removed": False,
                "worktree_existed": worktree_existed, "error": reason}

    killed = False
    if kill_session and tmux_session_exists(session_name):
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
        killed = True

    # Removing a project's last worktree also removes the now-empty
    # ~/worktrees/<project>/ dir.
    _cleanup_empty_project_dir(worktree_path, worktree_dir)

    branch_deleted, branch_note = (False, "")
    if branch and not keep_branch:
        branch_deleted, branch_note = _delete_branch_if_safe(
            project_path, branch, base, force=force_branch, close_pr_branch=close_pr_branch,
        )

    # Only unregister once the dir is confirmed gone (see docstring).
    worktree_registry.unregister(project_path, session=session_name, worktree_path=worktree_path)

    # Crash backstop (#717): agentwire can't call `tabs_close_mcp` itself (that
    # MCP server runs inside the calling agent's client, not this process), so
    # we can't close these — just report them so the caller can. A session
    # that closed its own tabs and untracked them normally leaves nothing here.
    orphaned_tabs = chrome_tabs.clear(session_name)

    return {"success": True, "session": session_name, "path": str(worktree_path),
            "killed": killed, "worktree_removed": worktree_existed,
            "worktree_existed": worktree_existed,
            "branch": branch, "branch_deleted": branch_deleted, "branch_note": branch_note,
            "orphaned_tabs": orphaned_tabs, "hard_deleted_orphan": hard_deleted_orphan}


def _unresolved_remove_error(name: str, project_path: Path, ref: WorktreeRef) -> str:
    """Message for a --remove that resolved to nothing real (#855).

    Names what git *does* know, so the operator can see immediately whether
    they're in the wrong repo or just used the wrong name — the alternative
    (a success line for a no-op) is how a teardown reports a session, worktree
    and branch gone while all three are still alive.
    """
    known = linked_git_worktrees(project_path)
    if known:
        listing = "\n".join(
            f"  {e['path']}  branch={e['branch'] or '(detached)'}" for e in known
        )
        hint = f"git knows these worktrees for {project_path}:\n{listing}"
    else:
        hint = f"git reports no linked worktrees for {project_path}."
    return (
        f"No worktree found for '{name}' in {project_path} — nothing removed. "
        f"(Neither the agentwire registry nor `git worktree list` knows it; the "
        f"conventional path {ref.path} does not exist.) {hint}"
    )


def _worktree_remove(args, project_path: Path, worktree_dir: Path, json_mode: bool) -> int:
    """Teardown: force-remove the worktree + prune, kill the session, unregister,
    and (best-effort) delete the branch — one atomic call (#717).

    The target path comes from GIT (via ``_resolve_worktree_entry``), never
    from string-building the documented convention. When nothing real
    resolves, this FAILS instead of tearing down a guess — a false success
    here is the dangerous direction, since the operator then never notices the
    surviving session/worktree/branch, and the #756 merged-branch safety
    checks are skipped along with everything else (#855).
    """
    name = getattr(args, 'name', None)
    if not name:
        return _output_result(False, json_mode, "Usage: agentwire worktree --remove <name|session>")

    ref = _resolve_worktree_entry(name, project_path, worktree_dir)

    # Guessed path + nothing on disk + no live session = nothing to remove.
    if not ref.found and not ref.path.exists() and not tmux_session_exists(ref.session):
        return _output_result(False, json_mode, _unresolved_remove_error(name, project_path, ref))

    # Capture the branch before the worktree disappears. Registry/git first
    # (recorded at creation); fall back to the worktree's own HEAD so branch
    # cleanup still works for a hand-created worktree.
    branch = ref.branch
    base = ref.base
    if branch is None and ref.path.exists():
        branch = worktree_status(ref.path).get("branch")

    result = _teardown_entry(
        project_path, worktree_dir, ref.session, ref.path, branch, base,
        keep_branch=getattr(args, 'keep_branch', False),
        force_branch=getattr(args, 'force_delete_branch', False),
        close_pr_branch=getattr(args, 'close_pr_branch', False),
        # A pane entry's session belongs to the orchestrator, not the worktree.
        kill_session=ref.topology != "pane",
    )
    result["resolved_by"] = ref.source

    if json_mode:
        _output_json(result)
    elif not result["success"]:
        print(f"FAILED to remove worktree at {result['path']}: {result['error']}"
              + (" (session killed)" if result["killed"] else ""))
    else:
        removal = (f"; worktree {result['path']} removed" if result.get("worktree_existed")
                   else f"; no worktree at {result['path']} (already gone)")
        msg = (f"Removed worktree session '{result['session']}'"
               + (" (killed live session)" if result["killed"] else "")
               + removal
               + (" (git had no registration for it — hard-deleted the leftover directory)"
                  if result.get("hard_deleted_orphan") else ""))
        if result["branch"]:
            msg += f"; branch '{result['branch']}' " + ("deleted" if result["branch_deleted"] else f"kept ({result['branch_note']})")
        if result["orphaned_tabs"]:
            ids = ", ".join(t.get("tab_id", "?") for t in result["orphaned_tabs"])
            msg += (f"; WARNING: {len(result['orphaned_tabs'])} claude-in-chrome tab(s) never closed "
                    f"by this session: {ids} — close via tabs_close_mcp")
        print(msg)
    return 0 if result["success"] else 1


def cmd_fork(args) -> int:
    """Fork a session into a new worktree with copied Claude context.

    Creates a new worktree from current branch state and optionally
    copies Claude session file for conversation continuity.

    Supports remote sessions with session@machine format.
    """
    source_full = args.source
    target_full = args.target
    json_mode = getattr(args, 'json', False)

    if not source_full or not target_full:
        return _output_result(False, json_mode, "Usage: agentwire fork -s <source> -t <target>")

    # Parse session names
    source_project, source_branch, source_machine = parse_session_name(source_full)
    target_project, target_branch, target_machine = parse_session_name(target_full)

    # Non-worktree fork: both have no branch (same directory, fork Claude context)
    # Worktree fork: at least one has a branch (create new worktree)
    is_non_worktree_fork = not source_branch and not target_branch

    # For worktree forks, validate project names match and target has a branch
    if not is_non_worktree_fork:
        if source_project != target_project:
            return _output_result(False, json_mode, f"For worktree forks, source and target must be same project (got {source_project} vs {target_project})")
        if not target_branch:
            return _output_result(False, json_mode, "For worktree forks, target must include a branch name (e.g., project/new-branch)")

    # Machines must match
    if source_machine != target_machine:
        return _output_result(False, json_mode, f"Source and target must be on same machine (got {source_machine} vs {target_machine})")

    machine_id = source_machine

    # Load config
    config = load_config()
    projects_dir = Path(config.get("projects", {}).get("dir", "~/projects")).expanduser()
    worktrees_config = config.get("projects", {}).get("worktrees", {})
    worktree_suffix = worktrees_config.get("suffix", "-worktrees")

    # Build session names (preserve slashes, convert dots to underscores)
    if source_branch:
        source_session = f"{source_project}/{source_branch}".replace(".", "_")
    else:
        source_session = source_project.replace(".", "_")

    if target_branch:
        target_session = f"{target_project}/{target_branch}".replace(".", "_")
    else:
        # Non-worktree fork: use target project name directly
        target_session = target_project.replace(".", "_")

    if machine_id:
        # Remote fork
        machine = _get_machine_config(machine_id)
        if machine is None:
            return _output_result(False, json_mode, f"Machine '{machine_id}' not found in machines.json")

        remote_projects_dir = machine.get("projects_dir", "~/projects")

        # Build paths
        project_path = f"{remote_projects_dir}/{source_project}"
        if source_branch:
            source_path = f"{remote_projects_dir}/{source_project}{worktree_suffix}/{source_branch}"
        else:
            source_path = project_path
        target_path = f"{remote_projects_dir}/{target_project}{worktree_suffix}/{target_branch}"

        # Check if target already exists
        check_cmd = f"test -d {shlex.quote(target_path)}"
        result = _run_remote(machine_id, check_cmd)
        if result.returncode == 0:
            return _output_result(False, json_mode, f"Target worktree already exists: {target_path}")

        # Create new worktree from source
        create_cmd = f"cd {shlex.quote(source_path)} && mkdir -p $(dirname {shlex.quote(target_path)}) && git worktree add -b {shlex.quote(target_branch)} {shlex.quote(target_path)}"
        result = _run_remote(machine_id, create_cmd)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create worktree: {result.stderr}")

        # Determine posture from --posture flag or source config
        source_config = load_project_config(Path(source_path))
        posture, st_err = _resolve_posture_or_config(args, source_config)
        if st_err:
            return _output_result(False, json_mode, st_err)

        # The fork target is a worktree (project/branch) → worker kind on
        # worktree topology, so resolve_roles stacks the non-overridable
        # safety etiquette under the source's roles instead of copying them
        # raw (which dropped the kind's intrinsic contract). Kind derived
        # from the target name.
        project_roles = list(source_config.roles) if source_config and source_config.roles else None
        role_names = _resolve_and_soul_worktree_roles(bool(target_branch), project_roles=project_roles)
        roles = None
        if role_names:
            roles, _ = load_roles(role_names, Path(source_path))

        # Build agent command
        agent = build_agent_command(posture, roles)
        agent.env.update(parse_env_args(getattr(args, 'env', None)))

        agent_cmd = agent.command

        result = _launch_tmux_session(target_session, target_path, agent.env, agent_cmd, machine_id)
        if result.returncode != 0:
            return _output_result(False, json_mode, f"Failed to create session: {result.stderr}")

        if json_mode:
            _output_json({
                "success": True,
                "session": f"{target_session}@{machine_id}",
                "path": target_path,
                "branch": target_branch,
                "machine": machine_id,
                "forked_from": source_full,
            })
        else:
            print(f"Forked '{source_full}' to '{target_session}' on {machine_id}")
            print(f"  Path: {target_path}")

        return 0

    # Local fork
    # Build paths
    project_path = projects_dir / source_project

    # Handle non-worktree fork (same directory, different Claude session)
    if is_non_worktree_fork:
        # Check if source session exists
        check_source = subprocess.run(
            ["tmux", "has-session", "-t", source_session],
            capture_output=True
        )
        if check_source.returncode != 0:
            return _output_result(False, json_mode, f"Source session '{source_session}' does not exist")

        # For non-worktree forks, use the source session's actual CWD.
        # The session name may not match the project directory name (e.g., a session
        # named "aw-context-source" running in ~/projects/aw-feature-test/).
        cwd_result = subprocess.run(
            ["tmux", "display-message", "-t", source_session, "-p", "#{pane_current_path}"],
            capture_output=True, text=True,
        )
        if cwd_result.returncode == 0 and cwd_result.stdout.strip():
            fork_path = Path(cwd_result.stdout.strip())
        else:
            fork_path = project_path

        if not fork_path.exists():
            return _output_result(False, json_mode, f"Source path does not exist: {fork_path}")

        # Check if target session already exists
        check_target = subprocess.run(
            ["tmux", "has-session", "-t", target_session],
            capture_output=True
        )
        if check_target.returncode == 0:
            return _output_result(False, json_mode, f"Target session '{target_session}' already exists")

        # Determine posture from --posture flag or source config (before
        # session creation, so we can inject env via `tmux new-session -e K=V`).
        source_project_config = load_project_config(fork_path)
        posture, st_err = _resolve_posture_or_config(args, source_project_config)
        if st_err:
            return _output_result(False, json_mode, st_err)

        # Non-worktree fork: target has no branch → orchestrator kind (a
        # replaceable persona), so source roles take precedence as usual.
        # Routed through resolve_roles for one consistent precedence path.
        project_roles = list(source_project_config.roles) if source_project_config and source_project_config.roles else None
        role_names = _resolve_and_soul_worktree_roles(bool(target_branch), project_roles=project_roles)
        roles = None
        if role_names:
            roles, _ = load_roles(role_names, fork_path)

        # Find the conversation JSONL for the source session to enable context inheritance.
        # Uses history.jsonl filtered by the source tmux session's creation time to identify
        # the correct JSONL even when multiple sessions share the same project directory.
        import json as _json
        resume_session_id = None

        # Get source session creation timestamp
        created_result = subprocess.run(
            ["tmux", "display-message", "-t", source_session, "-p", "#{session_created}"],
            capture_output=True, text=True,
        )
        session_created_unix = int(created_result.stdout.strip() or 0) if created_result.returncode == 0 else 0
        session_created_ms = session_created_unix * 1000

        history_file = Path.home() / ".claude" / "history.jsonl"
        if session_created_ms > 0 and history_file.exists():
            # Find sessions for this project that started AFTER the source tmux session was created.
            # The session whose first message is closest to (and after) creation time is the source.
            first_seen: dict[str, int] = {}
            for line in history_file.read_text().strip().splitlines():
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                    if entry.get("project") != str(fork_path):
                        continue
                    sid = entry.get("sessionId", "")
                    ts = entry.get("timestamp", 0)
                    if sid and (sid not in first_seen or ts < first_seen[sid]):
                        first_seen[sid] = ts
                except _json.JSONDecodeError:
                    continue

            # Pick the session with earliest first_seen >= session_created_ms
            candidates = [(sid, ts) for sid, ts in first_seen.items() if ts >= session_created_ms]
            if candidates:
                candidates.sort(key=lambda x: x[1])
                resume_session_id = candidates[0][0]

        if not resume_session_id:
            # Fallback: most recently modified JSONL in the project dir
            claude_projects_dir = Path.home() / ".claude" / "projects"
            from .history import encode_project_path as _encode_project_path
            encoded_path = _encode_project_path(str(fork_path))
            session_dir = claude_projects_dir / encoded_path
            if session_dir.exists():
                jsonl_files = sorted(session_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
                if jsonl_files:
                    resume_session_id = jsonl_files[0].stem

        # Build agent command — resume_session_id (if any) inserts
        # --resume/--fork-session right after `claude` so the forked session
        # starts with the source conversation in context.
        agent = build_agent_command(posture, roles, resume_session_id=resume_session_id)
        agent.env.update(parse_env_args(getattr(args, 'env', None)))

        agent_cmd = agent.command

        # Create new tmux session (env injected at creation time, cd, agent
        # start — see _launch_tmux_session).
        _launch_tmux_session(target_session, fork_path, agent.env, agent_cmd)

        if json_mode:
            _output_json({
                "success": True,
                "session": target_session,
                "path": str(fork_path),
                "branch": None,
                "machine": None,
                "forked_from": source_full,
                "resumed_from": resume_session_id,
            })
        else:
            print(f"Forked '{source_full}' to '{target_session}' (same directory)")
            print(f"  Path: {fork_path}")
            if resume_session_id:
                print(f"  Resumed from: {resume_session_id}")

        return 0

    # Worktree fork logic
    if source_branch:
        source_path = projects_dir / f"{source_project}{worktree_suffix}" / source_branch
    else:
        source_path = project_path
    target_path = projects_dir / f"{target_project}{worktree_suffix}" / target_branch

    # Check if target already exists
    if target_path.exists():
        return _output_result(False, json_mode, f"Target worktree already exists: {target_path}")

    # Check source exists
    if not source_path.exists():
        return _output_result(False, json_mode, f"Source path does not exist: {source_path}")

    # Create new worktree from source
    fork_commit = getattr(args, "commit", None) or None
    target_session = f"{target_project}/{target_branch}".replace(".", "_")
    fork_kind = derive_session_kind(True, None)
    ok, err = create_and_register_worktree(
        source_path,  # Use source as base for the worktree
        branch=target_branch,
        worktree_path=target_path,
        session=target_session,
        base=source_branch,
        kind=fork_kind,
        auto_create_branch=True,
        commit=fork_commit,
    )
    if not ok and project_path.exists():
        # Retry against the canonical repo — the registry is keyed by repo, so
        # this also records the entry under the right project.
        ok, err = create_and_register_worktree(
            project_path,
            branch=target_branch,
            worktree_path=target_path,
            session=target_session,
            base=source_branch,
            kind=fork_kind,
            auto_create_branch=True,
            commit=fork_commit,
        )

    if not ok:
        return _output_result(False, json_mode, err or f"Failed to create worktree for branch '{target_branch}'")

    # Determine posture from --posture flag or source config (before session
    # creation, so we can inject env via `tmux new-session -e K=V`).
    config_path = source_path if source_path != project_path else project_path
    source_config = load_project_config(config_path)
    posture, st_err = _resolve_posture_or_config(args, source_config)
    if st_err:
        return _output_result(False, json_mode, st_err)

    # Worktree fork: target is project/branch → worker kind on worktree
    # topology, so resolve_roles stacks the non-overridable
    # isolation/verify/draft-PR/notify etiquette under the source's roles.
    # Kind derived from the target name.
    project_roles = list(source_config.roles) if source_config and source_config.roles else None
    role_names = _resolve_and_soul_worktree_roles(bool(target_branch), project_roles=project_roles)
    roles = None
    if role_names:
        roles, _ = load_roles(role_names, config_path)

    # Build agent command
    agent = build_agent_command(posture, roles)
    agent.env.update(parse_env_args(getattr(args, 'env', None)))
    agent_cmd = agent.command

    # Create new session (env injected at creation time, cd, agent start —
    # see _launch_tmux_session).
    _launch_tmux_session(target_session, target_path, agent.env, agent_cmd)

    if json_mode:
        _output_json({
            "success": True,
            "session": target_session,
            "path": str(target_path),
            "branch": target_branch,
            "machine": None,
            "forked_from": source_full,
        })
    else:
        print(f"Forked '{source_full}' to '{target_session}'")
        print(f"  Path: {target_path}")
        print(f"Attach with: tmux attach -t {target_session}")

    return 0


def register_session_parser(subparsers) -> None:
    """Register the session-lifecycle commands (new/fork/worktree/recreate/session-defaults)."""
    from .core import _add_posture_flag

    # === new command (top-level) ===
    new_parser = subparsers.add_parser("new", help="Create new Claude Code session")
    new_parser.add_argument("-s", "--session", required=True, help="Session name (project, project/branch, or project/branch@machine)")
    new_parser.add_argument("-p", "--path", help="Working directory (default: ~/projects/<name>)")
    new_parser.add_argument("-f", "--force", action="store_true", help="Replace existing session")
    new_parser.add_argument("--allow-shared-dir", action="store_true",
                            help="Allow attaching to a directory that already has active sessions "
                                 "(unlike --force, never replaces an existing same-name session)")
    # Posture is the single session axis (#729).
    _add_posture_flag(new_parser)
    # Roles
    new_parser.add_argument("--roles", help="Comma-separated roles (replaces the default orchestrator persona)")
    new_parser.add_argument("--kind", choices=["orchestrator", "worker", "reviewer"],
                            help="Override the derived session role (advanced). A project/branch name "
                                 "normally derives 'worker' (draft-PR + notify etiquette on worktree "
                                 "topology). Pass 'orchestrator' for a worktree that is finalized "
                                 "externally — e.g. the scheduler, which opens/reaps the PR itself, so "
                                 "the task agent must NOT open its own. Pass 'reviewer' for a PR-review "
                                 "station: adversarially reviews a sibling session's PR and never opens "
                                 "or merges one of its own (non-overridable, like worker's rail — "
                                 "inverted); parented rooting (not rooted like orchestrator), pane/main "
                                 "topology by default.")
    new_parser.add_argument("--no-soul", dest="no_soul", action="store_true", help="Skip soul personality role injection for this session")
    new_parser.add_argument("--model", help="Model override (e.g., haiku, sonnet, opus)")
    new_parser.add_argument("--persist", action="store_true", help="Write the resolved posture/--roles to .agentwire.yml (default: session-level override only)")
    new_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var via `tmux set-environment` (repeatable, keeps secrets out of `ps`)")
    # Worktree-only flags (default None so they can be rejected unless the
    # session name is a project/branch worktree).
    new_parser.add_argument("--base", default=None, help="Worktree sessions only (project/branch name): base branch to fork from (default: main)")
    new_parser.add_argument("--pull-first", dest="pull_first", action="store_true", default=None, help="Worktree sessions only: fetch origin/<base> before branching (default)")
    new_parser.add_argument("--no-pull-first", dest="pull_first", action="store_false", help="Skip the fetch — branch from the local copy of <base> as-is")
    new_parser.add_argument("--first-message", dest="first_message",
                            help="After the agent boots, deliver this as its first message (verified; local sessions only)")
    new_parser.add_argument("--created-by", dest="created_by",
                            help="Force this session's recorded creator/parent for prompt routing, "
                                 "overriding the default same-project-only inheritance below; pass "
                                 "'' to force standalone (no parent) regardless of project")
    new_parser.add_argument("--no-cohort", dest="no_cohort", action="store_true",
                            help="Do not enroll this session in the calling session's fan-out cohort (#852) — a genuinely fire-and-forget spawn the caller will never wait on or tear down")
    new_parser.add_argument("--caller-session", dest="caller_session",
                            help="Internal: candidate caller session for computing the default "
                                 "--created-by — inherited only if this session's project matches "
                                 "the caller's (#715); ignored when --created-by is given explicitly. "
                                 "Default: the calling tmux session. MCP forwards this explicitly, "
                                 "since the CLI subprocess can't reliably auto-detect the caller "
                                 "across the MCP boundary.")
    new_parser.add_argument("--json", action="store_true", help="Output as JSON")
    new_parser.set_defaults(func=cmd_new)

    # === session-defaults command (resolver — backs the portal endpoint) ===
    sd_parser = subparsers.add_parser("session-defaults", help="Resolve a new session's composed type + intrinsic roles (JSON)")
    sd_parser.add_argument("--kind", choices=["orchestrator", "worker", "reviewer"], default="orchestrator",
                           help="Spawn-verb role (default: orchestrator = `agentwire new`)")
    _add_posture_flag(sd_parser)
    sd_parser.add_argument("--json", action="store_true", default=True, help="Output as JSON (default)")
    sd_parser.set_defaults(func=cmd_session_defaults)

    # === recreate command (top-level) ===
    recreate_parser = subparsers.add_parser("recreate", help="Destroy and recreate session with fresh worktree")
    recreate_parser.add_argument("-s", "--session", required=True, help="Session name (project/branch or project/branch@machine)")
    _add_posture_flag(recreate_parser)  # default: inherit the source config's posture
    recreate_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var via `tmux set-environment` (repeatable)")
    recreate_parser.add_argument("--json", action="store_true", help="Output as JSON")
    recreate_parser.set_defaults(func=cmd_recreate)

    # === fork command (top-level) ===
    fork_parser = subparsers.add_parser("fork", help="Fork a session into a new worktree")
    fork_parser.add_argument("-s", "--source", required=True, help="Source session (project or project/branch)")
    fork_parser.add_argument("-t", "--target", required=True, help="Target session (must include branch: project/new-branch)")
    _add_posture_flag(fork_parser)  # default: inherit the source config's posture
    fork_parser.add_argument("--commit", metavar="REF", help="Fork from this commit/ref instead of HEAD (e.g. abc123, main~5)")
    fork_parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var via `tmux set-environment` (repeatable)")
    fork_parser.add_argument("--json", action="store_true", help="Output as JSON")
    fork_parser.set_defaults(func=cmd_fork)

    # === worktree command (+ the `orchestrator` sugar verb below) ===
    def _add_worktree_flags(parser) -> None:
        """Flags shared by `worktree` and its `orchestrator` sugar verb."""
        parser.add_argument("--base", "-b", help="Base branch to fork from (default: config worktree.default_base, else the repo's origin/HEAD default branch)")
        parser.add_argument("--current", "-c", action="store_true", help="Fork from the repo's current branch (used when --base is not given)")
        parser.add_argument("--existing", "-e", action="store_true", help="Checkout an existing branch (no new branch created)")
        parser.add_argument("--ref", help="Detach at a specific ref (tag, commit, branch)")
        parser.add_argument("--project", "-p", help="Path to git repo (default: config worktree.default_project, else the git root of cwd)")
        parser.add_argument("--list", action="store_true", help="List registered worktree sessions for this repo (--all for every repo)")
        parser.add_argument("--watch", action="store_true", help="Watch registered worktree sessions in a loop and report status")
        parser.add_argument("--status", action="store_true", help="Show read-only git status (dirty/ahead/behind/pushed) for a worktree session")
        parser.add_argument("--dangling", action="store_true",
                            help="Scan for LIVE worktree sessions with an OPEN PR and no live recorded "
                                 "parent — a PR nobody is positioned to review/merge (--all for every "
                                 "repo). Distinct from --list's 'orphan' state (dead session, disk "
                                 "remnant) — this flags a LIVE session no one is watching.")
        parser.add_argument("--remove", action="store_true", help="Atomic teardown: force-remove the worktree, kill the session, delete the branch once merged, and unregister (cleanup/recovery)")
        parser.add_argument("--keep-branch", action="store_true", help="With --remove: skip branch cleanup entirely (default: best-effort delete once confirmed merged)")
        parser.add_argument("--force-delete-branch", action="store_true", help="With --remove: delete the branch (local + remote) even if not confirmed merged. Refuses if the branch has an OPEN PR (would silently close it) unless --close-pr-branch is also given")
        parser.add_argument("--close-pr-branch", action="store_true", help="With --remove --force-delete-branch: also delete a branch that has an OPEN PR, closing it (explicit escape hatch for #756's guard)")
        parser.add_argument("--prune", action="store_true", help="Drop registry entries whose worktree is gone + git worktree prune")
        parser.add_argument("--gc-merged", action="store_true", help="Tear down (session/worktree/branch) any registered entry whose branch is confirmed merged; implies --prune (runs standalone too)")
        parser.add_argument("--all", action="store_true", help="With --list/--dangling: include worktree sessions across every repo")
        _add_posture_flag(parser)
        parser.add_argument("--roles", help="Comma-separated roles, STACKED on top of the always-present worker/reviewer etiquette (kind=orchestrator: REPLACES the default persona instead)")
        parser.add_argument("--prompt", help="First message to deliver once the agent is booted/ready (spawn + seed in one step)")
        parser.add_argument("--model", help="Model override (e.g., haiku, sonnet, opus)")
        parser.add_argument("--env", action="append", metavar="KEY=VAL", help="Inject env var via `tmux set-environment` (repeatable)")
        parser.add_argument("--created-by", dest="created_by",
                            help="Force this session's recorded creator/parent for prompt routing, "
                                 "overriding the default below; pass '' to force standalone (no "
                                 "parent) regardless of project")
        parser.add_argument("--no-cohort", dest="no_cohort", action="store_true",
                            help="Do not enroll this session in the calling session's fan-out cohort (#852) — a genuinely fire-and-forget spawn the caller will never wait on or tear down")
        parser.add_argument("--caller-session", dest="caller_session",
                            help="Internal: candidate caller session for computing the default "
                                 "--created-by — inherited only if this worktree's project matches "
                                 "the caller's (#715); ignored when --created-by is given explicitly. "
                                 "Default: the calling tmux session. MCP forwards this explicitly, "
                                 "since the CLI subprocess can't reliably auto-detect the caller "
                                 "across the MCP boundary.")
        parser.add_argument("--json", action="store_true", help="Output as JSON")

    wt_parser = subparsers.add_parser("worktree", help="Create a git worktree + session in one command")
    wt_parser.add_argument("name", nargs="?", help="Name (becomes branch name in default mode, session suffix always)")
    wt_parser.add_argument("--kind", choices=["orchestrator", "worker", "reviewer"], default=None,
                           help="Session role (default: worker — the common dispatched-task case, "
                                "safety-railed: isolation/verify/draft-PR/notify, non-overridable). "
                                "'orchestrator' makes this worktree a durable, replaceable-persona "
                                "project window instead of a subordinate, and — unless --created-by "
                                "says otherwise — roots it (no parent), since a durable orchestrator "
                                "shouldn't answer to whoever happened to spawn it. See also the "
                                "`orchestrator` sugar verb. 'reviewer' is a PR-review station "
                                "(safety-railed the other way: never opens/merges its own PR) for "
                                "anyone who needs a local checkout to e2e a sibling's branch — stays "
                                "parented like worker, not rooted.")
    _add_worktree_flags(wt_parser)
    wt_parser.set_defaults(func=cmd_worktree)

    # === orchestrator command (sugar: `worktree --kind orchestrator`) ===
    orch_parser = subparsers.add_parser(
        "orchestrator",
        help="Stand up a durable, root orchestrator session on its own worktree "
             "(sugar for `worktree --kind orchestrator`)",
    )
    orch_parser.add_argument("name", nargs="?", default="orchestrator",
                             help="Name (becomes branch name in default mode, session suffix always); "
                                  "default: 'orchestrator'")
    _add_worktree_flags(orch_parser)
    orch_parser.set_defaults(func=cmd_orchestrator)
