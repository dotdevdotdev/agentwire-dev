"""Flagless and indirect writers to the protected control plane (#1095).

The Bash ladder used to name only writers that carry a write TOKEN (``>``,
``tee``, ``cp``, ``rm``, ``sed -i`` …). A writer whose only link to the file is
an operand — ``ln -sf x <file>``, ``curl -o <file>``, ``gzip <file>``,
``git restore <file>`` — or whose path arrives through a pipe, a loop, an
environment variable or a split string, passed. These tests pin the OPERATION,
not the phrasing: every canary is one writer, every control is the read-shaped
sibling that must keep working (a wrongful block costs a rephrase, a wrongful
allow is a silent control-plane write).

Loads the bundled rules (``agentwire/hooks/damage-control/rules``) — never the
live ``~/.agentwire/damage-control`` copies, which do not refresh and would
make this measure the machine instead of the code.
"""

import os
from pathlib import Path

import pytest
import yaml

from agentwire.safety._core import check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"

HOME = os.path.expanduser("~")
# One path, three spellings the hook must treat identically.
SPELLINGS = [
    "~/.agentwire/damagecontrol.yml",
    "$HOME/.agentwire/damagecontrol.yml",
    f"{HOME}/.agentwire/damagecontrol.yml",
]


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


def _blocked_by_control_plane(command, cfg):
    r = check_command(command, cfg)
    return r["decision"] == "block" and r.get("protected") is True


def test_bundled_rules_all_loaded(cfg):
    """The matrix below is measured against the whole bundled rule set."""
    expected = 0
    for f in sorted(RULES_DIR.glob("*.yaml")):
        expected += len((yaml.safe_load(f.read_text()) or {}).get("bashToolPatterns") or [])
    assert expected > 0
    assert len(cfg["bashToolPatterns"]) == expected


