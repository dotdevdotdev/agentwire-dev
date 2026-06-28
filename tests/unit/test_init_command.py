"""Tests for `agentwire init` default vs --assisted dispatch (issue #493)."""

from argparse import Namespace
from unittest.mock import patch

from agentwire.system_cli import cmd_init


def _run(assisted: bool) -> dict:
    """Invoke cmd_init with the version/pip preflight stubbed out, capturing
    the skip_session value passed to run_onboarding."""
    captured = {}

    def fake_onboarding(skip_session: bool = True) -> int:
        captured["skip_session"] = skip_session
        return 0

    with patch("agentwire.system_cli.check_python_version", return_value=True), \
         patch("agentwire.system_cli.check_pip_environment", return_value=True), \
         patch("agentwire.onboarding.run_onboarding", side_effect=fake_onboarding):
        rc = cmd_init(Namespace(assisted=assisted))

    captured["rc"] = rc
    return captured


def test_init_default_skips_agent_session():
    """Default `agentwire init` ends on the portal-URL next steps."""
    result = _run(assisted=False)
    assert result["skip_session"] is True
    assert result["rc"] == 0


def test_init_assisted_spawns_agent_session():
    """`agentwire init --assisted` opts back into the Claude setup session."""
    result = _run(assisted=True)
    assert result["skip_session"] is False
    assert result["rc"] == 0
