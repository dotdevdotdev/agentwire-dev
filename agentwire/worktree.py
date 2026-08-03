"""Git worktree-based session management for parallel development.

Session naming convention:
- "project" -> single session in ~/projects/project/
- "project/branch" -> worktree session in ~/projects/project-worktrees/branch/
- "project@machine" -> remote session on machine
- "project/branch@machine" -> remote worktree session
"""

import getpass
import re
import subprocess
from pathlib import Path


def git_root(path: Path) -> Path | None:
    """Return the top-level git directory containing ``path``, or None.

    Walks up via ``git rev-parse --show-toplevel`` so a worktree session can
    be spawned from any subdirectory of a (mono)repo and still target the
    repo root. Note: run from inside a linked worktree, this returns that
    worktree's own top-level path, not the main checkout's — use
    ``git_common_dir`` when you need an identity that's shared across all of
    a repo's worktrees.
    """
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    out = result.stdout.strip()
    if result.returncode == 0 and out:
        return Path(out)
    return None


def git_common_dir(path: Path) -> Path | None:
    """Return the shared ``.git`` dir for ``path``'s repo, or None outside a repo.

    Identical across all of a repo's linked worktrees (unlike ``git_root``,
    which returns each worktree's own top-level path) — the robust "same
    repo" signal for comparing two paths that may each be a different linked
    worktree of one logical project (#715).
    """
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True,
    )
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        return None
    common_dir = Path(out)
    if not common_dir.is_absolute():
        common_dir = path / common_dir
    return common_dir.resolve()


