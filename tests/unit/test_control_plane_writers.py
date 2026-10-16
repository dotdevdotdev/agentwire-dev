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
    # the path assembled from adjacent / concatenated literals
    "python3 -c 'open(\"~/.agentwire/damage\"+\"control.yml\",\"w\")'",
    "python3 -c \"open('$HOME/.agentwire/damage' 'control.yml','w')\"",
    "perl -e 'open(F,\">\",\"~/.agentwire/damage\".\"control.yml\")'",
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
    # string folding must not turn unrelated literals into a hit
    "python3 -c 'print(\"hello \" + \"world\")'",
    "python3 -c 'open(\"/tmp/a\"+\"b\",\"w\")'",
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
