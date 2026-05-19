"""Tests for ``agentwire.missions.config`` — yaml loading + override merge."""

from pathlib import Path

import pytest
import yaml

from agentwire.missions.config import MissionsConfig, load_config


@pytest.fixture
def missions_yaml(tmp_path):
    """Write a minimal global ``missions.yaml`` and return its path."""
    p = tmp_path / "missions.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "global_concurrency": 3,
                "work_hours_start": 9,
                "work_hours_end": 18,
                "default_max_iterations": 3,
                "repos": {
                    "agentwire-dev": {
                        "name": "dotdevdotdev/agentwire-dev",
                        "projects_dir": "/Users/dotdev/projects",
                        "per_repo_concurrency": 1,
                    },
                },
            }
        )
    )
    return p


class TestLoadConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(global_path=tmp_path / "nope.yaml")
        assert isinstance(cfg, MissionsConfig)
        assert cfg.global_concurrency == 3
        assert cfg.work_hours_start == 9
        assert cfg.work_hours_end == 18
        assert cfg.default_max_iterations == 3
        assert cfg.repos == {}

    def test_loads_global(self, missions_yaml):
        cfg = load_config(global_path=missions_yaml)
        assert cfg.global_concurrency == 3
        assert "agentwire-dev" in cfg.repos
        repo = cfg.repos["agentwire-dev"]
        assert repo.name == "dotdevdotdev/agentwire-dev"
        assert repo.projects_dir == Path("/Users/dotdev/projects")
        assert repo.per_repo_concurrency == 1

    def test_project_override_per_repo_concurrency(self, missions_yaml, tmp_path):
        project_cfg = tmp_path / ".agentwire.yml"
        project_cfg.write_text(
            yaml.safe_dump({"missions": {"repo": "agentwire-dev", "per_repo_concurrency": 2}})
        )
        cfg = load_config(global_path=missions_yaml, project_config_path=project_cfg)
        assert cfg.repos["agentwire-dev"].per_repo_concurrency == 2

    def test_project_override_for_unknown_repo_is_ignored(self, missions_yaml, tmp_path):
        project_cfg = tmp_path / ".agentwire.yml"
        project_cfg.write_text(
            yaml.safe_dump({"missions": {"repo": "ghost-repo", "per_repo_concurrency": 99}})
        )
        cfg = load_config(global_path=missions_yaml, project_config_path=project_cfg)
        assert "ghost-repo" not in cfg.repos
        assert cfg.repos["agentwire-dev"].per_repo_concurrency == 1

    def test_malformed_yaml_falls_back_to_defaults(self, tmp_path):
        p = tmp_path / "broken.yaml"
        p.write_text("::not valid yaml: [")
        cfg = load_config(global_path=p)
        assert cfg.global_concurrency == 3
        assert cfg.repos == {}

    def test_get_repo(self, missions_yaml):
        cfg = load_config(global_path=missions_yaml)
        repo = cfg.get_repo("agentwire-dev")
        assert repo is not None
        assert repo.name == "dotdevdotdev/agentwire-dev"
        assert cfg.get_repo("nope") is None

    def test_tilde_in_projects_dir_expanded(self, tmp_path):
        p = tmp_path / "missions.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "repos": {
                        "x": {"name": "owner/x", "projects_dir": "~/projects"},
                    }
                }
            )
        )
        cfg = load_config(global_path=p)
        assert str(cfg.repos["x"].projects_dir).startswith(str(Path.home()))