def default_base_branch(project_path: Path) -> str:
    """Resolve a repo's default base branch (no hardcoded 'main').

    Order:
        1. ``origin/HEAD`` symbolic ref (the remote's default branch) —
           e.g. a monorepo defaulting to ``develop``.
        2. The repo's current branch (when origin/HEAD isn't set locally;
           run ``git remote set-head origin -a`` to populate it).
        3. ``"main"`` as a last resort.
    """
    result = subprocess.run(
        ["git", "-C", str(project_path), "symbolic-ref", "--quiet",
         "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    ref = result.stdout.strip()
    if result.returncode == 0 and ref:
        return ref.rsplit("/", 1)[-1]

    result = subprocess.run(
        ["git", "-C", str(project_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    cur = result.stdout.strip()
    if result.returncode == 0 and cur and cur != "HEAD":
        return cur

    return "main"


def is_valid_branch_name(name: str, project_path: Path | None = None) -> bool:
    """True if ``name`` is a valid git branch name.

    Uses ``git check-ref-format --branch`` (the authority) plus cheap
    guards for cases git would mis-parse (leading dash → looks like a flag)
    or that aren't usable as a worktree branch. Guards against a templated
    or verbatim name with spaces / ``..`` / leading ``-`` reaching
    ``git checkout -b`` and failing *after* the worktree is already on disk.
    """
    if not name or name.startswith("-") or name.endswith("/") or name.endswith(".lock"):
        return False
    cwd = str(project_path) if project_path else None
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        capture_output=True, text=True, cwd=cwd,
    )
    return result.returncode == 0


def slugify(name: str) -> str:
    """Lowercase, hyphen-separated, filesystem/branch-safe slug of ``name``."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "wt"


class _SafeFormatDict(dict):
    """format_map helper: leave unknown ``{placeholders}`` literal."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def apply_naming(template: str | None, name: str) -> str:
    """Apply a branch-naming template to a CLI name.

    Placeholders: ``{name}`` (verbatim), ``{slug}`` (slugified),
    ``{user}`` (OS login). ``None``/empty template → ``name`` verbatim.
    Unknown placeholders are left literal rather than raising.
    """
    if not template:
        return name
    return template.format_map(_SafeFormatDict(
        name=name, slug=slugify(name), user=getpass.getuser(),
    ))


def parse_session_name(name: str) -> tuple[str, str | None, str | None]:
    """Parse session name into (project, branch, machine).

    Examples:
        "myapp" -> ("myapp", None, None)
        "myapp/feature" -> ("myapp", "feature", None)
        "myapp@server" -> ("myapp", None, "server")
        "myapp/feature@server" -> ("myapp", "feature", "server")
    """
    machine: str | None = None
    branch: str | None = None

    # Extract machine if present
    if "@" in name:
        name, machine = name.rsplit("@", 1)

    # Extract branch if present
    if "/" in name:
        project, branch = name.split("/", 1)
    else:
        project = name

    return project, branch, machine


def is_git_repo(path: Path) -> bool:
    """Check if path contains a .git directory."""
    return (path / ".git").exists()


def worktree_session_name(project_path: Path, name: str) -> str:
    """tmux session name for a child session on ``project_path``.

    The flat ``{project}-{safe_name}`` convention every child-session verb
    shares — deliberately NOT ``project/name``, which
    :func:`parse_session_name` would read as a branch (and which ``cmd_new``
    would then try to build a worktree for).

    One convention, one implementation: ``agentwire helper`` (#838) calls
    this, and ``session_cli.cmd_worktree`` inlines a byte-identical copy that
    can collapse to this call whenever that file is free to edit.
    """
    safe_name = re.sub(r"[\s/:.]+", "-", name).strip("-") or "wt"
    return f"{Path(project_path).name}-{safe_name}"


def ensure_worktree(
    project_path: Path,
    branch: str,
    worktree_path: Path,
    auto_create_branch: bool = True,
    commit: str | None = None,
    copy_files: list[str] | None = None,
) -> bool:
    """Ensure a git worktree exists for the given branch.

    Args:
        project_path: Path to the main git repository
        branch: Branch name for the worktree
        worktree_path: Path where the worktree should be created
        auto_create_branch: If True, create branch if it doesn't exist
        commit: Optional commit/ref to start the worktree from (default: HEAD)
        copy_files: Gitignored files to seed into the fresh worktree. None
            resolves the configured default (projects.worktrees.copy_files).

    Returns:
        True if worktree exists or was created successfully, False otherwise
    """
    # Already exists
    if worktree_path.exists():
        return True

    # Must be a git repo
    if not is_git_repo(project_path):
        return False

    # Ensure parent directory exists
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if branch exists
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=project_path,
        capture_output=True,
    )
    branch_exists = result.returncode == 0

    if branch_exists:
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch],
            cwd=project_path, capture_output=True,
        )
        if result.returncode != 0:
            return False
        if commit:
            # Detach HEAD at requested commit inside the worktree
            checkout = subprocess.run(
                ["git", "checkout", commit],
                cwd=worktree_path, capture_output=True,
            )
            if checkout.returncode != 0:
                return False
    elif auto_create_branch:
        cmd = ["git", "worktree", "add", "-b", branch, str(worktree_path)]
        if commit:
            cmd.append(commit)  # git worktree add -b branch path <commit> is native
        result = subprocess.run(cmd, cwd=project_path, capture_output=True)
        if result.returncode != 0:
            return False
    else:
        return False

    _seed_worktree_files(project_path, worktree_path, copy_files)
    return True


def _seed_worktree_files(
    project_path: Path,
    worktree_path: Path,
    copy_files: list[str] | None = None,
) -> None:
    """Copy gitignored-but-needed files (e.g. .env) into a fresh worktree.

    `git worktree add` only checks out tracked files — untracked/ignored
    files like .env, .env.local, or local config never come along, so an
    agent working in the worktree can't authenticate. Copy a configured
    seed list (relative paths) from the main repo, always including
    :data:`config.DEFAULT_WORKTREE_COPY_FILES` regardless of what the
    resolved `copy_files` says — a `config.yaml` written before
    `.agentwire.tasks.yml` split out of `.agentwire.yml` (#720) can carry a
    stale, narrower override (e.g. just `[.env, .agentwire.yml]`) that
    silently drops the newer file, and every worktree-dispatched scheduled
    task then fails with "No .agentwire.tasks.yml found" (#803).
    `copy_files` can only extend this mandatory set, never shrink it.
    Best-effort: missing sources are skipped and copy errors are swallowed.
    Files that are gitignored in the repo stay ignored in the worktree, so
    they're never committed.
    """
    from .config import DEFAULT_WORKTREE_COPY_FILES

    if copy_files is None:
        try:
            from .config import load_config
            copy_files = load_config().projects.worktrees.copy_files
        except Exception:
            copy_files = []

    seed_files = list(dict.fromkeys([*DEFAULT_WORKTREE_COPY_FILES, *(copy_files or [])]))

    import shutil

    for rel in seed_files:
        src = project_path / rel
        dst = worktree_path / rel
        if not src.exists() or dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        except Exception:
            pass  # best-effort — a missing seed file shouldn't fail dispatch


def remove_worktree(project_path: Path, worktree_path: Path, *, force: bool = True) -> tuple[bool, str]:
    """Remove a git worktree.

    Defaults to ``--force`` — teardown callers want this to succeed even with
    uncommitted changes (the session is being torn down, not preserved), and
    a non-force attempt just adds a doomed round-trip before the inevitable
    force retry (#717).

    Args:
        project_path: Path to the main git repository
        worktree_path: Path to the worktree to remove
        force: Pass ``--force`` to ``git worktree remove`` (default True)

    Returns:
        (removed, error) — error is "" on success, else git's stderr/reason.
    """
    if not is_git_repo(project_path):
        return False, f"{project_path} is not a git repository"

    cmd = ["git", "worktree", "remove", str(worktree_path)]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, cwd=project_path, capture_output=True, text=True)

    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout).strip()


