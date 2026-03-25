"""Git worktree-based session management for parallel development.

Session naming convention:
- "project" -> single session in ~/projects/project/
- "project/branch" -> worktree session in ~/projects/project-worktrees/branch/
- "project@machine" -> remote session on machine
- "project/branch@machine" -> remote worktree session
"""

import subprocess
from pathlib import Path


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


def get_session_path(
    name: str,
    projects_dir: Path,
    worktree_suffix: str = "-worktrees",
) -> Path:
    """Get filesystem path for a session.

    For "project" -> projects_dir / project
    For "project/branch" -> projects_dir / f"{project}{worktree_suffix}" / branch
    """
    project, branch, _ = parse_session_name(name)

    if branch:
        return projects_dir / f"{project}{worktree_suffix}" / branch
    return projects_dir / project


def ensure_worktree(
    project_path: Path,
    branch: str,
    worktree_path: Path,
    auto_create_branch: bool = True,
    commit: str | None = None,
) -> bool:
    """Ensure a git worktree exists for the given branch.

    Args:
        project_path: Path to the main git repository
        branch: Branch name for the worktree
        worktree_path: Path where the worktree should be created
        auto_create_branch: If True, create branch if it doesn't exist
        commit: Optional commit/ref to start the worktree from (default: HEAD)

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

    # Build git worktree add command
    # git worktree add [-b branch] <path> [<commit-ish>]
    cmd = ["git", "worktree", "add", str(worktree_path)]

    if branch_exists:
        cmd.append(branch)
        # Create worktree first, then checkout specific commit if requested
        result = subprocess.run(cmd, cwd=project_path, capture_output=True)
        if result.returncode != 0:
            return False
        if commit:
            # Detach HEAD at requested commit inside the worktree
            checkout = subprocess.run(
                ["git", "checkout", commit],
                cwd=worktree_path,
                capture_output=True,
            )
            return checkout.returncode == 0
        return True
    elif auto_create_branch:
        cmd.extend(["-b", branch])
        if commit:
            cmd.append(commit)  # git worktree add -b branch path <commit> is native
    else:
        return False

    # Create worktree
    result = subprocess.run(
        cmd,
        cwd=project_path,
        capture_output=True,
    )

    return result.returncode == 0


def list_worktrees(project_path: Path) -> list[dict]:
    """List all worktrees for a git repository.

    Returns:
        List of dicts with keys: path, branch, head
    """
    if not is_git_repo(project_path):
        return []

    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return []

    worktrees: list[dict] = []
    current: dict = {}

    for line in result.stdout.strip().split("\n"):
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue

        if line.startswith("worktree "):
            current["path"] = line[9:]
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            # refs/heads/branch-name -> branch-name
            ref = line[7:]
            if ref.startswith("refs/heads/"):
                current["branch"] = ref[11:]
            else:
                current["branch"] = ref
        elif line == "detached":
            current["branch"] = None

    # Don't forget the last entry
    if current:
        worktrees.append(current)

    return worktrees


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


def get_project_type(path: Path) -> str:
    """Determine project type based on git status.

    Returns:
        "full" if path is a git repository, "scratch" otherwise
    """
    if is_git_repo(path):
        return "full"
    return "scratch"
