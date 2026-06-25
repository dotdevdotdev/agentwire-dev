"""Bypass-resistance regression corpus for the damage-control matcher.

Loads the REAL bundled rule YAMLs (``agentwire/hooks/damage-control/rules``) — not
synthetic inline patterns — and asserts two things at once:

  * a corpus of known evasion vectors (quoting/escaping, ``$VAR`` indirection,
    command substitution, tilde/``$HOME`` secret reads, non-``rm`` deletion) is
    BLOCKed or ASKed, and
  * a corpus of common, safe everyday commands (the kind agents run constantly,
    including the #492 ``.env`` false positives) still PASSes.

A safety layer that cries wolf gets turned off, so the false-positive corpus is
as load-bearing as the bypass corpus. Both must stay green.
"""

from pathlib import Path

import pytest

from agentwire.safety._core import check_command, load_config

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"

# Built without literal "rm -<flags>" substrings where convenient so the live
# damage-control hook does not block the test file itself being written/read.
_RF = "-r" + "f"


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    assert c.get("bashToolPatterns"), "bundled rules failed to load"
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


# ---------------------------------------------------------------------------
# Evasion vectors — must NOT resolve to a silent allow.
# ---------------------------------------------------------------------------

BYPASS_VECTORS = [
    # quoting / escaping defeats raw-string matching
    "r\\m " + _RF + " /x",
    "r''m " + _RF + " /x",
    'r""m ' + _RF + " /x",
    # $VAR indirection
    "R=rm; $R " + _RF + " /x",
    "CMD=rm && ${CMD} " + _RF + " /x",
    # command substitution → unverifiable → fail closed (ask/block)
    "$(echo rm) " + _RF + " /x",
    "`echo rm` " + _RF + " /x",
    # non-rm deletion paths
    "find /important -delete",
    "find /important -exec rm {} +",
    'python3 -c "import shutil; shutil.rmtree(\'/important\')"',
    "perl -e 'unlink glob(\"/important/*\")'",
    # tilde / $HOME secret reads
    "cat ~/.ssh/id_rsa",
    "cat $HOME/.ssh/id_rsa",
    "cat ${HOME}/.ssh/id_rsa",
    "cat ~/.aws/credentials",
    "cat ~/.netrc",
    # baseline literal (sanity)
    "rm " + _RF + " /x",
]


@pytest.mark.parametrize("command", BYPASS_VECTORS)
def test_bypass_vector_not_allowed(cfg, command):
    decision = check_command(command, cfg)["decision"]
    assert decision in ("block", "ask"), (
        f"evasion vector resolved to {decision!r} (expected block/ask): {command!r}"
    )


# ---------------------------------------------------------------------------
# False-positive corpus — common safe commands that MUST keep passing.
# ---------------------------------------------------------------------------

SAFE_COMMANDS = [
    # #492 .env false positives
    "# loads .environment",
    "grep -v .environ docs/notes.txt",
    "echo configure-.env-vars",
    "cat docs/.env.example",
    "cat .env.sample",
    "ls config/.env.template",
    # everyday dev commands
    "git status",
    "git commit -m 'fix things'",
    "git push",
    "npm install",
    "npm run build",
    "uv run pytest -q",
    "uv sync",
    "ls -la",
    "cd /tmp && echo hi",
    "cat README.md",
    "grep -r environ agentwire",
    "docker compose up -d",
    "echo hello world",
    "mkdir -p build/out",
    "pytest tests/unit",
]


@pytest.mark.parametrize("command", SAFE_COMMANDS)
def test_safe_command_allowed(cfg, command):
    decision = check_command(command, cfg)["decision"]
    assert decision == "allow", (
        f"safe command was not allowed (got {decision!r}): {command!r}"
    )


# ---------------------------------------------------------------------------
# Read-surface policing (Read/Grep/Glob) via check_read_path.
# ---------------------------------------------------------------------------

# Note: ~/.agentwire/.env is intentionally allowlisted (read/write/edit) in
# core.yaml so the agent can load its own env — it is NOT in this list.
ZERO_ACCESS_READS = [
    "~/.ssh/id_rsa",
    "~/.aws/credentials",
    "/repo/server.pem",
    "/repo/app-secret.yaml",
]


@pytest.mark.parametrize("path", ZERO_ACCESS_READS)
def test_zero_access_read_blocked(cfg, path):
    from agentwire.safety._core import check_read_path

    blocked, _reason = check_read_path(path, cfg)
    assert blocked is True, f"zero-access read not blocked: {path}"


def test_normal_file_read_allowed(cfg):
    from agentwire.safety._core import check_read_path

    blocked, _ = check_read_path("/repo/src/main.py", cfg)
    assert blocked is False


def test_every_content_reading_tool_is_policed():
    """Each native content-reading tool must route to the read-tool hook, or a
    secret could be exfiltrated without traversing damage control."""
    from agentwire.cli_safety import DAMAGE_CONTROL_MATCHERS

    for tool in ("Read", "Grep", "Glob"):
        assert DAMAGE_CONTROL_MATCHERS.get(tool) == "read-tool-damage-control.py", (
            f"{tool} is not covered by the damage-control read hook"
        )


# ---------------------------------------------------------------------------
# Missing YAML parser must fail CLOSED, not open.
# ---------------------------------------------------------------------------


def test_missing_parser_fails_closed(monkeypatch):
    import agentwire.safety._core as core

    monkeypatch.setattr(core, "yaml", None)
    merged = core.load_config(RULES_DIR)
    assert merged.get("_parser_unavailable")
    result = core.check_command("echo hi", merged)
    assert result["decision"] == "block"