def _worktree_porcelain(project_path: Path) -> tuple[bool, str]:
    """Raw ``git worktree list --porcelain`` output + whether git succeeded.

    Split out so the two readers can keep their *different* failure
    postures over one shared call: :func:`list_git_worktrees` reports
    "nothing known" on error, while :func:`is_registered_worktree` fails
    closed to "assume registered".
    """
    result = subprocess.run(
        ["git", "-C", str(project_path), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    return result.returncode == 0, result.stdout


def list_git_worktrees(project_path: Path) -> list[dict]:
    """Every worktree git itself knows about for ``project_path``'s repo.

    The single source of truth for "where does this worktree actually live"
    (#855). Path conventions are a *default*, not a guarantee — two layouts
    are live on real machines (``~/worktrees/<project>/<name>/`` and
    ``~/projects/<project>-worktrees/<name>/``), and a hand-created worktree
    can sit anywhere. String-building a path from a convention and acting on
    it is how a teardown reports success while removing nothing; asking git
    is how it doesn't.

    Returns one dict per worktree, in git's own order — **the main checkout
    is always first** (see :func:`linked_git_worktrees` to drop it):

        path:     Path — absolute worktree directory
        branch:   short branch name, or None when detached/bare
        head:     commit sha, or None for a bare repo
        detached: bool
        bare:     bool
        locked:   bool

    Empty list when ``project_path`` isn't a repo or git errors — callers
    that mutate must treat "empty" as "don't know", never as "nothing there".
    """
    ok, text = _worktree_porcelain(project_path)
    if not ok:
        return []

    out: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        key, _, val = line.partition(" ")
        if key == "worktree":
            if cur is not None:
                out.append(cur)
            cur = {"path": Path(val), "branch": None, "head": None,
                   "detached": False, "bare": False, "locked": False}
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        elif key == "detached":
            cur["detached"] = True
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = True
    if cur is not None:
        out.append(cur)
    return out


def linked_git_worktrees(project_path: Path) -> list[dict]:
    """:func:`list_git_worktrees` minus the main checkout and any bare entry.

    Resolution and teardown must never be able to select the repo's own
    working copy — `git worktree remove` refuses it, but a caller that also
    kills a session or deletes a branch off the "resolved" entry would be
    acting on the main checkout. Excluding it here makes that unreachable
    rather than merely unlikely.
    """
    return [e for e in list_git_worktrees(project_path)[1:] if not e["bare"]]


def main_worktree(project_path: Path) -> Path:
    """The repo's MAIN checkout, per git — ``project_path`` itself as fallback.

    The registry is keyed by repo, so every entry for one logical project must
    land in one file. A caller can legitimately hand us a *linked worktree* as
    its "project path" (``agentwire fork`` forks from a worktree source), and
    keying off that would silently shard the registry per worktree — entries
    written under one key and looked up under another.
    """
    entries = list_git_worktrees(project_path)
    return entries[0]["path"] if entries else Path(project_path)


def find_git_worktree(
    project_path: Path,
    *,
    path: Path | str | None = None,
    branch: str | None = None,
    name: str | None = None,
) -> dict | None:
    """Ask git for the worktree matching ``path``, ``branch``, or ``name``.

    Tried in that order of authority: an exact (resolved) path match beats a
    branch match beats a directory-basename match. Never returns the main
    checkout (see :func:`linked_git_worktrees`). Returns None when git knows
    of no such worktree — which callers must treat as "not found", never as
    "assume the conventional path".
    """
    entries = linked_git_worktrees(project_path)
    if not entries:
        return None

    if path is not None:
        target = str(Path(path).expanduser().resolve())
        for e in entries:
            if str(e["path"].resolve()) == target:
                return e

    if branch:
        for e in entries:
            if e["branch"] == branch:
                return e

    if name:
        for e in entries:
            if e["path"].name == name:
                return e

    return None


def register_worktree(
    project_path: Path,
    *,
    branch: str | None,
    session: str,
    base: str | None,
    worktree_path: Path,
    kind: str | None = None,
    topology: str = "worktree",
) -> dict:
    """Record a worktree in the local registry at the path **git** reports.

    The one registration entry point (#837) — every creation site routes
    through here (usually via :func:`create_and_register_worktree`) so a
    worktree can't exist on disk while being invisible to
    ``agentwire worktree --list`` / ``--dangling`` / ``--prune`` / ``--remove``.

    The recorded path is git's own (symlinks resolved, ``/private/var`` vs
    ``/var`` normalized) whenever git knows the worktree, so later lookups
    compare like with like instead of re-deriving a convention. The registry
    file is keyed by the repo's MAIN checkout (see :func:`main_worktree`), so
    passing a linked worktree as ``project_path`` still writes where lookups
    will read.
    """
    from . import worktree_registry

    found = find_git_worktree(project_path, path=worktree_path, branch=branch)
    actual = found["path"] if found else Path(worktree_path)
    return worktree_registry.register(
        main_worktree(project_path),
        branch=branch,
        session=session,
        base=base,
        worktree_path=actual,
        kind=kind,
        topology=topology,
    )


def create_and_register_worktree(
    project_path: Path,
    *,
    branch: str,
    worktree_path: Path,
    session: str,
    base: str | None = None,
    kind: str | None = None,
    topology: str = "worktree",
    auto_create_branch: bool = True,
    commit: str | None = None,
    copy_files: list[str] | None = None,
) -> tuple[bool, str]:
    """Create a worktree **and** register it — the SSOT creation path (#837).

    Wraps :func:`ensure_worktree` (idempotent: an existing worktree is
    adopted, not recreated) and always registers the result, so re-running a
    creation heals a missing registry entry instead of leaving an orphan.

    A pre-existing directory that git does **not** know as a worktree is a
    hard failure, not something to register: launching an agent into a plain
    directory that merely sits at the worktree path is the silent-corruption
    case this guards.

    Returns ``(ok, error)`` — ``error`` is "" on success.
    """
    if not ensure_worktree(
        project_path, branch, worktree_path,
        auto_create_branch=auto_create_branch, commit=commit, copy_files=copy_files,
    ):
        return False, f"Failed to create worktree for branch '{branch}' in {project_path}"

    if not is_registered_worktree(project_path, worktree_path):
        return False, f"Path exists but is not a git worktree: {worktree_path}"

    register_worktree(
        project_path, branch=branch, session=session, base=base,
        worktree_path=worktree_path, kind=kind, topology=topology,
    )
    return True, ""


def is_registered_worktree(project_path: Path, worktree_path: Path) -> bool:
    """Does git's own worktree registry (still) know about ``worktree_path``?

    A directory can outlive its registration — e.g. `git worktree remove`
    succeeded on a prior teardown attempt but the `rm -rf` half crashed
    before clearing a build-tool cache dir left inside, or the admin file
    under `.git/worktrees/` was pruned independently. In that state git
    reports "fatal: ... is not a working tree" for every future removal
    attempt, forever — this lets a caller distinguish that from a real
    registered worktree that `remove --force` merely failed to clear. It
    says nothing about whether the directory holds valuable content — that
    judgment (e.g. "orphaned + unregistered is safe to hard-delete") belongs
    to the caller, not this function.

    Fails closed on an inconclusive read: if `git worktree list` itself
    errors (corrupt repo, I/O, lock contention), that's reported as
    registered rather than not — a caller gating a destructive action on
    this should default to "assume real" when unsure, not "assume orphan".
    """
    ok, text = _worktree_porcelain(project_path)
    if not ok:
        return True
    target = str(Path(worktree_path).resolve())
    for line in text.splitlines():
        if line.startswith("worktree "):
            if str(Path(line[len("worktree "):]).resolve()) == target:
                return True
    return False


def worktree_status(worktree_path: Path) -> dict:
    """Read-only git status for a worktree. Local git only — no network, no gh.

    Reports working-tree cleanliness and ahead/behind vs the upstream as it's
    known locally (reflects the last fetch — never reaches out to the remote).
    This is a pure read: it runs no `git add`/`commit`/`push`, by design.

    Returns dict:
        exists:    bool — the worktree path is present on disk
        branch:    current branch name, or None if detached
        dirty:     bool — any staged/unstaged/untracked changes
        staged/unstaged/untracked: int counts
        upstream:  upstream ref (e.g. "origin/fix-bug"), or None if unset
        ahead:     commits on HEAD not on upstream
        behind:    commits on upstream not on HEAD
        pushed:    bool — upstream exists and ahead == 0 (work is on the remote)
    """
    wt = Path(worktree_path)
    status = {
        "exists": False, "branch": None, "dirty": False,
        "staged": 0, "unstaged": 0, "untracked": 0,
        "upstream": None, "ahead": 0, "behind": 0, "pushed": False,
    }
    if not wt.exists():
        return status
    status["exists"] = True

    def _git(*a):
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)

    # Current branch (None when detached, e.g. --ref worktrees).
    r = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = r.stdout.strip() if r.returncode == 0 else ""
    status["branch"] = None if branch in ("", "HEAD") else branch

    # Working-tree state via porcelain. Column X = index/staged, Y = worktree.
    r = _git("status", "--porcelain")
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if not line:
                continue
            xy = line[:2]
            if xy == "??":
                status["untracked"] += 1
                continue
            if xy[0] not in (" ", "?"):
                status["staged"] += 1
            if xy[1] not in (" ", "?"):
                status["unstaged"] += 1
        status["dirty"] = bool(status["staged"] or status["unstaged"] or status["untracked"])

    # Upstream + ahead/behind, using the locally-stored remote-tracking ref.
    up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if up.returncode == 0 and up.stdout.strip():
        status["upstream"] = up.stdout.strip()
        # --left-right --count "@{upstream}...HEAD" → "<behind>\t<ahead>"
        cnt = _git("rev-list", "--left-right", "--count", "@{upstream}...HEAD")
        if cnt.returncode == 0 and cnt.stdout.split():
            parts = cnt.stdout.split()
            if len(parts) == 2:
                status["behind"], status["ahead"] = int(parts[0]), int(parts[1])
        status["pushed"] = status["ahead"] == 0

    return status


def get_project_type(path: Path) -> str:
    """Determine project type based on git status.

    Returns:
        "full" if path is a git repository, "scratch" otherwise
    """
    if is_git_repo(path):
        return "full"
    return "scratch"
