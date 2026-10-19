"""Hidden commands: text another program RUNS is judged as a command (batch 3 of the DC residuals).

``sh <<< 'X'``, ``cat <(X)``, ``sed 'e X'``, ``awk 'BEGIN{system("X")}'``,
``tar --to-command=X``, ``git fetch --upload-pack=X``, ``npm exec -c X`` and the
embedded-language interpreters all carry a second command the shell never
parses. The rule is ONE differential: a payload's decision inside any carrier
equals its decision alone. The controls price the other half — benign payloads
and the plain forms of each tool stay allowed.
"""

import shlex

import pytest

from agentwire.safety._core import has_command_position_eval

REPO_RULES = "agentwire/hooks/damage-control/rules"
CTL = "~/.agentwire/damagecontrol.yml"
SAFETY = {"enabled": True, "disabled_rules": [], "unattended_allow": []}


@pytest.fixture(scope="module")
def cfg(bash_hook):
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent.parent
    config = bash_hook.load_config(repo / "agentwire/hooks/damage-control/rules", repo / "agentwire/tooldefs")
    assert not config.get("_parser_unavailable"), "rules failed to load"
    return dict(config, safety=SAFETY)


@pytest.fixture(autouse=True)
def _attended(monkeypatch):
    monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)


def _decide(bash_hook, cfg, command):
    return bash_hook.check_command(command, cfg)["decision"]


# Payloads with no quote characters of their own, so every carrier can embed them verbatim.
BLOCKED_PAYLOADS = [
    "rm -rf /x",
    "git push --force origin main",
    "git reset --hard HEAD~3",
    f"touch {CTL}",
]
BENIGN_PAYLOADS = ["ls -la /tmp", "git status", "echo hello"]

# Each carrier runs {p} (or a quoted {q} form) as a command, whatever the wrapped program is.
CARRIERS = [
    "sh <<< {q}",
    "bash <<'EOF'\n{p}\nEOF",
    "cat <({p})",
    "diff <(ls a) <({p})",
    "echo {q} | sh",
    "cat <<'EOF' | xargs -I{{}} sh -c '{{}}'\n{p}\nEOF",
    "echo {q} | xargs -I{{}} sh -c '{{}}'",
    "echo {q} | ssh host",
    "echo {q} | mosh host",
    "mosh host -- {p}",
    "flock /tmp/l {p}",
    "flock -n /tmp/l {p}",
    "flock /tmp/l -c {q}",
    "nohup flock -w 5 /tmp/l {p}",
    "sudo flock /tmp/l {p}",
    "docker run alpine {p}",
    "docker run --rm -e A=1 alpine {p}",
    "podman exec c {p}",
    "parallel -j4 {p} ::: a b",
    "parallel '{p}' ::: a",
    "parallel \"{p}\" ::: a",
    "parallel --pipe '{p}' ::: a",
    "parallel -a f '{p}; echo {{}}'",
    "docker container exec c {p}",
    "docker compose exec svc {p}",
    "docker-compose run --rm svc {p}",
    "kubectl exec pod -- {p}",
    "sed -n '1e {p}' f",
    "sed -e 's/x/{p2}/e' f",
    "echo x | sed -ne '$e {p}'",
    "awk 'BEGIN{{system(\"{p}\")}}'",
    "awk 'BEGIN{{\"{p}\" | getline x}}'",
    "awk 'BEGIN{{print 1 | \"{p}\"}}'",
    "tar xf a.tar --to-command={q}",
    "tar xf a.tar --to-command {q}",
    "tar cf a.tar x --checkpoint=1 --checkpoint-action=exec={q}",
    "tar cf a.tar -I {q} x",
    "tar cf a.tar --use-compress-program={q} x",
    "git difftool -x {q}",
    "git difftool --extcmd={q}",
    "git fetch --upload-pack={q} origin",
    "git pull --upload-pack {q} origin",
    "git push --receive-pack={q} origin",
    "git clone -u {q} url",
    "git grep -O{q} foo",
    "git filter-branch --tree-filter {q} HEAD",
    "git submodule foreach {q}",
    "git bisect run {p}",
    "git config core.editor {q}",
    "git --no-pager -c user.name=x config --global core.pager {q}",
    "npm exec -c={q}",
    "npx -c {q}",
    "ssh -o ProxyCommand={q} host",
    "scp -S {q} a b:c",
    "sftp -o 'LocalCommand {p}' host",
    "sh -c 'sed -n \"1e {p}\" f'",
    "ssh host 'tar xf a.tar --to-command={q}'",
    "lua -e 'os.execute(\"{p}\")'",
    "luajit -e 'os.execute(\"{p}\")'",
    "Rscript -e 'system(\"{p}\")'",
    "R -e 'system(\"{p}\")'",
    "julia -e 'run(`{p}`)'",
    "groovy -e '\"{p}\".execute()'",
    "osascript -e 'do shell script \"{p}\"'",
    "tclsh <<< 'exec {p}'",
]


