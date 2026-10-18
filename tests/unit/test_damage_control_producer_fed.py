"""#1128 + #1127 (+ emptying == deleting) — the no-delete ladder judges what a delete/move is FED and what a move touches.

``find ~/.claude | xargs rm`` pairs the verb and the path across a pipe; ``mv
~/.claude/CLAUDE.md /var/tmp/away`` moves a guarded file out of place with a verb
the ladder never read. The differentials assert the pipeline decision equals the
direct-operand decision across consumer spellings, producers, targets, homes and
safety settings; the controls price the other half (scratch deletes and unrelated
moves stay allowed).
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"
SAFETY = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
SETTINGS = ["default", "kill_switch_off", "unattended"]
HOMES = ["/Users/x", "/var/folders/zz/T/x"]


@pytest.fixture(scope="module")
def bundled_config(bash_hook):
    cfg = bash_hook.load_config(RULES_DIR, TOOLDEFS_DIR)
    assert not cfg.get("_parser_unavailable"), "rules failed to load"
    assert cfg.get("noDeletePaths"), "no-delete set did not load"
    return cfg


@pytest.fixture
def decide(bash_hook, bundled_config, monkeypatch, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()

    def _decide(command, setting="default", home=HOMES[0]):
        monkeypatch.setenv("HOME", home)
        if setting == "unattended":
            monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
        else:
            monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)
        cfg = dict(bundled_config)
        cfg["safety"] = dict(SAFETY, enabled=(setting != "kill_switch_off"))
        return bash_hook.check_command(command, cfg, cwd=str(cwd))["decision"]

    return _decide


NO_DELETE_TARGETS = [
    "~/.claude/CLAUDE.md",
    "~/.claude/projects/p/memory/MEMORY.md",
    "~/.agentwire/sessions/s/metadata.json",
    "~/.agentwire/settings.json",
    "docs/CLAUDE.md",
    "README.md",
    "LICENSE",
    ".git/config",
]
SCRATCH_TARGET = "/tmp/work/a.o"

PRODUCERS = [
    "find {t}",
    "ls {t}",
    "cat {t}",
    "echo {t}",
    "grep -l x {t}",
]

PIPE_CONSUMERS = [
    "xargs rm",
    "xargs rm -f",
    "xargs -0 rm",
    "xargs -I{} rm {}",
    "xargs -n1 rm",
    "parallel rm",
    "parallel rm {}",
    "sudo xargs rm",
    'while read f; do rm "$f"; done',
]


def _pipelines(target):
    for producer in PRODUCERS:
        p = producer.format(t=target)
        for consumer in PIPE_CONSUMERS:
            yield f"{p} | {consumer}"
        yield f"rm $({p})"
        yield f"rm `{p}`"
        yield f'for f in $({p}); do rm "$f"; done'
        yield f"echo go && {p} | xargs rm"
        yield f"ssh host '{p} | xargs rm'"


def test_fed_delete_matches_the_direct_operand_decision(decide):
    bad = []
    for target in NO_DELETE_TARGETS:
        for setting in SETTINGS:
            for home in HOMES:
                direct = decide(f"rm {target}", setting, home)
                if setting == "default" and direct == "allow":
                    bad.append(("direct delete allowed", target, home))
                for command in _pipelines(target):
                    got = decide(command, setting, home)
                    if got != direct:
                        bad.append((command, setting, home, got, direct))
    assert not bad, bad[:20]


def test_scratch_deletes_through_pipelines_stay_allowed(decide):
    t = SCRATCH_TARGET
    allowed = [
        "find /tmp/work -name '*.o' | xargs rm",
        "find /tmp/work -type f -print0 | xargs -0 rm",
        "ls /tmp/work | parallel rm",
        "cat /tmp/list | xargs rm",
        f"echo {t} | xargs rm",
        f"printf '%s\\n' {t} | xargs -I{{}} rm {{}}",
        "grep -l foo /tmp/work/*.log | xargs rm",
        "find /tmp/work -name '*.o' -exec rm {} +",
    ]
    bad = [(c, s, got) for c in allowed for s in SETTINGS if (got := decide(c, s)) != "allow"]
    assert not bad, bad


def test_search_root_above_a_guarded_path_is_not_allowed(decide):
    """The producer's output is unknown: a root that contains the guarded path never allows."""
    roots = ["~/.claude", "~/.agentwire", "~", "$HOME/.claude", "/Users/x", "/Users/x/.claude/projects"]
    spellings = [
        "find {r} -name '*.o' | xargs rm",
        "find {r} -type f | xargs rm",
        "ls {r} | xargs rm",
        "grep -rl x {r} | xargs rm",
        "find {r} -type f | parallel rm",
    ]
    bad = []
    for r in roots:
        for spelling in spellings:
            c = spelling.format(r=r)
            for setting, want in (
                ("default", {"ask", "block"}),
                ("unattended", {"ask", "block"}),  # raw tier; the hook turns ask into block
                ("kill_switch_off", {"allow"}),
            ):
                got = decide(c, setting)
                if got not in want:
                    bad.append((c, setting, got))
    assert not bad, bad


