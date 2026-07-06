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
    seed list (relative paths) from the main repo. Best-effort: missing
    sources are skipped and copy errors are swallowed. Files that are
    gitignored in the repo stay ignored in the worktree, so they're never
    committed.
    """
    if copy_files is None:
        try:
            from .config import load_config
            copy_files = load_config().projects.worktrees.copy_files
        except Exception:
            copy_files = []

    import shutil

    for rel in copy_files or []:
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


def remove_worktree(project_path: Path, worktree_path: Path) -> bool:
    """Remove a git worktree.

    Args:
        project_path: Path to the main git repository
        worktree_path: Path to the worktree to remove

    Returns:
        True if removed successfully, False otherwise
    """
    if not is_git_repo(project_path):
        return False

    result = subprocess.run(
        ["git", "worktree", "remove", str(worktree_path)],
        cwd=project_path,
        capture_output=True,
    )

    return result.returncode == 0


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