# ``@P`` is the protected file in each spelling above. One entry per writer
# SHAPE; every one of these passed on main before #1095.
MUST_BLOCK = [
    # plain writers with no flags: the path is just an operand
    "ln -sf /tmp/x @P",
    "ln /tmp/x @P",
    "cd /tmp && ln -sfn /tmp/x @P",
    "curl -o @P http://e.com/x",
    "curl -sSLo @P http://e.com/x",
    "curl http://e.com/x --output @P",
    "curl -c @P http://e.com/x",
    "curl -D @P http://e.com/x",
    "wget -O @P http://e.com/x",
    "wget -qO @P http://e.com/x",
    "wget --output-document=@P http://e.com/x",
    "wget -o @P http://e.com/x",
    "tar xf /tmp/a.tar @P",
    "tar -xzf /tmp/a.tgz --directory /tmp @P",
    "tar cf @P /tmp/x",
    "gzip @P",
    "gunzip @P.gz",
    "bzip2 @P",
    "xz -9 @P",
    "zstd -q @P",
    "patch @P /tmp/p.diff",
    "patch -p0 @P < /tmp/p.diff",
    "touch @P",
    "sponge @P",
    "git restore @P",
    "git restore --worktree @P",
    "git restore --staged --worktree @P",
    "git checkout -- @P",
    "git checkout HEAD -- @P",
    "git checkout HEAD~3 @P",
    # extraction/download INTO a directory that holds control-plane files
    "tar xf /tmp/a.tar -C ~/.agentwire",
    "tar -xzf /tmp/a.tgz --directory=$HOME/.agentwire/damage-control",
    "tar -C ~/.claude/hooks -xf /tmp/a.tar",
    "tar xf /tmp/a.tar -C ~/.claude",
    "wget -P ~/.agentwire http://e.com/damagecontrol.yml",
    "curl --output-dir ~/.agentwire -O http://e.com/damagecontrol.yml",
    "unzip /tmp/a.zip -d ~/.agentwire",
    "cp -t ~/.agentwire /tmp/x",
    "mv -t ~/.agentwire/damage-control /tmp/x",
    # xargs is a wrapper: the verb it runs decides, as if unwrapped
    "echo @P | xargs rm",
    "echo @P | xargs -n1 rm",
    "echo @P | xargs -I{} ln -sf /tmp/x {}",
    "echo @P | xargs -I % cp /tmp/x %",
    "echo @P | xargs gzip",
    "echo @P | xargs touch",
    "echo @P | xargs cp /tmp/x",
    "echo @P | xargs -n1 -I % sh -c 'echo hi > %'",
    "printf '%s\\n' @P | xargs -0 rm",
    # shells fed code or paths through a pipe / heredoc / argv / loop
    "printf 'rm %s' @P | sh",
    "printf 'rm %s' @P | bash -s",
    "sh <<EOF\nrm @P\nEOF",
    "bash <<'EOF'\ncp /tmp/x @P\nEOF",
    "bash -c 'echo hi > \"$1\"' _ @P",
    "while read f; do rm \"$f\"; done <<< \"@P\"",
    "echo @P | while read f; do : > \"$f\"; done",
    "for f in @P; do rm \"$f\"; done",
    "for f in @P; do echo x > \"$f\"; done",
    # path set in the environment of an inline interpreter
    "X=@P python3 -c 'import os;open(os.environ[\"X\"],\"w\").write(\"a\")'",
    "env X=@P python3 -c 'import os;open(os.environ[\"X\"],\"w\").write(\"a\")'",
    "A=1 X=@P perl -e 'open(F,\">\",$ENV{X})'",
    # review of #1097: redirects after the target, shell fed by a heredoc pipe,
    # the path exported/assigned before an interpreter, a loop handing it to one
    "ln -sf /tmp/x @P 2>/dev/null",
    "ln -sf /tmp/x @P >/dev/null 2>&1",
    "cat <<EOF | sh\nrm @P\nEOF",
    "export X=@P && python3 -c 'import os;open(os.environ[\"X\"],\"w\")'",
    "X=@P; export X; python3 -c 'import os;open(os.environ[\"X\"],\"w\")'",
    "echo @P | while read f; do python3 -c 'import sys;open(sys.argv[1],\"w\")' \"$f\"; done",
    # a quoted operand that is only the path is still the path; wrapper payloads too
    "ln -sf /tmp/x \"@P\"",
    "touch '@P'",
    "sh -c \"touch @P\"",
    # round 2 of #1097: quoted args that EXECUTE are never prose
    "ssh host 'ln -sf /tmp/x @P'",
    "ssh host \"touch @P\"",
    "ssh host \"gzip @P\"",
    "ssh host \"curl -o @P http://e\"",
    "ssh -t host \"bash -c 'touch @P'\"",
    "eval 'touch @P'",
    "eval \"touch @P\"",
    "git commit -m \"$(touch @P)\"",
    "gh issue create --body \"$(curl -o @P http://e)\"",
    "echo \"$(ln -sf /tmp/x @P)\"",
    "echo \"`touch @P`\"",
    "watch -n1 'touch @P'",
    "echo 'touch @P' | xargs sh -c",
    "bash <(echo 'touch @P')",
    # directory spellings that normalize to a protected directory
    "tar xf a.tar -C ~/.agentwire/.",
    "tar xf a.tar -C ~/.agentwire//",
    "tar xf a.tar -C ~/.agentwire/sub/..",
    "tar xf a.tar --one-top-level=~/.agentwire",
    "bsdtar -xf a.tar -C ~/.agentwire",
    # the path assembled from adjacent / concatenated literals
    "python3 -c 'open(\"~/.agentwire/damage\"+\"control.yml\",\"w\")'",
    "python3 -c \"open('$HOME/.agentwire/damage' 'control.yml','w')\"",
    "perl -e 'open(F,\">\",\"~/.agentwire/damage\".\"control.yml\")'",
    # #1098: an UNQUOTED heredoc body expands $(...) / backticks, so the
    # substitution is a command in its own right
    "cat <<EOF\n$(touch @P)\nEOF",
    "cat <<EOF\n`touch @P`\nEOF",
    "git commit -F - <<EOF\nmsg $(touch @P)\nEOF",
    "git commit -m \"$(cat <<EOF\nbody\n$(touch @P)\nEOF\n)\"",
    "cat <<-EOF\n\t$(ln -sf /tmp/x @P)\n\tEOF",
    "cat <<EOF >/tmp/o\nline\n$(curl -o @P http://e)\nEOF",
    # #1099: the wrapped command is the command -- stdin text handed to a runner
    "ssh host <<EOF\ntouch @P\nEOF",
    "ssh -p 22 -o X=y host <<EOF\ntouch @P\nEOF",
    "ssh host bash -s <<EOF\ntouch @P\nEOF",
    "cat <<EOF | ssh host\ntouch @P\nEOF",
    "ssh host <<< 'touch @P'",
    "echo 'touch @P' | ssh host",
    "echo 'touch @P' | ssh host sh",
    "printf 'touch %s\\n' @P | ssh host",
    "echo 'touch @P' | env ssh host",
    "echo 'touch @P' | caffeinate ssh host",
    "echo 'touch @P' | at now",
    "echo 'rm @P' | at 5pm",
    "echo 'touch @P' | batch",
    "echo 'touch @P' | crontab -",
    ". /dev/stdin <<< 'touch @P'",
    "source /dev/stdin <<< 'touch @P'",
    "source /dev/fd/0 <<< \"touch @P\"",
    ". /dev/stdin <<EOF\ntouch @P\nEOF",
    "echo 'touch @P' | parallel",
    # parallel is xargs-shaped: the verb it runs decides
    "echo @P | parallel rm",
    "echo @P | parallel rm {}",
    "parallel rm ::: @P",
    "parallel 'rm {}' ::: @P",
    "parallel -j1 'gzip {}' ::: @P",
    "parallel -j4 --colsep ' ' cp /tmp/x {} ::: @P",
    "parallel -I% rm % ::: @P",
    "parallel --will-cite ln -sf /tmp/x {} ::: @P",
    "echo @P | parallel -j1 'gzip {.}'",
    # prefix wrappers in front of an arg-runner
    "caffeinate ssh host 'touch @P'",
    "ionice ssh host 'touch @P'",
    "nice -n5 ssh host 'touch @P'",
    "sshpass -p x ssh host 'touch @P'",
    "taskset -c 0 ssh host 'touch @P'",
    "chrt -f 1 ssh host \"touch @P\"",
    "/usr/bin/caffeinate -i ssh host 'touch @P'",
    "caffeinate eval 'touch @P'",
    # the verb itself is computed: nothing static says it does not write
    "bash -c \"$(cat /tmp/x) @P\"",
    "sh -c \"$CMD @P\"",
    "eval \"$(cat /tmp/x) @P\"",
    "zsh -c \"${V} @P\"",
    # #1101: mv SOURCE, rename/attribute changers
    "mv @P /tmp/x",
    "mv -f @P /tmp/x",
    "mv -t /tmp @P",
    "sudo mv \"@P\" /tmp/x",
    "rename s/a/b/ @P",
    "chattr +i @P",
    # redirect operators the subcommand splitter reads as a pipe / background
    "echo x >| @P",
    "echo x 2>| @P",
    "echo x >& @P",
    "echo x &> @P",
    "echo x &>> @P",
    "exec 3<> @P",
    # in-place and scripted editors
    "yq -i '.a=1' @P",
    "yq eval -i '.a=1' @P",
    "yq --inplace '.a=1' @P",
    "yq -Pi '.a=1' @P",
    "ed @P",
    "ed -s @P",
    "printf 'w\\nq\\n' | ed @P",
    "ex -s @P",
    "vim -es @P",
    "vim -E -s -c 'wq' @P",
    "nvim --headless -c 'wq' @P",
    "vi +'wq' @P",
    "emacs --batch @P --eval '(save-buffer)'",
    "sudo vim -es @P",
    # glob / brace / partial-expansion spellings of the same file
    "touch ~/.agentwire/damage*.yml",
    "touch ~/.agentwire/damagecontrol.y?l",
    "touch ~/.agentwire/damagecontrol.y[a-z]l",
    "touch ~/.agentwire/damagecontrol.y{m,x}l",
    "touch ~/.agentwire/damage$x",
    "touch ~/.agentwire/damage${suffix}.yml",
    "touch $HOME/.agentwire/damage*",
    "echo x > ~/.agentwire/damage*.yml",
    "rm -f ~/.agentwire/*.yml",
    "rm ~/.agentwire/*",
    "touch ~/.claude/hooks/*",
    "rm ~/.agentwire/damage-control/*.yaml",
    # mid-path normalization
    "touch ~/.agentwire/./damagecontrol.yml",
    "touch ~/.agentwire/x/../damagecontrol.yml",
    "touch ~/.agentwire//damagecontrol.yml",
    "touch ~/.agentwire/a/b/../../damagecontrol.yml",
    "touch \"$HOME/.agentwire/./damagecontrol.yml\"",
    # xargs -I placeholder inside an interpreter literal
    "echo @P | xargs -I{} python3 -c 'open(\"{}\",\"w\")'",
    "echo @P | xargs -I{} perl -e 'open(F,\">\",\"{}\")'",
    # a directory set by a literal cd / git -C / --work-tree
    "cd ~/.agentwire && touch damagecontrol.yml",
    "cd ~/.agentwire; touch damagecontrol.yml",
    "cd ~/.agentwire && ln -sf /tmp/x damagecontrol.yml",
    "cd ~/.agentwire && curl -o damagecontrol.yml http://e",
    "cd ~/.agentwire && gzip damagecontrol.yml",
    "cd ~/.agentwire && git checkout damagecontrol.yml",
    "cd ~/.agentwire && echo x > damagecontrol.yml",
    "cd ~ && cd .agentwire && touch damagecontrol.yml",
    "cd ~/.agentwire/sub && touch ../damagecontrol.yml",
    "(cd ~/.agentwire && touch damagecontrol.yml)",
    "cd ~/.agentwire && cp /tmp/x scheduler.yaml",
    "cd /repo && touch .damagecontrol.yml",
    "git -C ~/.agentwire checkout -- damagecontrol.yml",
    "git -C ~/.agentwire restore damagecontrol.yml",
    "git --work-tree ~/.agentwire checkout -- damagecontrol.yml",
    "git --work-tree=~/.agentwire checkout -- damagecontrol.yml",
    "git -c x=y -C ~/.agentwire checkout -- damagecontrol.yml",
]