def _carrier(template, payload):
    return template.format(p=payload, q=shlex.quote(payload), p2=payload.replace("/", "\\/"))


def test_a_hidden_payload_decides_like_the_payload_alone(bash_hook, cfg):
    bad = []
    for payload in BLOCKED_PAYLOADS:
        plain = _decide(bash_hook, cfg, payload)
        assert plain == "block", (payload, plain)
        for template in CARRIERS:
            wrapped = _decide(bash_hook, cfg, _carrier(template, payload))
            if wrapped != plain:
                bad.append((template, payload, wrapped))
    assert not bad, bad


def test_benign_payloads_add_nothing_to_the_carrier(bash_hook, cfg):
    """A carrier can be ask-tier itself (``git pull``); a benign payload must not move that verdict."""
    bad = []
    for payload in BENIGN_PAYLOADS:
        assert _decide(bash_hook, cfg, payload) == "allow", payload
        for template in CARRIERS:
            if "config" in template and "core." in template:
                continue  # set-form exec-key config asks by rule (#1133) for any non-trivial value
            baseline = _decide(bash_hook, cfg, _carrier(template, "true"))
            command = _carrier(template, payload)
            wrapped = _decide(bash_hook, cfg, command)
            if wrapped != baseline:
                bad.append((command, baseline, wrapped))
    assert not bad, bad


# The plain form of each tool, and the carrier syntax holding ordinary content, are NOT payloads.
PLAIN_FORMS = [
    "sed -n '1p' f", "echo x | sed 's/x/y/g'", "sed 's/e/E/' f", "sed -n '/re/p' f", "sed -i 's/a/b/' f",
    "sed -n 's/x/echo e/p' f", "awk '{print $1}' f", "awk -F: '{print $1 \"|\" $2}' f", "awk 'BEGIN{x=1||0}' f",
    "tar xf a.tar", "tar czf a.tgz dir", "tar cf a.tar -C /tmp x", "tar xf a.tar --checkpoint=100 --checkpoint-action=dot",
    "git fetch origin", "git difftool HEAD~1",
    "git config user.name x", "git config --global core.editor vim", "git config --get core.editor",
    "git clone https://example.com/r.git", "git grep -n foo", "git submodule update --init",
    "npm exec prettier -- --check .", "npx prettier --check .",
    "ssh -o StrictHostKeyChecking=no host ls", "ssh -o ProxyJump=jump host ls", "scp a host:/tmp/b",
    "sftp host", "sftp ~/.agentwire/damagecontrol.yml host:/x",
    "diff <(ls a) <(ls b)", "sh <<< 'echo hi'", "bash <<'EOF'\necho hi\nls\nEOF",
    "echo 'print(1)' | ssh host python3", "echo hi | mosh host", "mosh host",
    "lua -e 'print(1)'", "lua script.lua", "Rscript -e 'print(1)'", "julia -e 'println(1)'",
    "echo 'touch ~/.agentwire/damagecontrol.yml' | jq .",
    "echo x | xargs -I{} echo {}", "cat f | xargs -I{} sh -c 'echo {} done'",
    "tclsh script.tcl",
]


def test_plain_forms_and_ordinary_content_are_not_payloads(bash_hook, cfg):
    bad = [c for c in PLAIN_FORMS if _decide(bash_hook, cfg, c) != "allow"]
    assert not bad, bad


