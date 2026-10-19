"""Class-C residuals: shapes no text hook can decide, closed by failing closed (#1065).

A path assembled at run time, an alias made before the hook ran, a hook that
outlives its timeout: none can be read off the command text. What CAN be refused
is the unknowable SHAPE when it sits next to a guarded target. Every canary is
one such shape; every control is the near-miss that must keep working, because
a wrongful ask in the unattended tier is a silent loop.
"""

import os
import threading
import time
from pathlib import Path

import pytest

import agentwire.safety._core as core
from agentwire.safety._core import check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR)
    c["safety"] = {"enabled": True, "disabled_rules": []}
    return c


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A real, resolved HOME holding the control-plane files, so aliases can actually exist."""
    h = Path(os.path.realpath(tmp_path)) / "home"
    (h / ".agentwire").mkdir(parents=True)
    (h / ".claude").mkdir()
    (h / ".agentwire" / "damagecontrol.yml").write_text("x")
    monkeypatch.setenv("HOME", str(h))
    core._protected_inodes.cache_clear() if hasattr(core._protected_inodes, "cache_clear") else None
    return h


def _decisions(cfg, cmds):
    return {c: check_command(c, cfg)["decision"] for c in cmds}


# ---- (a) inline interpreter writing to a run-time path near the control plane --------------------

INLINE_ASKS = [
    "python3 -c \"import os;open(os.path.join(h,'.agentwire','damage'+'control.yml'),'w')\"",
    "python3 -c \"open(d+'/.agentwire/'+chr(100)+'amagecontrol.yml','w')\"",
    "perl -e 'my $d=shift; open(F,\">$d/.claude/settings.json\")'",
    "node -e \"require('fs').writeFileSync(p+'/.claude/settings.json','{}')\"",
    "ruby -e \"File.write(dir + '/.agentwire/x', 'y')\"",
    "python3 -c \"import os;os.system('touch '+d+'/.claude/hooks/x')\"",
    # the sink hides in the receiver, a getattr, a non-literal mode, or a heredoc program
    "python3 -c \"from pathlib import Path;(Path.home()/'.claude'/'settings.json').write_text('{}')\"",
    "python3 -c \"getattr(__import__('os'),'system')('echo x > '+h+'/.claude/settings.json')\"",
    "python3 -c \"open(h+'/.claude/settings.json',m)\"",
    "python3 - <<'EOF'\nimport os\nopen(os.path.expanduser('~')+'/.claude/'+'settings.json','w')\nEOF",
    # the agent-owned dirs are not an exemption once the path climbs out of them
    "python3 -c \"import os;open(os.path.expanduser('~/.claude/projects/x/memory')+'/../../hooks/evil.sh','w')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.claude/projects/x/memory')+'/../../commands/evil.md','w')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.agentwire/wiki')+'/../hooks/damage-control/rules/x.yaml','w')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.agentwire/wiki')+'/../machines.json','w')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.agentwire/wiki')+'/../config.yaml','w')\"",
    "python3 -c \"from pathlib import Path;(Path.home()/'.claude/projects/x/memory/../../hooks/x').write_text('y')\"",
    "python3 -c \"from pathlib import Path;(Path.home()/'.agentwire/wiki'/'..'/'hooks'/'x').write_text('y')\"",
    "python3 -c \"import shutil,os;shutil.copy(s,os.path.expanduser('~/.agentwire/wiki')+'/../hooks/x')\"",
    "python3 -c \"import os;os.system('cp a '+os.path.expanduser('~/.claude/projects/x/memory')+'/../../hooks/x')\"",
    # the exemption is ONE static path: any other spelling of "inside memory/wiki" asks
    "python3 -c \"import os;open(os.path.join(os.path.expanduser('~/.claude/projects/x/memory'),os.pardir,os.pardir,'hooks','e.sh'),'w')\"",
    "python3 -c \"import os;open(os.path.join(os.path.expanduser('~/.claude/projects/x/memory'),os.path.pardir,'e.sh'),'w')\"",
    "python3 -c \"import os;open(os.path.join(os.path.expanduser('~/.claude/projects/p/memory'),d),'w')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.claude/projects/x/memory')+'/'+d,'w')\"",
    "python3 -c \"from pathlib import Path;(Path.home()/'.claude/projects/x/memory'/d).write_text('x')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.claude/projects/x/memory')+'/.\\x2e/.\\x2e/hooks/e.sh','w')\"",
    "python3 -c \"import os;dd='..';open(os.path.join(os.path.expanduser('~/.claude/projects/x/memory'),dd,dd,'hooks','e.sh'),'w')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.claude/projects/x/memory')+'/'+'.'*2+'/hooks/e','w')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.claude/projects/x/memory')+'/'+chr(46)*2+'/hooks/e','w')\"",
    "python3 -c \"import os;os.chdir(os.path.expanduser('~/.claude/projects/x/memory'));os.chdir('..');os.chdir('..');open('hooks/e.sh','w')\"",
    "python3 -c \"import os;M=os.path.expanduser('~/.claude/projects/x/memory');os.chdir(M);os.chdir('..');open('hooks/e.sh','w')\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.agentwire/wiki-evil/x'),'w')\"",
    # more ways to open a path for writing
    "ruby -e \"File.open(h+'/.claude/settings.json','w')\"",
    "ruby -e \"File.new(h+'/.claude/settings.json','w')\"",
    "node -e \"require('fs').createWriteStream(h+'/.claude/settings.json')\"",
    "node -e \"fs.openSync(h+'/.claude/settings.json','w')\"",
    "python3 -c \"import io;io.open(h+'/.claude/settings.json','w')\"",
    "python3 -c \"import codecs;codecs.open(h+'/.claude/settings.json','w')\"",
    "python3 -c \"import os;os.open(h+'/.claude/settings.json',os.O_WRONLY)\"",
    "python3 -c \"import tarfile;tarfile.open(a).extractall(h+'/.claude')\"",
    "python3 -c \"import shutil;shutil.unpack_archive(a,h+'/.claude/hooks')\"",
    "php -r \"file_put_contents(\\$h.'/.claude/settings.json','x');\"",
    "php -r \"fopen(\\$h.'/.claude/settings.json','w');\"",
]
INLINE_ALLOWS = [
    "python3 -c \"import os;open(os.path.join('/tmp','x'),'w')\"",  # no control-plane name
    "python3 -c \"import os;os.system('ls '+os.getcwd())\"",
    "python3 -c \"print(open(p+'/.claude/x').read())\"",  # reads only
    "python3 -c \"print(1)\"; echo .claude",
    "cat > notes.md <<'EOF'\npython3 -c \"open(p+'/.claude/x','w')\"\nEOF",  # a heredoc body is prose
    "cat >> tests/test_env.py <<EOF\nx = 1\nEOF",
    # the control word is nowhere near a sink's path expression
    "python3 -c \"import os;os.makedirs(os.path.join(root,'.claude','x'))\"",
    "python3 -c \"import shutil,sys;shutil.copy(sys.argv[1],sys.argv[2])\" ~/.claude/a ~/.claude/b",
    "python3 -c \"import os;open(os.path.join(os.environ['W'],'p.md'),'w').write('~/.agentwire/wiki')\"",
    "python3 -c \"import json,sys;json.dump(d,open(out,'w'))\" package/settings.json",
    "ruby -e \"File.write(dir+'/x', IO.read('.claude/CLAUDE.md'))\"",
    "python3 -c \"import os;open(os.path.expanduser('~/.claude/projects/x/memory/n.md'),'w')\"",
    "python3 -c \"from pathlib import Path;Path('/home/u/.agentwire/wiki/p.md').write_text('x')\"",
    "python3 -c \"import os;os.open(h+'/.claude/CLAUDE.md',os.O_RDONLY)\"",
    # sink-shaped text quoted as DATA (a test fixture, a message) is not a call
    "python3 - <<'PYEOF'\nFIX = \"open(h+'/.claude/settings.json','w')\"\nopen(out,'w').write(FIX)\nPYEOF",
    "python3 - <<'PYEOF'\np='docs/a.md'\ns=open(p).read()\nopen(p,'w').write(s.replace('.claude','x'))\nPYEOF",
]


def test_inline_interpreter_with_a_computed_path_near_the_control_plane_asks(cfg):
    got = _decisions(cfg, INLINE_ASKS)
    assert all(v == "ask" for v in got.values()), got


def test_inline_interpreter_near_misses_stay_allowed(cfg):
    got = _decisions(cfg, INLINE_ALLOWS)
    assert all(v == "allow" for v in got.values()), got


def test_inline_interpreter_decision_is_the_same_inside_a_wrapper(cfg):
    bad = []
    for cmd in INLINE_ASKS:
        for wrapped in (f"ssh host {cmd}", f"sudo {cmd}", f"bash -c {core.shlex.quote(cmd)}"):
            if check_command(wrapped, cfg)["decision"] != "ask":
                bad.append(wrapped)
    assert not bad, bad


# ---- (b) existing aliases, resolved at hook time (the relative-after-cd cases are new here) --------


def test_existing_aliases_of_the_control_plane_are_judged_as_what_they_resolve_to(cfg, home, tmp_path):
    d = Path(os.path.realpath(tmp_path)) / "aliases"
    d.mkdir()
    (d / "lnk").symlink_to(home / ".agentwire" / "damagecontrol.yml")
    os.link(home / ".agentwire" / "damagecontrol.yml", d / "hl")
    (d / "dir").symlink_to(home / ".agentwire")
    (d / "benign").symlink_to(tmp_path)
    refused = [
        f"echo x > {d}/lnk",
        f"echo x > {d}/hl",
        f"touch {d}/dir/damagecontrol.yml",
        f"cd {d} && echo x > lnk",  # relative word: only a path once the cwd is applied
        f"cd {d} && echo x > hl",
        f"cd {d}/dir && touch damagecontrol.yml",
    ]
    got = _decisions(cfg, refused)
    assert all(v == "block" for v in got.values()), got
    fine = _decisions(cfg, [f"echo x > {d}/benign/f", f"cd {d} && echo x > benign/f", f"cat {d}/lnk"])
    assert all(v == "allow" for v in fine.values()), fine


def test_in_command_links_onto_the_control_plane_are_refused(cfg, home):
    """Pin of behavior that predates this change (#1101/#1139); it guards nothing new."""
    cp = home / ".agentwire" / "damagecontrol.yml"
    refused = [
        f"ln -s {cp} /tmp/lnk && echo x > /tmp/lnk",
        f"ln {cp} /tmp/hl && echo x > /tmp/hl",
        f"ln -s {home}/.agentwire /tmp/dir && touch /tmp/dir/damagecontrol.yml",
        f"ln -sf /tmp/x {cp}",
    ]
    got = _decisions(cfg, refused)
    assert all(v == "block" for v in got.values()), got
    assert check_command("ln -s /tmp/a /tmp/b", cfg)["decision"] == "allow"


# ---- (c) brace expansion past the cap -----------------------------------------------------------


def test_over_cap_brace_expansion_never_allows_a_protected_prefix(cfg, home):
    """Pin of behavior that predates this change; it guards nothing new."""
    big = "{x,y}" * 9  # 512 variants, past the 256 cap
    bad = [
        c
        for c in (
            f"touch {home}/.agentwire/damagecontrol{big}",
            f"touch {home}/.agentwire/{big}",
            f"touch {home}/.claude/hooks/{big}",
            f"touch ~/.agentwire/{big}",
        )
        if check_command(c, cfg)["decision"] == "allow"
    ]
    assert not bad, bad
    assert check_command(f"touch /tmp/{'{x,y}' * 3}", cfg)["decision"] == "allow"


# ---- (d) every hook has a bounded, fail-closed deadline ------------------------------------------


@pytest.fixture
def captured_exit(monkeypatch):
    codes, fired = [], threading.Event()

    def fake_exit(code):
        codes.append(code)
        fired.set()

    monkeypatch.setattr(os, "_exit", fake_exit)
    return codes, fired


def test_watchdog_blocks_a_hook_that_outlives_its_budget_without_sigalrm(monkeypatch, captured_exit):
    codes, fired = captured_exit
    monkeypatch.setattr(core, "HOOK_WATCHDOG_SECONDS", 0.2)
    core.run_hook_guarded(lambda: fired.wait(5))  # a wait that releases the GIL, as a hung stat does
    assert codes[0] == 2, codes


def test_scan_deadline_blocks_a_spinning_hook(monkeypatch, captured_exit):
    codes, _ = captured_exit

    def spin():  # bounded, so a missing deadline fails the assert instead of hanging the suite
        end = time.monotonic() + 3
        while time.monotonic() < end:
            pass

    core.run_hook_guarded(spin, deadline_seconds=0.2)
    assert codes == [2], codes


def test_a_hook_that_finishes_is_not_killed_later(monkeypatch, captured_exit):
    codes, _ = captured_exit
    monkeypatch.setattr(core, "HOOK_WATCHDOG_SECONDS", 0.2)
    core.run_hook_guarded(lambda: None, deadline_seconds=5)
    threading.Event().wait(0.5)
    assert codes == [0], codes


# ---- (e) the residuals the user owns are stated, never scored -------------------------------------


def test_doctor_lists_every_user_owned_residual_with_its_control(capsys):
    from agentwire.doctor_cli import _render_residuals_you_own
    from agentwire.safety.residuals import USER_OWNED_RESIDUALS

    _render_residuals_you_own()
    out = capsys.readouterr().out
    missing = [r for r, _why, control in USER_OWNED_RESIDUALS if r not in out or control not in out]
    assert USER_OWNED_RESIDUALS and not missing, missing
    assert "[!!]" not in out