def test_relative_search_in_the_repo_cannot_hide_a_guarded_name(decide):
    refused = [
        "find . -type f | xargs rm",
        "find . -name 'README*' | xargs rm",
        "find . -name '*.md' | xargs rm",
        "ls | xargs rm",
    ]
    allowed = [
        "find . -name '*.o' | xargs rm",
    ]
    bad = [(c, got) for c in refused if (got := decide(c)) == "allow"]
    bad += [(c, got) for c in allowed if (got := decide(c)) != "allow"]
    assert not bad, bad


MOVE_BLOCKED = [
    "mv ~/.claude/CLAUDE.md /var/tmp/away",
    "mv ~/.claude/CLAUDE.md ~/.claude/CLAUDE.md.bak",
    "mv -t /tmp/away ~/.claude/CLAUDE.md",
    "mv --target-directory=/tmp/away ~/.claude/CLAUDE.md",
    "mv -f ~/.agentwire/settings.json /tmp/s",
    "mv ~/.agentwire/sessions /tmp/s",
    "mv /tmp/a ~/.agentwire/settings.json",
    "mv -T /tmp/a ~/.agentwire/settings.json",
    "mv -f /tmp/a docs/CLAUDE.md",
    "mv README.md /tmp/r",
    "mv /tmp/new README.md",
    "git mv README.md docs/README.md",
    "git -C ~/.claude mv CLAUDE.md x",
    "rename s/a/b/ ~/.claude/CLAUDE.md",
    "cp /tmp/a ~/.agentwire/settings.json",
    "cp -f /tmp/a CLAUDE.md",
    "install -m644 /tmp/a ~/.agentwire/settings.json",
    "rsync -a /tmp/a ~/.agentwire/settings.json",
    "ln -sf /tmp/a ~/.agentwire/settings.json",
    "ln -f /tmp/a LICENSE",
    "sudo mv ~/.claude/CLAUDE.md /tmp/x",
    "nohup mv ~/.claude/CLAUDE.md /tmp/x",
    "ssh host 'mv ~/.claude/CLAUDE.md /tmp/x'",
    "echo ok && mv ~/.claude/CLAUDE.md /tmp/x",
    "if true; then mv ~/.claude/CLAUDE.md /tmp/x; fi",
    "( mv ~/.claude/CLAUDE.md /tmp/x )",
    "bash -c 'mv ~/.claude/CLAUDE.md /tmp/x'",
    "echo ~/.claude/CLAUDE.md | xargs -I{} mv {} /tmp/away",
    "find ~/.claude -name CLAUDE.md | xargs mv -t /tmp/away",
    "find ~/.claude -name CLAUDE.md -exec mv {} /tmp/away \\;",
    # emptying == deleting
    "truncate -s0 ~/.claude/CLAUDE.md",
    "truncate -s 0 ~/.agentwire/settings.json",
    "dd if=/dev/null of=~/.claude/CLAUDE.md",
    "dd if=/dev/null of=README.md bs=1",
    ": > ~/.claude/CLAUDE.md",
    ":> ~/.agentwire/settings.json",
    "sudo truncate -s0 ~/.claude/CLAUDE.md",
    "echo ok && : > README.md",
]

MOVE_ALLOWED = [
    "mv /tmp/a /tmp/b",
    "mv a.o b.o",
    "mv out.o out.old",
    "mv /tmp/a ~/.claude/",
    "mv /tmp/a ~/.claude/brand-new-file.txt",
    "cp /tmp/a ~/.claude/",
    "cp -r /tmp/a /tmp/b",
    "cp ~/.claude/CLAUDE.md /tmp/backup",
    "cp README.md /tmp/r",
    "rsync -a ~/.agentwire/sessions/ /tmp/s/",
    "ln -s ~/.agentwire/settings.json /tmp/link",
    "install -m644 /tmp/a /tmp/b",
    "git mv src/a.py src/b.py",
    "find /tmp/work -name '*.o' | xargs -I{} mv {} /tmp/old",
    "echo mv ~/.claude/CLAUDE.md",
    "grep -n mv ~/.claude/CLAUDE.md",
    "truncate -s0 /tmp/scratch.log",
    "dd if=/dev/zero of=/tmp/blob bs=1k count=1",
    ": > /tmp/scratch.log",
    "echo x > /tmp/scratch.log",
]


def test_moving_overwriting_or_emptying_a_guarded_path_is_refused(decide):
    bad = []
    for command in MOVE_BLOCKED:
        for home in HOMES:
            for setting, want in (("default", "block"), ("unattended", "block"), ("kill_switch_off", "allow")):
                got = decide(command, setting, home)
                if got != want:
                    bad.append((command, setting, home, got))
    assert not bad, bad


def test_unrelated_moves_and_copies_stay_allowed(decide):
    bad = [(c, s, got) for c in MOVE_ALLOWED for s in SETTINGS if (got := decide(c, s)) != "allow"]
    assert not bad, bad


def test_destination_inside_a_guarded_dir_is_refused_only_when_it_exists(decide, tmp_path):
    """A new file moved INTO a no-delete dir is fine; replacing a file already there is an overwrite."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    h = str(home)
    assert decide("mv /tmp/a ~/.claude/settings.json", home=h) == "block"
    assert decide("cp /tmp/a ~/.claude/settings.json", home=h) == "block"
    assert decide("mv /tmp/a ~/.claude/never-existed.json", home=h) == "allow"
    assert decide("cp /tmp/a ~/.claude/never-existed.json", home=h) == "allow"