def test_remote_interpreter_stdin_is_judged_as_that_interpreters_program(bash_hook, cfg):
    blocked = [
        f"echo \"open('{CTL}','w')\" | ssh host python3",
        f"echo \"os.execute('touch {CTL}')\" | ssh host lua",
        f"echo \"os.execute('touch {CTL}')\" | ssh host lua -",
        f"ssh host python3 <<'EOF'\nopen('{CTL}','w')\nEOF",
        f"echo \"open('{CTL}','w')\" | mosh host python3",
    ]
    allowed = [
        "echo 'print(1)' | ssh host python3",
        f"echo \"os.execute('touch {CTL}')\" | ssh host lua -e 'print(1)'",  # stdin is data: a program was given
    ]
    bad = [(c, "block") for c in blocked if _decide(bash_hook, cfg, c) != "block"]
    bad += [(c, "allow") for c in allowed if _decide(bash_hook, cfg, c) != "allow"]
    assert not bad, bad


def test_sftp_writes_its_last_operand_like_scp(bash_hook, cfg):
    blocked = [
        f"sftp host:/x {CTL}",
        f"sftp -P 22 host:/x {CTL}",
        f"sftp host <<< 'get /x {CTL}'",
        f"sftp host <<< '!touch {CTL}'",
        "printf 'lcd ~/.agentwire\\nget /x damagecontrol.yml\\n' | sftp host",
    ]
    bad = [c for c in blocked if _decide(bash_hook, cfg, c) != "block"]
    assert not bad, bad


def test_embedded_interpreters_get_the_inline_program_net(bash_hook, cfg):
    names = {
        "lua": "-e", "luajit": "-e", "Rscript": "-e", "R": "-e", "julia": "-e", "groovy": "-e",
        "osascript": "-e",
    }
    bad = []
    for name, flag in names.items():
        for program in (f"open('{CTL}','w')", f"io.open('{CTL}','w')"):
            command = f"{name} {flag} {shlex.quote(program)}"
            if _decide(bash_hook, cfg, command) != "block":
                bad.append(command)
        if _decide(bash_hook, cfg, f"{name} {flag} 'print(1)'") != "allow":
            bad.append(f"{name} benign")
    assert not bad, bad


def test_the_outer_escape_hatch_still_ends_the_walk(bash_hook, cfg):
    """A hidden command is part of the command the user already waved through (kill switch / hatch)."""
    result = bash_hook.check_command("sh <<< 'rm -rf /x' # allow: scratch cleanup", cfg)
    assert result["decision"] == "allow" and result.get("escape")
    off = dict(cfg, safety=dict(SAFETY, enabled=False))
    assert bash_hook.check_command("sed -n '1e rm -rf /x' f", off)["decision"] == "allow"


def test_nesting_is_bounded_not_crashing(bash_hook, cfg):
    command = "rm -rf /x"
    for _ in range(12):
        command = f"sh <<< {shlex.quote(command)}"
    assert _decide(bash_hook, cfg, command) in {"block", "ask"}


# ---- launchers (#14) -------------------------------------------------------

LAUNCHER_DATA = [
    "parallel -j4 grep eval ::: f", "parallel grep eval {} ::: a b", "parallel -j 4 --colsep , grep eval ::: f",
    "flock /tmp/l grep eval f", "flock -w 5 /tmp/l grep eval f", "flock -n /tmp/l grep eval f",
    "docker run img grep eval f", "docker run --rm -e A=1 -v /a:/b img grep eval f",
    "docker exec c grep eval f", "podman run -it img grep eval f",
]
LAUNCHER_EXEC = [
    "parallel -j4 eval ::: f", "parallel 'eval {}' ::: f", "flock /tmp/l eval 'rm -rf /x'",
    "flock /tmp/l -c 'eval x'", "flock -w 5 /tmp/l eval x", "docker run img eval 'rm -rf /x'",
    "docker run --rm img sh -c 'eval x'", "docker exec c eval x", "docker run --entrypoint eval img x",
]


def test_known_launchers_see_the_verb_not_the_word_after_it():
    bad = [c for c in LAUNCHER_DATA if has_command_position_eval(c)]
    bad += [c for c in LAUNCHER_EXEC if not has_command_position_eval(c)]
    assert not bad, bad


def test_container_entrypoint_is_the_launched_program(bash_hook, cfg):
    assert _decide(bash_hook, cfg, "docker run --entrypoint rm alpine -rf /x") == "block"
    assert _decide(bash_hook, cfg, "docker run alpine ls") == _decide(bash_hook, cfg, "docker run alpine true")


