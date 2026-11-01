"""#1217: Read/Edit/Write path checks must agree with the case-insensitive filesystem.

``~/.SSH/ID_RSA`` reaches the same file as ``~/.ssh/id_rsa`` on macOS/NTFS/WSL
drvfs, so a case variant of any guarded path is guarded too. Linux stays
exact-case. A symlink into a guarded dir is judged by where it lands.
"""

import os
import sys

import pytest

from agentwire.safety import _core
from agentwire.safety._core import check_path, check_read_path, load_config, match_path
from agentwire.safety.groups import bundled_rule_dirs

pytestmark = pytest.mark.real_agentwire_home

RULES_DIR = bundled_rule_dirs()
HOME = os.path.expanduser("~")
VARIANTS = (
    ".SSH/ID_RSA",
    ".Ssh/id_rsa",
    ".CLAUDE/Settings.json",
    ".AgentWire/DamageControl.yml",
)


@pytest.fixture
def config():
    return load_config(RULES_DIR)


def _force_platform(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)


def test_case_variants_blocked_on_casefolding_platforms(monkeypatch, config):
    missed = []
    for platform in ("darwin", "win32"):
        _force_platform(monkeypatch, platform)
        for rel in VARIANTS:
            path = f"{HOME}/{rel}"
            if rel.lower().startswith((".ssh",)) and not check_read_path(path, config)[0]:
                missed.append((platform, "read", path))
            if not check_path(path, config)[0]:
                missed.append((platform, "edit/write", path))
    assert not missed, missed


def test_linux_stays_exact_case(monkeypatch):
    _force_platform(monkeypatch, "linux")
    assert match_path(f"{HOME}/.ssh/id_rsa", "~/.ssh/")
    assert not match_path(f"{HOME}/.SSH/id_rsa", "~/.ssh/")


def test_drvfs_mount_casefolds_on_linux(monkeypatch):
    _force_platform(monkeypatch, "linux")
    assert match_path("/mnt/c/Users/me/.SSH/id_rsa", "/mnt/c/users/me/.ssh/")
    assert not match_path("/mnt/data/.SSH/id_rsa", "/mnt/data/.ssh/")


def test_symlink_into_zero_access_dir_is_blocked(tmp_path, monkeypatch, config):
    _force_platform(monkeypatch, "linux")
    target = tmp_path / "secrets"
    target.mkdir()
    link = tmp_path / "innocent"
    link.symlink_to(target)
    # tmp_path sits under /tmp on Linux, which core.yaml allowlists; an allowlist
    # entry outranks zero-access, so isolate the symlink behavior from it.
    monkeypatch.setattr(_core, "_read_global_allowed_paths", lambda: [])
    monkeypatch.setattr(_core, "_find_project_config", lambda: ("", []))
    cfg = dict(config, zeroAccessPaths=[str(target) + "/"], allowedPaths=[])
    path = str(link / "key")
    assert check_read_path(path, cfg)[0]
    assert check_path(path, cfg)[0]
