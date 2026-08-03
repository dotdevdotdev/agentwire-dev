"""``agentwire helper`` — a worker session with NO isolation (#838).

A worker pane's one genuine advantage over a worktree session is that it
shares the orchestrator's checkout: no ``git worktree add``, no branch, no
directory, instant. Everything else about a pane is a regression — no msg
inbox (#834), no voice, a headless exit-summary instead of report-back.

This verb reproduces that one advantage on a real *session*. The cwd IS the
caller's own working tree; creation performs zero git operations. Everything
else — msg inbox, voice, prompt routing, cohort enrollment, portal
visibility — comes free from being an ordinary session.

**Not a creation path.** :func:`cmd_helper` creates nothing. It resolves a
directory, derives a session name, and delegates to ``session_cli.cmd_new``
— the one session-creation path — exactly as ``cmd_orchestrator`` delegates
to ``cmd_worktree``.

Two things make sharing a checkout safe:

1. **The guard is declared past, not disarmed.** ``session_cli.cmd_new``'s
   shared-working-dir guard refuses to put a second agent in a tree that
   already has one, because two agents in one working tree is a real footgun
   (see the ``shared-checkout`` role for the concrete list). ``helper`` is
   *literally* the sentence "put a second agent in this tree", so it passes
   ``allow_shared_dir=True`` — the same posture as ``services.py``
   ("registering a service is explicit intent") and #857's dispatch
   derivation. Deliberately NOT ``--force``, which would additionally
   kill-replace a live same-name session. The guard stays fully armed for
   interactive ``agentwire new``.
2. **The ``shared-checkout`` role states the constraint.** Read and edit
   freely — editing the shared tree is the entire point — but never mutate
   git state (checkout/commit/stash/reset/branch/pull/rebase), because that
   rewrites the tree under the co-resident agent or sweeps its in-flight
   edits into your commit.

Nothing is written to the worktree registry: this session creates no
directory and no branch, so there is no git resource for
``--list``/``--prune``/``--remove``/``--dangling`` to track. ``agentwire
kill`` reclaims all of it.
"""

import argparse
import os
from pathlib import Path

from .core import _output_result
from .worktree import git_root, worktree_session_name

#: Stacked after the non-overridable ``worker`` rail (recency weight), so its
#: "never mutate git state" corrects ``worker.md``'s "commit your work" —
#: which is right for a pane or a worktree and wrong in a shared checkout.
SHARED_CHECKOUT_ROLE = "shared-checkout"


def _resolve_checkout(project_arg: str | None) -> Path:
    """The working tree the helper will share.

    ``--project`` (an explicit path, or a bare project name resolved against
    the configured projects dir) else cwd — then normalized to that
    directory's git top-level. Run from inside a *linked* worktree,
    :func:`worktree.git_root` returns that worktree's own root, which is
    right: a worktree worker spawning a helper wants it sharing ITS files,
    not the main checkout's. A non-git directory is fine and is left as-is —
    a pane never required a repo either.
    """
    from .config import load_config as load_config_typed
    from .session_cli import _resolve_project_arg

    if project_arg:
        base = _resolve_project_arg(project_arg, load_config_typed().projects.dir)
    else:
        base = Path(os.getcwd())
    return git_root(base) or base


def cmd_helper(args) -> int:
    """Create a worker session that shares the caller's checkout.

    Role is fixed to ``worker`` — there is no ``--kind``. A helper is
    definitionally a subordinate, and an orchestrator persona co-resident in
    someone else's working tree is the exact footgun the shared-dir guard
    exists for.
    """
    json_mode = getattr(args, "json", False)

    name = getattr(args, "name", None)
    if not name:
        return _output_result(False, json_mode, "Usage: agentwire helper <name> [-p <project>]")

    checkout = _resolve_checkout(getattr(args, "project", None))
    if not checkout.is_dir():
        return _output_result(False, json_mode, f"Not a directory: {checkout}")

    # User roles STACK on the safety rail; shared-checkout rides directly
    # behind the intrinsic `worker` etiquette it amends.
    user_roles = [r.strip() for r in (getattr(args, "roles", None) or "").split(",") if r.strip()]
    roles = ",".join([SHARED_CHECKOUT_ROLE, *(r for r in user_roles if r != SHARED_CHECKOUT_ROLE)])

    from .session_cli import cmd_new

    return cmd_new(argparse.Namespace(
        session=worktree_session_name(checkout, name),
        path=str(checkout),
        json=json_mode,
        # `worker` + worktree_topology=False resolves to the `worker`
        # etiquette (already written for "a standalone session on the same
        # checkout"), and enrolls in the caller's cohort as topology "main"
        # so `wait --children` collects the report and THEN kills it — the
        # pane lifecycle, on a real session.
        kind="worker",
        worktree_topology=False,
        # Declared intent, not an accident — see the module docstring.
        allow_shared_dir=True,
        force=False,
        roles=roles,
        posture=getattr(args, "posture", None),
        model=getattr(args, "model", None),
        first_message=getattr(args, "prompt", None),
        env=getattr(args, "env", None),
        created_by=getattr(args, "created_by", None),
        caller_session=getattr(args, "caller_session", None),
        no_cohort=getattr(args, "no_cohort", False),
        no_soul=getattr(args, "no_soul", False),
        # Branchless by construction: cmd_new errors loudly if these are set.
        base=None,
        pull_first=None,
        bare=False,
        prompted=False,
        instructions=None,
        persist=False,
    ))


def register_helper_parser(subparsers) -> None:
    """Register ``agentwire helper``."""
    p = subparsers.add_parser(
        "helper",
        help="Worker session sharing the current checkout — no worktree, no branch (#838)",
    )
    p.add_argument("name", nargs="?", help="Helper name (session becomes {project}-{name})")
    p.add_argument("--project", "-p", help="Repo/dir to share (default: git root of cwd)")
    p.add_argument("--prompt", help="First message to deliver once the session is ready")
    p.add_argument("--roles", help="Extra roles (comma-separated) — STACK on the worker rail")
    p.add_argument("--posture", help="Permission posture (default: bypass)")
    p.add_argument("--model", help="Model override")
    p.add_argument("--env", action="append", help="Env var KEY=VALUE (repeatable)")
    p.add_argument("--created-by", help="Force the parent session ('' for a standalone root)")
    p.add_argument("--caller-session", help="Override the detected calling session")
    p.add_argument("--no-cohort", action="store_true", help="Skip fan-out cohort enrollment")
    p.add_argument("--no-soul", action="store_true", help="Skip the soul personality role")
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.set_defaults(func=cmd_helper)
