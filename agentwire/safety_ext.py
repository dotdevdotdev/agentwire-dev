"""AgentWire's whole damage-control surface: the extension points of the ``damage_control`` package.

``build_profile()`` is the profile (#1209 M3, section 4.2), as plain data: protected paths and
commands, unattended grants, the notifier argv, MCP command templates, the audit field, the rules
layer. A hook never imports it: ``install()`` writes it to the protected manifest
(``manifest_path()``) and the hook's command line names that file. In-process callers (the CLI,
the portal, the tests) call ``activate()`` to judge by the same profile.

Everything else agentwire does about damage control is a call through the package's own API
(``damage_control.install``, ``.packs``, ``.cli``); ``install()`` here is the only installer.
"""

import json
from pathlib import Path
from typing import Any, Callable, Dict, NamedTuple

from damage_control import engine
from damage_control.profile import AuditSpec, GroupSpec, McpCommandTemplate, Profile, dump_profile

# AgentWire's rule layer (JSON built from the YAML beside it). An absolute path: the hook
# reads it through the manifest, and a manifest is only valid for the install that wrote it.
RULES_DIR = Path(__file__).resolve().parent / "safety" / "rules"


def build_profile() -> Profile:
    return Profile(
        name="agentwire",
        config_dir="~/.agentwire",
        config_dir_env="AGENTWIRE_DIR",
        protected_paths=(
            "~/.agentwire/hooks/damage-control/*.py",  # the hook scripts themselves
            "~/.agentwire/damage-control/*.yaml",      # the damage-control rule files
            "~/.agentwire/damage-control/*.json",      # ...and their JSON form (the hook reads either)
            "~/.agentwire/bin/damage-control-breakglass",  # the human's way out: an agent must not rewrite it
            "~/.agentwire/damagecontrol-install.json",     # the interpreter record doctor re-verifies
            # Execution-plane configs: agentwire runs strings from these files through
            # its OWN ``subprocess.run(..., shell=True)`` calls (scheduler gate commands
            # in scheduler.py, service healthchecks in services.py) — those subprocesses
            # do NOT traverse the Claude Code hook, so a policed agent that can write a
            # gate/healthcheck gets unguarded code execution on the next tick (a
            # confused-deputy escape). Treat them as control plane (#466 lockdown).
            "~/.agentwire/scheduler.yaml",      # scheduler gate commands run via shell
            "~/.agentwire/config.yaml",         # service healthcheck commands run via shell
            # Per-project task-EXECUTION config (#720): split out of ``.agentwire.yml``,
            # which is now purely declarative (type/roles/voice/parent/worktree) and
            # agent-writable again — it carries no exec vector. ``.agentwire.tasks.yml``
            # holds the ``tasks:`` block (pre/post/on_task_end/shell/unattended_allow),
            # which agentwire runs the same way (confused-deputy risk above), so it
            # stays protected. Authored via propose-and-promote: an agent drafts to the
            # UNPROTECTED ``.agentwire.tasks.proposed.yml`` staging file; a human reviews
            # (``agentwire tasks review``) and promotes (``agentwire tasks promote``) —
            # the agent never writes the live file. See ``agentwire/tasks_cli.py``.
            "*.agentwire.tasks.yml",            # project task-execution config (any dir)
        ),
        control_plane_words=(".agentwire",),
        benign_path_patterns=(r'(?:^|/)\.agentwire/(?:wiki|artifacts)(?:/|$)',),
        home_alias="/home/agentwire-canonical",
        # ``agentwire tasks promote`` is the confused-deputy escape itself (#720/#721):
        # the host-trusted program that copies a vetted draft into the protected
        # ``.agentwire.tasks.yml``. If a policed/unattended agent could invoke the CLI
        # itself, the file protection is moot. This is defense in depth ON TOP OF
        # ``cmd_tasks_promote``'s own runtime refusal (unattended check + host-context
        # gate in ``tasks_cli.py``), which is what closes the ``python3 -c "from
        # agentwire.tasks_cli import cmd_tasks_promote; ..."`` bypass.
        protected_commands=(
            (
                r'\bagentwire\s+tasks\s+promote\b',
                "agentwire tasks promote writes the protected .agentwire.tasks.yml — "
                "host-only, run it from your own terminal (not through an agent).",
            ),
            (
                r'\bagentwire\s+safety\s+(?:packs|groups)\s+(?:enable|disable)\b',
                "agentwire safety packs/groups enable|disable changes damage-control "
                "policy — host-only, run it from your own terminal (not through an agent).",
            ),
            (
                r'\bagentwire\s+safety\s+migrate\b(?!.*--dry-run)',
                "agentwire safety migrate rewrites damage-control registrations and rule files — "
                "host-only, run it from your own terminal (not through an agent).",
            ),
        ),
        unattended_env="AGENTWIRE_UNATTENDED",
        unattended_allow_env="AGENTWIRE_UNATTENDED_ALLOW",
        unattended_grant_hint=(
            "To permit this for an unattended task, add rule id '{rule_id}' "
            "to its .agentwire.tasks.yml task `unattended_allow`"
        ),
        unattended_block_notifier=("agentwire", "safety", "notify-unattended-block"),
        # The built-in grants. Written in the SAME shape as a task's ``unattended_allow``
        # (bare id = unscoped, or ``{id, paths}`` = scoped) and parsed by the same parser
        # (#914). Deliberately UNSCOPED — narrowing them is a host policy decision, and a
        # task can already express a narrower grant (naming a rule at a more specific
        # level REPLACES this one). These ids only exist if the LOADED tooldefs carry
        # them; ``agentwire doctor`` reports a drifted install (``_check_unattended_defaults``).
        default_unattended_allow=(
            "git.add",       # stage files
            "git.add-u",     # stage tracked changes
            "git.commit",    # commit
            "git.push",      # push (force/delete are SEPARATE hard-block/ask rules)
            "gh.pr-create",  # open a PR — the human-reviewed gate
            "outbound.agentwire-email",  # blanket allow, any recipient (#804)
            # Run the project's own tooling — tests, linters, the project CLI (#925).
            #
            # This tier is `ask` because `uv run` executes code, which is true of every
            # scheduled task by definition: the dispatch already granted "run this
            # agent on this repo". Withholding the project's OWN test runner from it
            # does not withhold code execution, it only withholds VERIFICATION — the
            # task still writes the change and still opens the PR, just without having
            # been able to run the suite over it. 18 of the 111 unattended blocks
            # measured over the 14 days to 2026-08-06 were exactly this.
            #
            # Safe to allow only because the id it grants cannot shadow a hard block.
            # Verified, not assumed: rules are evaluated in order and the FIRST match
            # returns its id, so an earlier `ask` rule CAN hide a later `block` — the
            # matrix in tests/unit/test_unattended_allow.py pins that every dangerous
            # form behind `uv run` still comes back with the DESTRUCTIVE rule's id, not
            # this one (the launcher-payload rescan that guarantees it landed in #1028).
            #
            # BOTH ids, deliberately. `uv run <script.py>` and `uv run <cmd>` are two
            # tooldef lines that compile to the IDENTICAL pattern `\buv\s+run\b`, so
            # which id a command comes back with is decided by which yaml line sits
            # higher, not by anything about the command. Allowlisting only one would be
            # guarding the phrasing: reorder uv.yaml and the permission silently
            # evaporates with nothing failing to say so.
            "tooldef.uv-run-a-script-in-project-environment",
            "tooldef.uv-run-a-command-in-project-environment",
        ),
        audit=AuditSpec(
            session_env="AGENTWIRE_SESSION_ID",
            agent_env="AGENTWIRE_AGENT_ID",
            sessions_dir="sessions",
        ),
        extra_rules_dirs=(str(RULES_DIR),),
        # Outbound MCP tools and the shell command they run under the hood. Keep in
        # lockstep with ``email_send`` / ``quo_send`` in ``agentwire/mcp_server.py`` and
        # the ``outbound.agentwire-*`` rules.
        mcp_command_templates=(
            McpCommandTemplate("email_send", ("agentwire", "email"),
                               (("to", "--to", True), ("subject", "--subject", False))),
            McpCommandTemplate("quo_send", ("agentwire", "quo"), (("to", "--to", False),)),
        ),
        groups=(
            GroupSpec("agentwire", "group", "AgentWire state and session destruction",
                      layer_rules=("agentwire.yaml",)),
        ),
        group_layer_rules=(
            ("control-plane", ("agentwire-control-plane.yaml",)),
            ("core", ("agentwire-core.yaml",)),
            ("mcp-tools", ("agentwire-mcp-tools.yaml",)),
            ("outbound", ("agentwire-outbound.yaml",)),
        ),
        # ``tmux`` was part of the always-on ``agentwire`` group before it was carved into
        # a generic opt-in pack (#1209 M2); AgentWire is a tmux tool, so it keeps the two
        # rules by default.
        default_packs=("tmux",),
    )


