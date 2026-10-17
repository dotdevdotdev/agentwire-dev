"""#1125 — destructive flags are order-insensitive (GNU/git accept options after operands).

A rule spelled ``verb\\s+(-[^\\s]*)*-f`` saw the flag only BEFORE the first
operand, so ``rm foo -rf x`` / ``chown root -R /`` / ``git clean . -fdx`` /
``git reset HEAD~3 --hard`` / ``git branch main -D`` / ``git push origin -f
main`` were allowed (or demoted block -> ask). The rules now use ``{ARGS}``
(``_core.RULE_ARGS_FRAGMENT``): scan every later argument of the segment, stop
at a separator and at the bare ``--``.

The differential asserts decision(flag after operand) == decision(plain order)
across wrappers and safety settings; the controls price the other half.
"""

from pathlib import Path

import pytest

from agentwire.safety import _core

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"
SAFETY = {"enabled": True, "disabled_rules": [], "unattended_allow": []}


@pytest.fixture(scope="module")
def bundled_config(bash_hook):
    """Bundled rules AND tooldefs — what the real hook loads, not ~/.agentwire."""
    cfg = bash_hook.load_config(RULES_DIR, TOOLDEFS_DIR)
    assert not cfg.get("_parser_unavailable"), "rules failed to load"
    return cfg


# (verb prefix, flag spellings, operands) — the plain order is `verb FLAGS ops`.
FAMILIES = [
    ("rm", ["-rf", "-fr", "-Rf", "-r -f", "-f", "-r", "--recursive --force", "--force"], ["foo", "bar"]),
    ("chown", ["-R"], ["root", "/srv/x"]),
    ("git clean", ["-fdx", "-f", "-fd"], ["src", "docs"]),
    ("git reset", ["--hard"], ["HEAD~3", "main"]),
    ("git branch", ["-D"], ["main", "other"]),
    ("git push", ["-f", "--force", "-fu"], ["origin", "main"]),
    ("git push", ["--delete"], ["origin", "topic"]),
]

WRAPPERS = [
    "{c}",
    "sudo {c}",
    "env X=1 {c}",
    "nohup {c}",
    "xargs {c}",
    "ssh host '{c}'",
    "echo ok && {c}",
    "true; {c}",
    "if true; then {c}; fi",
    "( {c} )",
    "echo $({c})",
]

SETTINGS = ["default", "kill_switch_off", "unattended"]


def _positions(verb, flags, ops):
    before = f"{verb} {flags} {' '.join(ops)}"
    first = f"{verb} {ops[0]} {flags} {' '.join(ops[1:])}"
    last = f"{verb} {' '.join(ops)} {flags}"
    return {"before": before, "after_first": first, "after_last": last}


def _decide(bash_hook, cfg, command, setting, monkeypatch):
    cfg = dict(cfg)
    cfg["safety"] = dict(SAFETY, enabled=(setting != "kill_switch_off"))
    if setting == "unattended":
        monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
    else:
        monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)
    return bash_hook.check_command(command, cfg)["decision"]


def test_flag_position_does_not_change_the_decision(bash_hook, bundled_config, monkeypatch):
    bad = []
    for verb, flag_spellings, ops in FAMILIES:
        for flags in flag_spellings:
            for wrapper in WRAPPERS:
                for setting in SETTINGS:
                    decisions = {
                        pos: _decide(
                            bash_hook, bundled_config, wrapper.format(c=cmd), setting, monkeypatch
                        )
                        for pos, cmd in _positions(verb, flags, ops).items()
                    }
                    plain = decisions["before"]
                    if setting == "default" and wrapper == "{c}" and plain == "allow":
                        bad.append(("plain order allowed", verb, flags))
                    for pos in ("after_first", "after_last"):
                        if decisions[pos] != plain:
                            bad.append((verb, flags, wrapper, setting, pos, decisions[pos], plain))
    assert not bad, bad


# Each spelling must be refused somewhere the issue named it, by command alone.
ISSUE_REPROS = [
    "rm foo -rf /tmp/x",
    "rm foo -fr bar",
    "rm foo --recursive --force bar",
    "rm foo -r -f bar",
    "chown root -R /",
    "git clean . -fdx",
    "git reset HEAD~3 --hard",
    "git branch main -D",
    "git push origin -f main",
    "git push origin --delete topic",
]


def test_issue_repros_are_refused(bash_hook, bundled_config):
    cfg = dict(bundled_config)
    bad = [c for c in ISSUE_REPROS if bash_hook.check_command(c, cfg)["decision"] == "allow"]
    assert not bad, bad


FLAG_RULE_REASON_PREFIXES = (
    "rm with ",
    "recursive chown",
    "git clean with",
    "git reset --hard",
    "git push -f",
    "git push --force",
    "Force deletes branch",
    "Deletes remote branch",
)

# Operations that merely LOOK like the flagged ones: after `--` the flag text is
# an operand, a different flag is a different operation, and quoted/echoed text
# is data.
MUST_NOT_HIT_A_FLAG_RULE = [
    "rm -- -rf",
    "rm foo.txt",
    "git branch -d feature",
    "git branch main",
    "chown root file",
    "git reset HEAD~3",
    "git reset --soft HEAD~3",
    "git clean -n",
    "git clean . -nd",
    "git push origin main",
    "git push --force-with-lease origin main",
    "echo rm foo -rf",
    'git commit -m "rm foo -rf"',
    "ls -f && git status",
    "rm foo && ls -rf",
    "git status; git clean -n",
]


def test_lookalikes_do_not_hit_a_flag_rule(bash_hook, bundled_config):
    bad = []
    for command in MUST_NOT_HIT_A_FLAG_RULE:
        result = bash_hook.check_command(command, dict(bundled_config))
        reason = result.get("reason") or ""
        if result["decision"] != "allow" and reason.startswith(FLAG_RULE_REASON_PREFIXES):
            bad.append((command, reason))
    assert not bad, bad


def test_args_fragment_stops_at_double_dash_and_separators():
    rx = _core._rx(_core.expand_rule_pattern(r"\bverb\s+{ARGS}-f\b"))
    hits = {c: bool(rx.search(c)) for c in [
        "verb a -f", "verb a b -f", "verb -f", "verb -- -f", "verb a -- -f",
        "verb a ; -f", "verb a && x -f", "verb a | x -f",
    ]}
    assert hits == {
        "verb a -f": True, "verb a b -f": True, "verb -f": True,
        "verb -- -f": False, "verb a -- -f": False,
        "verb a ; -f": False, "verb a && x -f": False, "verb a | x -f": False,
    }, hits
