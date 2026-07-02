"""Tests for the PEP 668 externally-managed environment detector (#635)."""

from unittest.mock import patch

from agentwire.core import check_pip_environment


def _patch_stdlib(tmp_path):
    return patch("agentwire.core.sysconfig.get_path", return_value=str(tmp_path))


def _patch_no_venv():
    # sys.prefix == sys.base_prefix → not inside a virtualenv
    return patch.multiple("agentwire.core.sys", prefix="/usr", base_prefix="/usr")


def test_marker_present_returns_false(tmp_path, capsys):
    (tmp_path / "EXTERNALLY-MANAGED").write_text("managed")
    with _patch_stdlib(tmp_path), _patch_no_venv():
        assert check_pip_environment() is False
    out = capsys.readouterr().out
    assert "uv tool install agentwire-dev" in out
    assert "pipx install agentwire-dev" in out


def test_marker_absent_returns_true(tmp_path):
    with _patch_stdlib(tmp_path), _patch_no_venv():
        assert check_pip_environment() is True


def test_runs_on_macos(tmp_path):
    """The detector must not short-circuit on darwin — Homebrew Python ships
    the PEP 668 marker too."""
    (tmp_path / "EXTERNALLY-MANAGED").write_text("managed")
    with _patch_stdlib(tmp_path), _patch_no_venv(), \
            patch("agentwire.core.sys.platform", "darwin"):
        assert check_pip_environment() is False


def test_virtualenv_skips_marker_check(tmp_path):
    """Inside a venv the interpreter is never externally managed, even if the
    base interpreter's stdlib carries the marker."""
    (tmp_path / "EXTERNALLY-MANAGED").write_text("managed")
    with _patch_stdlib(tmp_path), \
            patch.multiple("agentwire.core.sys", prefix="/home/x/.venv", base_prefix="/usr"):
        assert check_pip_environment() is True


def test_marker_checked_in_stdlib_dir(tmp_path):
    """The marker lives in sysconfig's stdlib dir, not sys.prefix (they differ
    on Homebrew/framework builds)."""
    stdlib = tmp_path / "lib" / "python3.12"
    stdlib.mkdir(parents=True)
    (stdlib / "EXTERNALLY-MANAGED").write_text("managed")
    with patch("agentwire.core.sysconfig.get_path", return_value=str(stdlib)) as gp, \
            _patch_no_venv():
        assert check_pip_environment() is False
    gp.assert_called_once_with("stdlib")
