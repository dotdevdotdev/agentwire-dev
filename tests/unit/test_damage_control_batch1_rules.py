"""#1133 — rule-YAML residual closure: kill, chmod operand, rsync --delete, crontab, set-form git config.

Each family is ONE test over a decision table (a failure lists every wrong row).
The wrapper differential asserts decision(inside a wrapper) == decision(top level).
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"
SAFETY = {"enabled": True, "disabled_rules": [], "unattended_allow": []}


@pytest.fixture(scope="module")
def decide(bash_hook):
    cfg = dict(bash_hook.load_config(RULES_DIR, TOOLDEFS_DIR))
    assert not cfg.get("_parser_unavailable"), "rules failed to load"
    cfg["safety"] = SAFETY
    return lambda cmd: bash_hook.check_command(cmd, cfg)["decision"]


TABLE = [
    # kill: -1 is the all-processes target whenever a signal option rides along
    ("kill -9 -1", "block"), ("kill -1 -9", "block"), ("kill -KILL -1", "block"),
    ("kill -s KILL -1", "block"), ("kill -TERM -- -1", "block"), ("kill -- -1", "block"),
    ("kill -1 123", "allow"), ("kill 123", "allow"), ("kill -HUP 123", "allow"),
    ("kill -15 123", "allow"), ("kill -l", "allow"), ("tmux kill-session -t x", "allow"),
    # kill -9 / -KILL / -s KILL: ask
    ("kill -9 123", "ask"), ("kill -KILL 123", "ask"), ("kill -s KILL 123", "ask"),
    ("kill -sKILL 123", "ask"), ("kill 123 -9", "ask"), ("kill -n 9 123", "ask"),
    ("kill --signal=KILL 123", "ask"), ("kill -SIGKILL 123", "ask"),
    # chmod: the mode is the first non-flag operand
    ("chmod u+x 777", "allow"), ("chmod 755 777", "allow"),
    ("chmod -R u+x 777", "block"),  # priced: the recursive rule stays wide on purpose
    ("chmod +x run.sh", "allow"), ("chmod 777 /x", "block"), ("chmod -R 777 /x", "block"),
    ("chmod -- 777 x", "block"), ("chmod 0777 x", "block"), ("chmod 777 -R x", "block"),
    ("chmod -v 777 f", "block"),
    # rsync
    ("rsync -a --delete src/ dst/", "ask"), ("rsync --delete-after a b", "ask"),
    ("rsync a b --delete", "ask"), ("rsync --remove-source-files a b", "ask"),
    ("rsync --del a b", "ask"), ("rsync -a src/ dst/", "allow"),
    ("rsync -a --no-delete a b", "allow"), ("rsync --max-delete=5 a b", "allow"),
    # crontab: only -l is read-only
    ("crontab -l", "allow"), ("crontab -u me -l", "allow"), ("crontab mycron.txt", "ask"),
    ("crontab -", "ask"), ("crontab -e", "ask"), ("crontab -r", "ask"), ("crontab", "ask"),
    ("crontab -l | crontab -", "ask"),
    # git config, set form: a bare program name stays allowed, anything git would execute asks
    ("git config core.editor vim", "allow"), ("git config --global core.editor vim", "allow"),
    ("git config core.fsmonitor true", "allow"), ("git config core.editor", "allow"),
    ("git config --get core.editor", "allow"), ("git config --unset core.worktree", "allow"),
    ("git config --list", "allow"), ("git config user.name 'Dot Dev'", "allow"),
    ("git config alias.co checkout", "allow"), ("git config alias.st 'status -s'", "allow"),
    ("git config core.editor 'touch x'", "ask"), ("git config core.pager /tmp/x.sh", "ask"),
    ("git config --global core.sshCommand 'ssh -i k'", "ask"),
    ("git config core.worktree /x", "ask"), ("git config core.hooksPath .hooks", "ask"),
    ("git config include.path ../x", "ask"), ("git config --global core.hooksPath /x", "ask"),
    ("git -C d config core.worktree /x", "ask"), ("git config set core.worktree /x", "ask"),
    ("git config alias.x '!touch y'", "ask"), ("git config filter.x.clean 'sh y'", "ask"),
    ("git config --file f core.worktree /x", "ask"),
    # reviewer r1: -1 after any option/pids, long options, any signal
    ("kill --signal TERM -1", "block"), ("kill --signal KILL -1", "block"), ("kill --signal=KILL -1", "block"),
    ("kill -s KILL 1 -1", "block"), ("kill -n 9 123 -1", "block"), ("kill 123 -1", "block"),
    ("kill -s HUP -1", "block"), ("kill --signal TERM 123", "allow"), ("builtin kill -9 -1", "block"),
    ("builtin kill -9 123", "ask"),
    # exec keys: only a known editor/pager name is bare-safe
    ("git config core.editor sh", "ask"), ("git config core.editor bash", "ask"),
    ("git config core.editor curl", "ask"), ("git config core.editor python3", "ask"),
    ("git config core.editor rm", "ask"), ("git config core.pager sh", "ask"),
    ("git config core.alternateRefsCommand x", "ask"), ("git config sequence.editor x", "ask"),
    ("git config gpg.program x", "ask"), ("git config uploadpack.packObjectsHook x", "ask"),
    ("git config merge.x.driver sh", "ask"), ("git config remote.o.receivepack x", "ask"),
    ("git config core.askpass x", "ask"), ("git config core.editor vim\\ -f", "ask"),
    ("git config core.pager sh\\ x", "ask"), ("git config core.pager less", "allow"),
    ("git config core.editor nano", "allow"),
    ("rsync --delete-missing-args a b", "ask"), ("rsync --delete-delay a b", "ask"),
    ("crontab -lu me", "allow"), ("crontab -ul me", "allow"), ("crontab --list", "allow"),
    ("crontab -V", "allow"), ("cat crontab.sh", "allow"), ("bash crontab.sh", "allow"),
    ("vim ./crontab", "allow"), ("/usr/bin/crontab file", "ask"),
    ("git config core.hooksPath .githooks", "ask"),
    ("find . -exec chmod 777 {} +", "block"), ("find . -type f -execdir chmod -R 0777 {} ;", "block"),
    ("find . -exec chmod 644 {} +", "allow"),
    # mentions are content, not commands
    ("echo 'git config core.hooksPath /x'", "allow"), ("echo kill -9 -1", "allow"),
    ("echo rsync --delete", "allow"), ("echo crontab", "allow"),
]


def test_decision_table(decide):
    bad = [(c, want, got) for c, want in TABLE if (got := decide(c)) != want]
    assert not bad, bad


WRAPPERS = ["sudo {c}", "env X=1 {c}", "nohup {c}", "xargs {c}", "bash -c '{c}'",
            "ssh host '{c}'", "echo ok && {c}", "timeout 5 {c}"]
# A hard decision must survive a wrapper; an ask may only be raised to block, never lowered.
RANK = {"allow": 0, "ask": 1, "block": 2}


def test_wrapped_decision_is_not_weaker_than_top_level(decide):
    bad = []
    for c, want in TABLE:
        if want == "allow":
            continue
        for w in WRAPPERS:
            if "'" in c and "'{c}'" in w:
                continue  # nested single quotes would build a different command
            if " -C " in c and w.startswith("timeout"):
                continue  # pre-existing gap for EVERY git rule (`timeout 5 git -C d push --force` allows); not this batch
            got = decide(w.format(c=c))
            if RANK[got] < RANK[want]:
                bad.append((w.format(c=c), want, got))
    assert not bad, bad


def test_rules_are_plain_regexes_for_an_older_hook(bash_hook):
    """A rule must never require a newer hook: every shipped pattern compiles bare (no {ARGS})."""
    cfg = bash_hook.load_config(RULES_DIR, TOOLDEFS_DIR)
    unexpanded = [p["id"] if p.get("id") else p["pattern"] for p in cfg["bashToolPatterns"] if "{ARGS}" in p["pattern"]]
    assert not unexpanded, unexpanded


# ---- wrapper x git global option x destructive git op (differential) ----
PREFIX_WRAPPERS = [
    "timeout 5", "gtimeout 5", "nice", "nice -n 5", "ionice -c 3", "caffeinate", "caffeinate -i",
    "env", "env X=1", "sudo", "sudo -u me", "nohup", "time", "stdbuf -o0", "chrt 1", "taskset 0x1",
    "setsid", "command", "exec", "doas", "flock /tmp/l", "unbuffer", "numactl -N 0",
    "sshpass -p x", "strace", "fakeroot", "arch -arm64", "watchexec", "systemd-run", "chronic",
    "nsenter -t 1", "sg grp -c", "runuser -u me --", "chroot /new",
]
GIT_GLOBALS = ["", "-C d", "-c a=b", "--git-dir=g", "--work-tree=w", "--no-pager", "-C d -c a=b --no-pager"]
GIT_OPS = ["push -f origin main", "push --force origin main", "push origin --delete x",
           "reset --hard HEAD~1", "clean -fd", "branch -D x", "checkout -- .", "stash drop",
           "filter-branch", "restore .", "push origin :x"]


def test_wrapper_and_git_global_options_do_not_weaken_destructive_git(decide):
    bad = []
    for op in GIT_OPS:
        want = RANK[decide(f"git {op}")]
        assert want, f"git {op} must be refused at top level"
        for w in PREFIX_WRAPPERS:
            for g in GIT_GLOBALS:
                cmd = f"{w} git {g} {op}".replace("  ", " ")
                if RANK[decide(cmd)] < want:
                    bad.append(cmd)
    assert not bad, bad


def test_wrapped_benign_git_decides_like_top_level(decide):
    benign = ["git status", "git -C d log", "git -c a=b diff", "git --no-pager log -5"]
    bad = [f"{w} {b}" for b in benign for w in PREFIX_WRAPPERS if decide(f"{w} {b}") != decide(b)]
    assert not bad, bad
    assert decide("sudo grep rm x") == "allow" and decide("flock f grep git x") == "allow"