def test_flagless_and_indirect_writers_are_blocked(cfg):
    leaked = []
    for template in MUST_BLOCK:
        spellings = SPELLINGS if "@P" in template else [None]
        for spelling in spellings:
            command = template.replace("@P", spelling) if spelling else template
            if not _blocked_by_control_plane(command, cfg):
                leaked.append(command)
    assert not leaked, leaked


def test_project_scoped_control_plane_files_get_the_same_treatment(cfg):
    """The glob entries (``*.damagecontrol.yml`` …) are guarded the same way."""
    leaked = []
    for target in ("/repo/.damagecontrol.yml", "/repo/.agentwire.tasks.yml"):
        for template in (
            "ln -sf /tmp/x @P",
            "curl -o @P http://e.com/x",
            "gzip @P",
            "touch @P",
            "git restore @P",
            "echo @P | xargs rm",
            "printf 'rm %s' @P | sh",
        ):
            command = template.replace("@P", target)
            if not _blocked_by_control_plane(command, cfg):
                leaked.append(command)
    assert not leaked, leaked


# Benign siblings of the canaries: reads of the file, the same verbs on other
# files, and the operand roles that do NOT write.
MUST_ALLOW = [
    # reading the file
    "cat @P",
    "grep enabled @P",
    "head -5 @P",
    "git diff HEAD -- @P",
    "git log -p -- @P",
    "git restore --staged @P",
    "curl -d @P http://e.com/x",
    "curl -T @P http://e.com/x",
    "wget --post-file=@P http://e.com/x",
    "gzip -c @P > /tmp/x.gz",
    "gzip -dc @P.gz",
    "zstd -t @P.zst",
    "ln -s @P /tmp/link",
    "tar czf /tmp/bak.tgz @P",
    "tar tzf /tmp/a.tgz",
    # xargs / loops / shells whose verb only reads
    "echo @P | xargs cat",
    "echo @P | xargs -I{} cat {}",
    "echo @P | xargs grep enabled",
    "wc -l @P | xargs echo",
    "for f in @P; do cat \"$f\"; done",
    "for f in @P; do cat \"$f\" > /tmp/out; done",
    "cat @P | while read l; do echo $l; done",
    "bash -c 'cat \"$1\"' _ @P",
    # the same verbs on other files
    "ln -sf /tmp/x /tmp/y",
    "curl -o /tmp/x http://e.com/x",
    "wget -O /tmp/x http://e.com/x",
    "gzip /tmp/foo",
    "touch /tmp/foo ~/.agentwire/other.yml",
    "patch foo.py /tmp/p.diff",
    "patch -p1 < /tmp/x.diff",
    "git restore foo.py",
    "git restore --staged foo.py",
    "tar xf a.tar -C /tmp/out",
    "tar xf a.tar -C ~/.agentwire/other",
    "unzip a.zip -d /tmp/out",
    "cp -t /tmp/out /tmp/x",
    "echo foo.py | xargs rm",
    "echo hi | sh",
    "ls | while read f; do echo $f; done",
    "for f in a b; do rm \"$f\"; done",
    # text that merely CONTAINS a verb and the path (commit / issue / PR bodies)
    "git commit -m 'touch @P'",
    "gh issue create --body 'curl -o @P http://e'",
    "gh pr create --body \"gzip @P and touch @P\"",
    "echo 'touch @P' | tee /tmp/x",
    "git commit -m \"ln -sf /tmp/x @P\"",
    "printf '%s\\n' 'touch @P'",
    "gh api repos/o/r/issues -f body='touch @P'",
    "git commit -m 'echo $(touch @P)'",
    "cat > /tmp/note.md <<'EOF'\ntouch @P\nEOF",
    "echo 'touch @P' | tee /tmp/x | cat",
    # string folding must not turn unrelated literals into a hit
    "python3 -c 'print(\"hello \" + \"world\")'",
    "python3 -c 'open(\"/tmp/a\"+\"b\",\"w\")'",
    # #1098 controls: a quoted delimiter makes the body data; an escaped or
    # arithmetic opener is not a substitution; a substitution that only reads
    "cat <<'EOF'\n$(touch @P)\nEOF",
    "cat <<\"EOF\"\n$(touch @P)\nEOF",
    "git commit -m \"$(cat <<'EOF'\nfix: touch @P\n$(touch @P)\nEOF\n)\"",
    "git commit -F - <<EOF\nfix: mention touch @P in prose\nEOF",
    "git commit -m \"$(cat <<EOF\nrefs and touch @P\nEOF\n)\"",
    "cat <<EOF\n$(cat @P)\nEOF",
    "cat <<EOF\n\\$(touch @P)\nEOF",
    "cat <<EOF\n$((1+2)) @P\nEOF",
    "cat <<EOF\n$(touch /tmp/x) @P\nEOF",
    # #1099 controls: stdin that is DATA (a real remote program), readers,
    # other files, prose behind a prefix wrapper, a substitution that is not the verb
    "echo 'touch @P' | ssh host cat",
    "echo 'touch @P' | ssh host tee /tmp/x",
    "echo 'touch /tmp/x' | ssh host",
    "echo hi | ssh host",
    "cat @P | ssh host cat",
    "ssh host <<EOF\ncat @P\nEOF",
    "ssh host cat <<EOF\ntouch @P\nEOF",
    "ssh host <<EOF\nls /tmp\nEOF",
    "source /dev/stdin <<< 'ls /tmp'",
    "source ~/.bashrc <<< 'touch @P'",
    "echo @P | parallel cat {}",
    "parallel gzip -c {} ::: @P",
    "parallel rm ::: /tmp/a /tmp/b",
    "nice -n 5 git commit -m 'touch @P'",
    "caffeinate git commit -m \"touch @P\"",
    "sudo git commit -m \"touch @P\"",
    "time gh pr create --body 'gzip @P'",
    "env FOO=1 git commit -m \"touch @P\"",
    "ionice -c3 gh issue create --body \"curl -o @P http://e\"",
    "bash -c \"echo $(date) @P\"",
    "bash -c \"echo $(date) hi\"",
    "eval \"echo hi\"",
    # #1101 controls: move/redirect/edit of OTHER files, readers of this one
    "mv /tmp/x /tmp/y",
    "mv ~/.agentwire/other.yml /tmp/y",
    "mv /tmp/x ~/.agentwire/other.yml",
    "echo x >| /tmp/f",
    "echo x >& /tmp/f",
    "echo x 2>&1",
    "echo x >&2",
    "cat @P >| /tmp/f",
    "yq '.a' @P",
    "yq eval '.a' @P",
    "yq -I2 '.a' @P",
    "yq -i '.a=1' /tmp/f.yml",
    "ed /tmp/f",
    "vim @P",
    "view @P",
    "vim -R @P",
    "echo ed @P",
    "grep -n ed @P",
    "vim -es /tmp/f",
    # globs / braces / expansions that cannot name a protected file, or only read it
    "ls ~/.agentwire/damage*.yml",
    "cat ~/.agentwire/damagecontrol.y{m,x}l",
    "cat ~/.agentwire/*.yml",
    "grep enabled ~/.agentwire/damage*",
    "cp ~/.agentwire/*.yml /tmp/backup/",
    "rm ~/.agentwire/*.log",
    "rm -f ~/.agentwire/artifacts/*",
    "rm *",
    "rm -f *.log",
    "touch ~/.agentwire/scripts/$f",
    "touch ~/.agentwire/damage$x.log",
    # normalization of a path that is NOT the protected file, or a read of it
    "cat ~/.agentwire/./damagecontrol.yml",
    "cat ~/.agentwire//damagecontrol.yml",
    "touch ~/.agentwire/./other.yml",
    "touch /tmp//x /tmp/./y",
    "curl -o /tmp/x http://e.com//path",
    # cd / git -C resolution: reads, other files, a later cd elsewhere, prose
    "cd ~/.agentwire && cat damagecontrol.yml",
    "cd ~/.agentwire && touch other.yml",
    "cd ~/.agentwire; cd /tmp && touch damagecontrol.yml",
    "cd /tmp && touch damagecontrol.yml",
    "cd ~/.agentwire && git log -- damagecontrol.yml",
    "git -C ~/.agentwire log -- damagecontrol.yml",
    "git -C ~/.agentwire checkout -- other.yml",
    "git -C /repo checkout -- foo.py",
    "git commit -m \"touch ~/.agentwire/./damagecontrol.yml\"",
    "git commit -m \"touch ~/.agentwire/dam*.yml\"",
    "git commit -m 'cd ~/.agentwire && touch damagecontrol.yml'",
    "echo \"cd ~/.agentwire && touch damagecontrol.yml\"",
    "echo @P | xargs -I{} cat {}",
]