# Real audit-log commands the first cut wrongly asked on (data that merely LOOKS like a carrier).
DATA_THAT_LOOKS_LIKE_A_CARRIER = [
    "sed 's/x/My own changes do not touch `resume.ts`, the PDF or `\\/resume`./' f",
    "gh pr view 1 --json body -q .body | sed 's/a/b `c` \\/d e/'",
    "cat > f.md <<'EOF'\nprose: read c <<< eval; bash -c \"$c\" (plain `read c <<< eval; also `mapfile`)\nEOF",
    "agentwire msg send --to x --kind request \"(2) 'echo os.execute(...) | lua', 'lua <<< ...' and 'flock /tmp/l -c \\\"rm\\\"' it's data\"",
    "echo 'sh <<< rm' | cat",
    "sed -n 's/e/E/p' f",
]


def test_text_that_only_resembles_a_carrier_stays_data(bash_hook, cfg):
    bad = [c for c in DATA_THAT_LOOKS_LIKE_A_CARRIER if _decide(bash_hook, cfg, c) != "allow"]
    assert not bad, bad


def test_a_message_body_quoting_carrier_syntax_is_data(bash_hook, cfg):
    """A real audit-log ``agentwire msg send`` whose body quotes ``'lua <<< …'`` and ``flock … -c "…"``."""
    from pathlib import Path

    body = (Path(__file__).resolve().parent.parent / "fixtures" / "dc_msg_body_quoting_carriers.txt").read_text()
    assert _decide(bash_hook, cfg, body) == "allow"


R3_BLOCK = [
    # a quoted <<< before the real one, in every separator / position
    "echo '<<<' ; sh <<< 'rm -rf xx'", "echo '<<<' && sh <<< 'rm -rf xx'", "echo '<<<' || sh <<< 'rm -rf xx'",
    "echo '<<<'\nsh <<< 'rm -rf xx'", "echo '<<<' | cat; sh <<< 'rm -rf xx'", "echo \"$(echo '<<<'; sh <<< 'rm -rf xx')\"",
    "cat <<'EOF'\nbody\nEOF\necho '<<<'; sh <<< 'rm -rf xx'",
    # here-string / stdin inside a -c or ssh payload
    "bash -c 'sh <<< \"rm -rf xx\"'", "sh -c 'sh <<< \"rm -rf xx\"'", "bash -c \"sh <<< 'rm -rf xx'\"",
    "ssh host \"sh <<< 'rm -rf xx'\"", "bash -c 'cat <(rm -rf xx)'",
    # producers beyond echo
    "cat <<< 'rm -rf xx' | sh", "cat <<< 'rm -rf xx' | bash -s", "printf '%s' 'rm -rf xx' | sh", "printf '%s\\n' 'rm -rf xx' | bash",
    # container global options
    "docker compose -f x.yml exec web rm -rf xx", "docker compose -p x run web rm -rf xx",
    "docker --context x compose exec web rm -rf xx", "docker -H host container exec c rm -rf xx",
    "docker-compose -f x.yml exec web rm -rf xx", "oc exec pod -- rm -rf xx", "kubectl -n ns exec pod -- rm -rf xx",
    "oc exec pod -- git push --force origin main", "oc -n ns exec pod -- git reset --hard HEAD~3",
    f"oc exec pod -- touch {CTL}",
    # sed delimiter
    "sed 's_a_rm -rf xx_e' f", "echo a | sed 's|a|rm -rf xx|e'",
]
R3_ALLOW = [
    "echo '<<<'; echo hi", "echo '<<<' ; sh <<< 'echo hi'", "bash -c 'sh <<< \"echo hi\"'",
    "cat <<< 'echo hi' | sh", "printf '%s' 'echo hi' | sh", "docker compose -f x.yml exec web true".replace("exec web true", "ps"),
    "sed 's_a_b_' f", "sed 's_a_b_g' f",
    "sed -i '' 's/if _EVAL_RE.search(command):/if has_it(command):/; s/if _EVAL_RE.search(masked):/if has_it(masked):/' f",
]


def test_round3_evasions_and_gaps_block(bash_hook, cfg):
    bad = [c for c in R3_BLOCK if _decide(bash_hook, cfg, c) != "block"]
    assert not bad, bad


def test_round3_data_stays_allowed(bash_hook, cfg):
    bad = [c for c in R3_ALLOW if _decide(bash_hook, cfg, c) != "allow"]
    assert not bad, bad
