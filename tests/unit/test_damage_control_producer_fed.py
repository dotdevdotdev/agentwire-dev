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

    def _decide(command, setting="default", home=HOMES[0], where=None):
        monkeypatch.setenv("HOME", home)
        if setting == "unattended":
            monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
        else:
            monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)
        cfg = dict(bundled_config)
        cfg["safety"] = dict(SAFETY, enabled=(setting != "kill_switch_off"))
        return bash_hook.check_command(command, cfg, cwd=where or str(cwd))["decision"]

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


def test_relative_search_is_judged_by_where_it_can_reach(decide, tmp_path):
    """A producer with no protected argument in an unguarded cwd is allowed (these were allowed
    before #1128); a name filter that could match a guarded name, an unresolved variable root, or a
    cwd INSIDE a guarded dir is not."""
    refused = [
        "find . -name 'README*' | xargs rm",
        "find . -name '*.md' | xargs rm",
        'find "$d" | xargs rm',
        'ls $d | xargs rm',
        "git ls-files ~/.claude | xargs rm",
        "git -C ~/.claude ls-files | xargs rm",
    ]
    allowed = [
        "find . -name '*.o' | xargs rm",
        "find . -type f | xargs rm",
        "find . -mtime +7 | xargs rm",
        "ls | xargs rm",
        "ls build | xargs rm",
        "ls -t | tail | xargs rm",
        "git ls-files | xargs rm",
        "git ls-files src | xargs rm",
        "fd -e o . build | xargs rm",
        # a cp/mv with a $var operand is not a producer, and a search verb in ANOTHER
        # segment is not feeding it (wrongful unverifiable ask on benign scripts)
        "cp a.txt $D/b.txt; grep -n foo bar.json",
        "for i in a b; do cp t/$i.webp /tmp/$i.webp; done; grep -n foo bar.json",
        "mv $a /tmp/b; ls",
        "mv $a /tmp/b; find . -name x",
        "cp $a /tmp/b && git ls-files",
    ]
    bad = [(c, got) for c in refused if (got := decide(c)) == "allow"]
    bad += [(c, got) for c in allowed if (got := decide(c)) != "allow"]
    guarded_cwd = str(tmp_path / "home" / ".claude")
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    for c in ("ls | xargs rm", "find . -type f | xargs rm", "git ls-files | xargs rm"):
        if (got := decide(c, home=str(tmp_path / "home"), where=guarded_cwd)) == "allow":
            bad.append((c, "cwd inside a guarded dir", got))
    assert not bad, bad


def test_execdir_ok_and_parallel_forms_equal_the_exec_decision(decide):
    bad = []
    for root in ("~/.claude/CLAUDE.md", "~/.claude", "~", "~/.agentwire/settings.json", "/tmp/work"):
        base = decide(f"find {root} -name x -exec rm {{}} \\;")
        for prim in ("-execdir", "-ok", "-okdir"):
            got = decide(f"find {root} -name x {prim} rm {{}} \\;")
            if got != base:
                bad.append((root, prim, got, base))
    for target in NO_DELETE_TARGETS:
        direct = decide(f"rm {target}")
        for c in (f"parallel 'rm {{}}' ::: {target}", f"parallel rm ::: {target}", f"git ls-files {target} | xargs rm"):
            if (got := decide(c)) != direct:
                bad.append((c, got, direct))
    assert not bad, bad


MOVE_BLOCKED = [
    "mv ~/.claude/CLAUDE.md /var/tmp/away",
    "mv ~/.claude ~/.claude.bak",
    "mv ~/.agentwire /tmp/aw",
    "mv ~/.agentwire/sessions /tmp/s",
    "mv -t /tmp/aw ~/.agentwire",
    "mv * /tmp/away",
    "find ~/.claude | xargs truncate -s0",
    "find ~/.claude -name CLAUDE.md -exec truncate -s0 {} +",
    "cat /dev/null > ~/.claude/CLAUDE.md",
    "true > ~/.claude/CLAUDE.md",
    "printf '' > ~/.claude/CLAUDE.md",
    ": >| ~/.claude/CLAUDE.md",
    "tee ~/.claude/CLAUDE.md </dev/null",
    "mv -t /tmp/away ~/.claude/CLAUDE.md",
    # target-directory parsing: only a correct -t / --target-directory reads the SOURCE as a source
    "mv -t /tmp/away ~/.claude",
    "mv --target-directory=/tmp/away ~/.agentwire",
    "mv --target-directory /tmp/away ~/.agentwire",
    "fd . ~/.claude | xargs rm",
    "fd -e md . ~/.agentwire | xargs rm",
    "mv --target-directory=/tmp/away ~/.claude/CLAUDE.md",
    "mv -f ~/.agentwire/settings.json /tmp/s",
    "mv ~/.agentwire/sessions /tmp/s",
    "mv /tmp/a ~/.agentwire/settings.json",
    "mv -T /tmp/a ~/.agentwire/settings.json",
    "mv -f /tmp/a docs/CLAUDE.md",
    "mv README.md /tmp/r",
    "mv /tmp/new README.md",
    "git mv README.md docs/README.md",
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


STATE_MOVES_ALLOWED = [
    # the real owner command (audit log): a backup-rename of runtime state
    "mv ~/.agentwire/cohorts/x.json x.json.held",
    "mv ~/.agentwire/cohorts/x.json ~/.agentwire/cohorts/x.json.held",
    "mv ~/.agentwire/inbox/s/a.json /tmp/a.json",
    "mv /tmp/a.json ~/.agentwire/inbox/s/a.json",
    "cp /tmp/a ~/.agentwire/cohorts/y.json",
    "find ~/.agentwire/cohorts -name '*.json' | xargs -I{} mv {} /tmp/old",
    ": > ~/.agentwire/cohorts/y.json",
    "echo x > ~/.claude/CLAUDE.md",
]


def test_runtime_state_is_not_in_the_mv_ladder(decide):
    """Directory entries guard state agents move and replace routinely; only the dir itself counts."""
    bad = [(c, s, got) for c in STATE_MOVES_ALLOWED for s in SETTINGS if (got := decide(c, s)) != "allow"]
    assert not bad, bad


def test_renaming_a_guarded_file_inside_its_own_directory_asks(decide):
    renames = [
        "mv ~/.claude/CLAUDE.md ~/.claude/CLAUDE.md.bak",
        "mv ~/.agentwire/settings.json ~/.agentwire/settings.json.bak",
        "mv README.md README.old",
        "git -C ~/.claude mv CLAUDE.md x",
    ]
    bad = []
    for c in renames:
        for setting, want in (("default", "ask"), ("unattended", "ask"), ("kill_switch_off", "allow")):
            if (got := decide(c, setting)) != want:
                bad.append((c, setting, got))
    assert not bad, bad