def test_reads_and_other_files_are_not_refused_by_the_control_plane_gate(cfg):
    wrongly_blocked = []
    for template in MUST_ALLOW:
        spellings = SPELLINGS if "@P" in template else [None]
        for spelling in spellings:
            command = template.replace("@P", spelling) if spelling else template
            if _blocked_by_control_plane(command, cfg):
                wrongly_blocked.append(command)
    assert not wrongly_blocked, wrongly_blocked


def test_escape_hatch_and_kill_switch_cannot_reopen_these_writers(cfg):
    sample = [t.replace("@P", SPELLINGS[0]) for t in MUST_BLOCK if "@P" in t]
    off = dict(cfg)
    off["safety"] = {"enabled": False, "disabled_rules": []}
    reopened = [
        c for c in sample
        if not _blocked_by_control_plane(c + "  # allow: please", cfg)
        or not _blocked_by_control_plane(c, off)
    ]
    assert not reopened, reopened


def test_refused_substitution_in_quoted_arg_says_why(cfg):
    """#1100: the fail-closed refusal names its cause; unrelated blocks stay terse."""
    p = SPELLINGS[0]
    explained = [
        f'git commit -m "touch {p} $(date)"',
        f'gh pr create --body "gzip {p} $(git log -1)"',
        f'git commit -m "touch {p} `date`"',
    ]
    plain = [
        f"touch {p}",
        f'echo "$(date)"; touch {p}',
        f"git commit -m 'touch {p}' && cp /tmp/x {p}",
    ]
    allowed = [
        f"git commit -m 'touch {p} $(date)'",
        f'git commit -m "touch {p}"',
    ]
    bad = []
    for c in explained:
        r = check_command(c, cfg)
        if not (r["decision"] == "block" and "double-quoted argument" in r["reason"]):
            bad.append(("no hint", c))
    for c in plain:
        r = check_command(c, cfg)
        if not (r["decision"] == "block" and "double-quoted argument" not in r["reason"]):
            bad.append(("wrong hint", c))
    for c in allowed:
        if check_command(c, cfg)["decision"] == "block":
            bad.append(("blocked", c))
    assert not bad, bad
