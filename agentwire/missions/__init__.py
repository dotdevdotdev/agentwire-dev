"""Missions: first-class auto-dispatcher subsystem.

Tick mission-dispatcher → spawn worker session for an eligible GitHub issue.
Tick pr-feedback-router → inject new PR review comments into the worker.
Tick worktree-janitor → tear down sessions + worktrees for merged/closed PRs.

See docs/MISSIONS.md (Phase 8) and issue #195 for the full design.
"""

from agentwire.missions.config import MissionsConfig, RepoConfig, load_config
from agentwire.missions.naming import (
    branch_name,
    is_mission_session,
    parse_mission_session,
    session_name,
    slugify,
    worktree_path,
)

__all__ = [
    "MissionsConfig",
    "RepoConfig",
    "load_config",
    "branch_name",
    "is_mission_session",
    "parse_mission_session",
    "session_name",
    "slugify",
    "worktree_path",
]
