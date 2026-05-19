"""Missions config loader.

Two sources, merged with per-repo override winning over the matching global entry:

1. ``~/.agentwire/missions.yaml`` — global defaults + repo registry
2. ``.agentwire.yml`` ``missions:`` block — per-repo override of select keys

Repos are keyed by short name (e.g. ``agentwire-dev``); ``name`` holds the full
``owner/repo`` form used by ``gh`` CLI commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GLOBAL_CONFIG_PATH = Path.home() / ".agentwire" / "missions.yaml"


@dataclass
class RepoConfig:
    """A configured target repo for missions."""

    short: str                # local key, e.g. "agentwire-dev"
    name: str                 # GitHub full name, e.g. "dotdevdotdev/agentwire-dev"
    projects_dir: Path        # parent dir; worktrees go in {projects_dir}/{short}-worktrees/
    per_repo_concurrency: int = 1


@dataclass
class MissionsConfig:
    """Top-level missions configuration."""

    repos: dict[str, RepoConfig] = field(default_factory=dict)
    global_concurrency: int = 3
    work_hours_start: int = 9
    work_hours_end: int = 18
    default_max_iterations: int = 3

    def get_repo(self, short: str) -> RepoConfig | None:
        return self.repos.get(short)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(
    global_path: Path = GLOBAL_CONFIG_PATH,
    project_config_path: Path | None = None,
) -> MissionsConfig:
    """Load missions config.

    Per-repo override only updates fields under a matched repo entry; the
    repo must already exist in the global registry. Unknown repos in the
    project override are silently ignored.
    """
    global_data = _load_yaml(global_path)
    cfg = MissionsConfig(
        global_concurrency=int(global_data.get("global_concurrency", 3)),
        work_hours_start=int(global_data.get("work_hours_start", 9)),
        work_hours_end=int(global_data.get("work_hours_end", 18)),
        default_max_iterations=int(global_data.get("default_max_iterations", 3)),
    )
    for short, raw in (global_data.get("repos") or {}).items():
        cfg.repos[short] = RepoConfig(
            short=short,
            name=raw["name"],
            projects_dir=Path(raw["projects_dir"]).expanduser(),
            per_repo_concurrency=int(raw.get("per_repo_concurrency", 1)),
        )

    if project_config_path is not None:
        project_data = _load_yaml(project_config_path)
        override = project_data.get("missions") or {}
        repo_short = override.get("repo")
        if repo_short and repo_short in cfg.repos:
            if "per_repo_concurrency" in override:
                cfg.repos[repo_short].per_repo_concurrency = int(
                    override["per_repo_concurrency"]
                )

    return cfg