def manifest_path() -> Path:
    """Where the installed hooks find this profile: beside the policy file, protected like it."""
    return engine.host_path(build_profile().config_dir) / engine.PROFILE_FILE_NAME


def write_manifest() -> Path:
    """Write the profile the hooks load. Atomic: a hook racing the write sees the old file or the new."""
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_profile(build_profile()))
    tmp.replace(path)
    return path


def activate() -> None:
    """Judge by AgentWire's profile in this process."""
    engine.install_profile(build_profile())


class InstallOutcome(NamedTuple):
    code: int
    changed: bool
    refused: bool


def install(
    force: bool = False,
    allow_foreign: bool = False,
    dry_run: bool = False,
    refresh: bool = False,
    fail_open: bool = False,
    log: Callable[[str], None] = print,
) -> InstallOutcome:
    """Register the package hooks with AgentWire's profile: provenance, manifest, then the package's ``init``.

    **Provenance (#936, both packages).** Machine-global files are written only from the
    canonically installed tool. Either package running from a worktree or a foreign checkout
    refuses outright: a worktree's venv is deleted with the worktree, and ``onFailure: block``
    would then refuse every tool call.

    The package's ``init`` proves the registered command runs (self-test) before it switches
    anything, writes the settings file in one atomic edit, and records the install; a refusal
    there leaves the existing registration alone (#1219/#1220).
    """
    from damage_control import install as dc_install
    from damage_control.harness_config import ConfigError

    from agentwire.safety import provenance as prov

    refusing = prov.refusing_provenance()
    if refusing and not allow_foreign:
        for package, _, canonical, running in refusing:
            for line in prov.refusal_lines(canonical, running, f"damage-control hooks ({package})"):
                log(line)
        return InstallOutcome(1, False, True)

    # A bootstrap (fresh machine, no canonical install anywhere) is a legitimate source; the
    # package's own dev-source guard exists for installers that have no provenance of their own.
    all_bootstrap = all(row[1] == prov.BOOTSTRAP for row in prov.all_provenance())
    settings = Path.home() / ".claude" / "settings.json"
    before = settings.read_bytes() if settings.exists() else None
    manifest = write_manifest()
    try:
        code = dc_install.init(
            profile=str(manifest), force=force, dry_run=dry_run, refresh=refresh, fail_open=fail_open,
            allow_dev_source=allow_foreign or all_bootstrap, log=log,
        )
    except (dc_install.InstallError, ConfigError) as e:
        log(f"REFUSED: {e}")
        return InstallOutcome(1, False, False)
    after = settings.read_bytes() if settings.exists() else None
    return InstallOutcome(code, before != after, False)


def status(deep: bool = False) -> Dict[str, Any]:
    """The package's ``status --json`` (``doctor --json`` when ``deep``) under AgentWire's profile.

    ``{ok, version, profile, findings}``. Deep adds the self-test and a live Claude Code check.
    """
    import contextlib
    import io

    from damage_control import cli

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(["doctor" if deep else "status", "--json", "--profile", str(manifest_path())])
    if not out.getvalue():
        raise RuntimeError(f"damage-control status unavailable: profile {manifest_path()} is missing or unreadable "
                           "(run `agentwire hooks install`)")
    return json.loads(out.getvalue())
