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

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

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
    ("chown", ["--recursive"], ["root", "/srv/x"]),
    ("git clean", ["--force"], ["src", "docs"]),
    ("git branch", ["-d -f", "-df", "--delete --force"], ["main", "other"]),
    ("chmod", ["-R"], ["0777", "/srv/x"]),
    ("kubectl delete", ["--all"], ["all", "pods"]),
    ("killall", ["-9"], ["foo", "bar"]),
    ("pkill", ["-9"], ["foo", "bar"]),
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
    # review round 1: long options, force-delete spelled -d + force, other rules
    "git clean --force",
    "git clean src --force",
    "git branch -d -f x",
    "git branch -df x",
    "git branch --delete --force x",
    "chown --recursive root /",
    "chmod 0777 -R /x",
    "kubectl delete --all all",
    "mail a@b -s x",
    "mailx a@b -s x",
    "killall foo -9",
    "pkill foo -9",
]

# Spellings main already refused (plain order): the rewrite must never weaken one.
STILL_REFUSED = [
    "rm -rf x", "rm -fr x", "rm -r x", "rm -f x", "rm -vrf x", "rm --recursive x",
    "rm --force x", "git clean -fdx", "git clean -f", "git clean -d", "git clean -df",
    "git push -f", "git push --force", "git push -fu origin x", "git branch -D x",
    "git reset --hard", "git push origin --delete x", "chown -R root /",
    "chown -Rv root /", "chmod -R 777 /x", "chmod 777 x", "kubectl delete all --all",
    "mail -s x a@b", "killall -9 foo", "pkill -9 foo",
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
    "git clean -f -n",
    "git clean src -f -n",
    "git clean -n -f",
    "git clean -fd --dry-run",
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


def test_main_refusals_survive_the_rewrite(bash_hook, bundled_config):
    bad = [c for c in STILL_REFUSED if bash_hook.check_command(c, dict(bundled_config))["decision"] == "allow"]
    assert not bad, bad


def _generator():
    spec = importlib.util.spec_from_file_location("gen_rule_patterns", REPO / "scripts" / "gen_rule_patterns.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shipped_patterns_are_plain_regex_the_hook_needs_no_expansion_for():
    """Deploy skew (#1125 review): a hook that does not know the placeholder must
    still read a valid pattern, or every rule using it silently stops matching."""
    bad = []
    for path in sorted(RULES_DIR.glob("*.yaml")):
        for rule in yaml.safe_load(path.read_text()).get("bashToolPatterns", []):
            if "{ARGS}" in rule["pattern"]:
                bad.append((path.name, rule["pattern"]))
    assert not bad, bad


def test_generator_output_is_current_and_expands_what_it_claims():
    gen = _generator()
    stale = [p.name for p in sorted(RULES_DIR.glob("*.yaml")) if gen.regenerate(p.read_text()) != p.read_text()]
    assert not stale, stale
    n = 0
    for path in sorted(RULES_DIR.glob("*.yaml")):
        for rule in yaml.safe_load(path.read_text()).get("bashToolPatterns", []):
            if "pattern_src" in rule:
                n += 1
                assert gen.expand(rule["pattern_src"]) == rule["pattern"], rule["pattern_src"]
    assert n >= 12, n


def test_args_fragment_stops_at_double_dash_and_separators():
    gen = _generator()
    rx = re.compile(gen.expand(r"\bverb\s+{ARGS}-f\b"), re.I)
    hits = {c: bool(rx.search(c)) for c in [
        "verb a -f", "verb a b -f", "verb -f", "verb -- -f", "verb a -- -f",
        "verb a ; -f", "verb a && x -f", "verb a | x -f",
    ]}
    assert hits == {
        "verb a -f": True, "verb a b -f": True, "verb -f": True,
        "verb -- -f": False, "verb a -- -f": False,
        "verb a ; -f": False, "verb a && x -f": False, "verb a | x -f": False,
    }, hits


# One behavior pin per rule, flag after the LAST operand, through the real
# check_command — so a rule that stops scanning operands goes red here, not only
# in the pattern-text companion test.
FLAG_AFTER_LAST_OPERAND = [
    ("rm a --recursive", "rm with --recursive"),
    ("rm a --force", "rm with --force"),
    ("rm a -vrf", "rm with recursive or force"),
    ("git push origin topic --delete", "Deletes remote branch"),
    ("chown root /x -hR", "recursive chown"),
    ("git reset HEAD~3 --hard", "git reset --hard"),
    ("git clean src -fd", "git clean with"),
    ("git push origin main -f", "git push -f"),
    ("git branch main -D", "Force deletes branch"),
    ("chmod 0777 -R /x", "chmod 777"),
    ("chmod -v 0777 /x", "chmod 777"),
    ("killall foo -9", "killall -9"),
    ("pkill foo -9", "pkill -9"),
    ("mail a@b -s x", "mail -s"),
    ("kubectl delete --all all", "kubectl delete all --all"),
]


def test_flag_after_last_operand_hits_its_own_rule(bash_hook, bundled_config):
    bad = []
    for command, reason in FLAG_AFTER_LAST_OPERAND:
        result = bash_hook.check_command(command, dict(bundled_config))
        if not (result.get("reason") or "").startswith(reason):
            bad.append((command, result["decision"], result.get("reason")))
    assert not bad, bad
