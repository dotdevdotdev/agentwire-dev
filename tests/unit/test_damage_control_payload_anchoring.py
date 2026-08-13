"""#915 — a report-back must not be refused because of what it SAYS.

``agentwire msg send --to X --kind done "<text>"`` runs its payload through the
Bash rules, so a message that merely *describes* a blocked operation was itself
blocked. It is not a ``msg send`` bug: any command whose ARGUMENTS discuss a
guarded operation is refused — ``echo``, ``grep`` for a rule's own reason text,
a probe script listing dangerous commands as test data. Reading the rules is
blocked by the rules.

#675 fixed this shape for tooldef-derived rules and for ``git.yaml`` with
``anchored: true`` (match masked command position, never quoted argument
content). This extends the same property to the rest of the bundled set, and
marks the files that must NOT get it.

SCOPE: the payload bug has THREE mechanisms and this fixes ONE of them —
``bashToolPatterns``. The path ladders (mechanism 2) and whitespace-keyed
masking (mechanism 3) are #922, and are asserted here as still-refused ON
PURPOSE so a green run cannot read as "the reported symptom is fixed". See
``TestRemainingPayloadMechanisms``.

RULE SET UNDER TEST: the BUNDLED rules at
``agentwire/hooks/damage-control/rules/*.yaml`` — not ``~/.agentwire/
damage-control/``, which the live hook prefers and which has drifted (#916).
Every assertion here is a claim about what ships.

This is a guard-WEAKENING change, so the weight is on the guard: every anchored
rule carries a companion dangerous form proven to still refuse *by that rule
alone*, and a mutation class proves those assertions go red when the anchoring
decision is wrong. The payload cases are the small half.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
TOOLDEFS_DIR = REPO / "agentwire" / "tooldefs"

REFUSED = {"block", "ask"}
SAFETY = {"enabled": True, "disabled_rules": [], "unattended_allow": []}


def _bundled_rules():
    """Every explicit bashToolPattern, straight off disk."""
    out = []
    for path in sorted(RULES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        for entry in data.get("bashToolPatterns", []) or []:
            if isinstance(entry, dict):
                out.append((path.name, entry))
    return out


BUNDLED = _bundled_rules()
ANCHORED = [(f, e) for f, e in BUNDLED if e.get("anchored")]
UNANCHORED = [(f, e) for f, e in BUNDLED if not e.get("anchored")]


@pytest.fixture(scope="module")
def bundled_config(bash_hook):
    """Bundled rules AND bundled tooldefs — what the real hook loads.

    Loading rules alone is a fixture-shaped blind spot: the tooldef-derived
    ask-rules are ~87 of the 265 patterns the hook actually sees, and omitting
    them makes commands read ``allow`` here that are ``ask`` in reality. That
    matters for this file specifically, because ``ask`` resolves to a BLOCK
    under ``AGENTWIRE_UNATTENDED=1`` — so a payload carrier that is merely
    "ask" is still broken on a scheduler dispatch.
    """
    cfg = bash_hook.load_config(RULES_DIR, TOOLDEFS_DIR)
    assert not cfg.get("_parser_unavailable"), "rules failed to load"
    # `source` is the rules-file stem for hand-written rules and the literal
    # "tooldef" for generated ones, so it separates them reliably — an id prefix
    # does not (a tooldef command with an explicit `id:` yields e.g. `git.push`,
    # not `tooldef.*`).
    hand = [p for p in cfg["bashToolPatterns"] if p.get("source") != "tooldef"]
    # 177 before #924: 8 remote.yaml ssh-twins deleted (redundant with the
    # wrapper-payload rescan) + 1 payloads.yaml rule added (#921).
    assert len(hand) == 170, f"expected 170 hand-written rules, got {len(hand)}"
    cfg["safety"] = dict(SAFETY)
    return cfg


def _solo_config(rule):
    """A config holding exactly one rule — no other rule can take the credit."""
    return {
        "bashToolPatterns": [rule],
        "zeroAccessPaths": [],
        "readOnlyPaths": [],
        "noDeletePaths": [],
        "allowedPaths": [],
        "safety": dict(SAFETY),
    }


# ---------------------------------------------------------------------------
# Anchoring is a FILE-WIDE property gated on a shape test
# ---------------------------------------------------------------------------
#
# ``anchored`` swaps the haystack for masked_subcommands(), which blanks EVERY
# fully-quoted token containing whitespace regardless of position. That is
# lossless for a command-prefix rule and FATAL for a WRAPPER rule whose inner
# command arrives as a quoted argument: ``_SHELL_NAMES`` (_core.py) rescans
# ``sh -c "…"`` payloads, but ssh and the SQL/interpreter clients are not in it.
#
# It is not a rule-level property either. Anchored ``core.sudo-rm`` still blocks
# ``sudo rm /etc/hosts`` but loses ``ssh prod "sudo rm -rf /var/lib"`` — and its
# twin ``remote.ssh-remote-sudo-rm``, which exists to cover the wrapped form, is
# lost in the same change, taking that form from double-covered to UNCOVERED.
#
# So the unit is the FILE, following the git.yaml precedent (14 rules, 14
# anchored, the only anchored file before this): a file may be anchored when
# every rule in it is command-prefix shaped AND the tool takes no inner-command
# payload. The rules that fail that test were MOVED into payloads.yaml rather
# than flagged in place, so there is no per-rule skip-list to keep in sync.

UNANCHORED_FILES = {"payloads.yaml", "remote.yaml"}


class TestAnchoringIsFileWide:
    @pytest.mark.parametrize(
        "filename,entry", BUNDLED,
        ids=[f"{f}:{e.get('pattern', '')[:44]}" for f, e in BUNDLED],
    )
    def test_every_rule_declares_anchored(self, filename, entry):
        # The matcher default is unanchored (fail-safe), so a rule that forgets
        # the key silently reintroduces #915. Force the author to choose.
        assert "anchored" in entry, (
            f"{filename}: rule {entry.get('pattern')!r} must declare "
            f"`anchored:` — see #915."
        )
        assert isinstance(entry["anchored"], bool)

    def test_no_file_mixes_anchored_and_unanchored(self):
        """A mixed file is a per-rule skip-list wearing a filename."""
        mixed = {}
        for filename, entry in BUNDLED:
            mixed.setdefault(filename, set()).add(entry["anchored"])
        offenders = {f: v for f, v in mixed.items() if len(v) > 1}
        assert not offenders, (
            f"these files mix anchored and unanchored rules: {sorted(offenders)}. "
            f"Move the wrapper rules to payloads.yaml instead."
        )

    def test_unanchored_files_are_exactly_the_wrapper_files(self):
        actual = {f for f, e in BUNDLED if not e["anchored"]}
        assert actual == UNANCHORED_FILES, (
            f"unanchored file set drifted: unexpected={sorted(actual - UNANCHORED_FILES)} "
            f"missing={sorted(UNANCHORED_FILES - actual)}"
        )

    def test_all_of_remote_yaml_is_unanchored(self):
        """remote.yaml is 100% wrapper rules — anchoring any of it is a hole."""
        remote = [e for f, e in BUNDLED if f == "remote.yaml"]
        # 12 before #924's rescan made the local rules apply over ssh; what
        # remains is the ssh-ONLY surface (reboot/shutdown/service-stop) plus
        # the deliberately-stricter docker rm -f twin.
        assert len(remote) == 4
        assert all(e["anchored"] is False for e in remote)

    def test_payloads_rules_pin_their_ids(self):
        """The move must not churn ids that safety.disabled_rules may name."""
        payloads = [e for f, e in BUNDLED if f == "payloads.yaml"]
        assert len(payloads) == 16  # +1: git.config-exec-key (#921)
        assert all(e.get("id") for e in payloads), (
            "every rule moved into payloads.yaml needs an explicit `id:` pinned "
            "to the id it had in its old file — otherwise the id is re-derived "
            "from the new filename and any config naming it breaks."
        )
        # ids are pinned to the ORIGINAL file, which is the point
        assert {e["id"].split(".")[0] for e in payloads} == {
            "core", "databases", "db", "containers", "git",
        }


# ---------------------------------------------------------------------------
# Companion dangerous form, one per ANCHORED rule — the large half
# ---------------------------------------------------------------------------
#
# Each entry is a real invocation that its rule must still refuse. Proven two
# ways: against a SOLO config holding only that rule (so no other rule can take
# the credit — the failure mode of the #675 test, which stayed green through 13
# regressions because its fixture held one tooldef-shaped rule), and against
# the full bundled set.

DANGEROUS_SAMPLE = {
    '\\btmux\\s+kill-server\\b': 'tmux kill-server',
    '\\btmux\\s+kill-session\\s+.*\\bagentwire': 'tmux kill-session -t agentwire-main',
    '\\btmux\\s+kill-session\\s+.*-a\\b': 'tmux kill-session -t main -a',
    '\\bagentwire\\s+destroy\\b': 'agentwire destroy',
    '\\bagentwire\\s+.*--force.*remove\\b': 'agentwire worktree --force --remove old',
    '\\brm\\s+(?:[^;&|]*\\s)?-(?:[a-zA-Z]*[rRf][a-zA-Z]*|-recursive|-force)\\b'
    '[^;&|]*\\s(?:(?:~|\\$HOME|/Users/[^/\\s]+|/home/[^/\\s]+)/)?'
    '\\.agentwire/?(?=\\s|$|[;&|])': 'rm -r ~/.agentwire',
    '\\baws\\s+s3\\s+rm\\s+.*--recursive': 'aws s3 rm s3://bucket/data --recursive',
    '\\baws\\s+s3\\s+rb\\s+.*--force': 'aws s3 rb s3://bucket --force',
    '\\baws\\s+ec2\\s+terminate-instances\\b': 'aws ec2 terminate-instances',
    '\\baws\\s+rds\\s+delete-db-instance\\b': 'aws rds delete-db-instance',
    '\\baws\\s+cloudformation\\s+delete-stack\\b': 'aws cloudformation delete-stack',
    '\\baws\\s+dynamodb\\s+delete-table\\b': 'aws dynamodb delete-table',
    '\\baws\\s+eks\\s+delete-cluster\\b': 'aws eks delete-cluster',
    '\\baws\\s+lambda\\s+delete-function\\b': 'aws lambda delete-function',
    '\\baws\\s+iam\\s+delete-role\\b': 'aws iam delete-role',
    '\\baws\\s+iam\\s+delete-user\\b': 'aws iam delete-user',
    '\\baws\\s+cloudformation\\s+deploy\\b': 'aws cloudformation deploy',
    '\\baws\\s+lambda\\s+update-function-code\\b': 'aws lambda update-function-code',
    '\\baws\\s+ecs\\s+update-service\\b': 'aws ecs update-service',
    '\\bvercel\\s+remove\\s+.*--yes': 'vercel remove my-site --yes',
    '\\bvercel\\s+projects\\s+rm\\b': 'vercel projects rm',
    '\\bvercel\\s+env\\s+rm\\s+.*--yes': 'vercel env rm API_KEY production --yes',
    '\\bnetlify\\s+sites:delete\\b': 'netlify sites:delete',
    '\\bnetlify\\s+functions:delete\\b': 'netlify functions:delete',
    '\\bwrangler\\s+delete\\b': 'wrangler delete',
    '\\bwrangler\\s+r2\\s+bucket\\s+delete\\b': 'wrangler r2 bucket delete',
    '\\bwrangler\\s+kv:namespace\\s+delete\\b': 'wrangler kv:namespace delete',
    '\\bwrangler\\s+d1\\s+delete\\b': 'wrangler d1 delete',
    '\\bwrangler\\s+queues\\s+delete\\b': 'wrangler queues delete',
    '\\bheroku\\s+apps:destroy\\b': 'heroku apps:destroy',
    '\\bheroku\\s+pg:reset\\b': 'heroku pg:reset',
    '\\bfly\\s+apps\\s+destroy\\b': 'fly apps destroy',
    '\\bfly\\s+destroy\\b': 'fly destroy',
    '\\bdoctl\\s+compute\\s+droplet\\s+delete\\b': 'doctl compute droplet delete',
    '\\bdoctl\\s+databases\\s+delete\\b': 'doctl databases delete',
    '\\bsupabase\\s+db\\s+reset\\b': 'supabase db reset',
    '\\bgh\\s+repo\\s+delete\\b': 'gh repo delete',
    '\\bnpm\\s+unpublish\\b': 'npm unpublish',
    '\\bvercel\\s+deploy\\b': 'vercel deploy',
    '\\bvercel\\s+(-[^\\s]*\\s+)*--prod\\b': 'vercel --prod',
    '\\bnetlify\\s+deploy\\b': 'netlify deploy',
    '\\b(fly|flyctl)\\s+deploy\\b': 'fly deploy',
    '\\bwrangler\\s+(deploy|publish)\\b': 'wrangler deploy',
    '\\brailway\\s+(up|deploy)\\b': 'railway up',
    '\\brender\\s+deploys?\\s+create\\b': 'render deploys create --service-id srv-1',
    '\\bsupabase\\s+functions\\s+deploy\\b': 'supabase functions deploy',
    '\\bdocker\\s+system\\s+prune\\s+.*-a': 'docker system prune -a',
    '\\bdocker\\s+rmi\\s+.*-f': 'docker rmi -f myimage:latest',
    '\\bdocker\\s+volume\\s+rm\\b': 'docker volume rm',
    '\\bdocker\\s+volume\\s+prune\\b': 'docker volume prune',
    '\\bkubectl\\s+delete\\s+namespace\\b': 'kubectl delete namespace',
    '\\bkubectl\\s+delete\\s+all\\s+--all': 'kubectl delete all --all',
    '\\bkubectl\\s+delete\\s+.*--all\\s+--all-namespaces': 'kubectl delete pods --all --all-namespaces',
    '\\bhelm\\s+uninstall\\b': 'helm uninstall',
    '\\bdocker\\s+(compose\\s+)?push\\b': 'docker push myimage:latest',
    '\\bkubectl\\s+delete\\b': 'kubectl delete',
    '\\brm\\s+(-[^\\s]*)*-[rRf]': 'rm -rf /tmp/build',
    '\\brm\\s+-[rRf]': 'rm -r /tmp/build',
    '\\brm\\s+--recursive': 'rm --recursive',
    '\\brm\\s+--force': 'rm --force',
    '\\bsudo\\s+rm\\b': 'sudo rm',
    '\\brmdir\\b': 'rmdir',
    '\\brm\\s+[^-]': 'rm notes.txt',
    '(?:^|[;&|])\\s*trash\\s+': 'trash notes.txt',
    '\\bfind\\b.*\\s-delete\\b': "find /tmp -name '*.log' -delete",
    '\\bfind\\b.*-exec\\s+rm\\b': "find /tmp -name '*.log' -exec rm {} +",
    '\\bchmod\\s+(-[^\\s]+\\s+)*777\\b': 'chmod 777 /srv/app',
    '\\bchmod\\s+-[Rr].*777': 'chmod -R 777 /srv/app',
    '\\bchown\\s+-[Rr].*\\broot\\b': 'chown -R root /srv/app',
    '\\bmkfs\\.': 'mkfs.ext4 /dev/disk2',
    '\\bdd\\s+.*of=/dev/': 'dd if=/dev/zero of=/dev/disk2',
    '\\bkill\\s+-9\\s+-1\\b': 'kill -9 -1',
    '\\bkillall\\s+-9\\b': 'killall -9',
    '\\bpkill\\s+-9\\b': 'pkill -9',
    '\\bhistory\\s+-c\\b': 'history -c',
    '\\bredis-cli\\s+FLUSHALL': 'redis-cli FLUSHALL',
    '\\bredis-cli\\s+FLUSHDB': 'redis-cli FLUSHDB',
    '\\bdropdb\\b': 'dropdb',
    '\\bmysqladmin\\s+drop\\b': 'mysqladmin drop',
    '\\bprisma\\s+migrate\\s+reset\\b': 'prisma migrate reset',
    '\\bflyway\\s+clean\\b': 'flyway clean',
    '\\bprisma\\s+migrate\\s+(deploy|dev)\\b': 'prisma migrate deploy',
    '\\bprisma\\s+db\\s+push\\b': 'prisma db push',
    '\\bsupabase\\s+db\\s+push\\b': 'supabase db push',
    '\\bsupabase\\s+migration\\s+up\\b': 'supabase migration up',
    '\\balembic\\s+(upgrade|downgrade)\\b': 'alembic upgrade head',
    '\\bmanage\\.py\\s+migrate\\b': 'python manage.py migrate',
    '\\b(rails|rake)\\s+db:migrate\\b': 'rails db:migrate',
    '\\bknex\\s+migrate:(latest|up|down|rollback)\\b': 'knex migrate:latest',
    '\\bsequelize\\s+db:migrate\\b': 'sequelize db:migrate',
    '\\bflyway\\s+migrate\\b': 'flyway migrate',
    '\\bliquibase\\s+update\\b': 'liquibase update',
    '\\bfirebase\\s+projects:delete\\b': 'firebase projects:delete',
    '\\bfirebase\\s+firestore:delete\\s+.*--all-collections': 'firebase firestore:delete --all-collections',
    '\\bfirebase\\s+database:remove\\b': 'firebase database:remove',
    '\\bfirebase\\s+hosting:disable\\b': 'firebase hosting:disable',
    '\\bfirebase\\s+functions:delete\\b': 'firebase functions:delete',
    '\\bgcloud\\s+projects\\s+delete\\b': 'gcloud projects delete',
    '\\bgcloud\\s+compute\\s+instances\\s+delete\\b': 'gcloud compute instances delete',
    '\\bgcloud\\s+sql\\s+instances\\s+delete\\b': 'gcloud sql instances delete',
    '\\bgcloud\\s+container\\s+clusters\\s+delete\\b': 'gcloud container clusters delete',
    '\\bgcloud\\s+storage\\s+rm\\s+.*-r': 'gcloud storage rm -r gs://bucket/data',
    '\\bgcloud\\s+functions\\s+delete\\b': 'gcloud functions delete',
    '\\bgcloud\\s+iam\\s+service-accounts\\s+delete\\b': 'gcloud iam service-accounts delete',
    '\\bgcloud\\s+run\\s+deploy\\b': 'gcloud run deploy',
    '\\bgcloud\\s+app\\s+deploy\\b': 'gcloud app deploy',
    '\\bgit\\s+reset\\s+--hard\\b': 'git reset --hard',
    '\\bgit\\s+clean\\s+(-[^\\s]*)*-[fd]': 'git clean -fd',
    '\\bgit\\s+push\\s+.*--force(?!-with-lease)': 'git push origin --force',
    '\\bgit\\s+push\\s+(-[^\\s]*)*-f\\b': 'git push -f origin main',
    '\\bgit\\s+stash\\s+clear\\b': 'git stash clear',
    '\\bgit\\s+reflog\\s+expire\\b': 'git reflog expire',
    '\\bgit\\s+gc\\s+.*--prune=now': 'git gc --prune=now',
    '\\bgit\\s+filter-branch\\b': 'git filter-branch',
    '\\bgit\\s+checkout\\s+--\\s*\\.': 'git checkout -- .',
    '\\bgit\\s+restore\\s+\\.': 'git restore .',
    '\\bgit\\s+stash\\s+drop\\b': 'git stash drop',
    '\\bgit\\s+branch\\s+(-[^\\s]*)*-D': 'git branch -D feature',
    '\\bgit\\s+push\\s+\\S+\\s+--delete\\b': 'git push origin --delete feature',
    '\\bgit\\s+push\\s+\\S+\\s+:\\S+': 'git push origin :feature',
    '\\bgws\\s+gmail\\s+users\\.messages\\.delete\\b': 'gws gmail users.messages.delete',
    '\\bgws\\s+gmail\\s+users\\.messages\\.batchDelete\\b': 'gws gmail users.messages.batchDelete',
    '\\bgws\\s+drive\\s+files\\.delete\\b': 'gws drive files.delete',
    '\\bgws\\s+drive\\s+files\\.emptyTrash\\b': 'gws drive files.emptyTrash',
    '\\bgws\\s+calendar\\s+calendars\\.delete\\b': 'gws calendar calendars.delete',
    '\\bgws\\s+admin\\s+users\\.delete\\b': 'gws admin users.delete',
    '\\bgws\\s+admin\\s+users\\.makeAdmin\\b': 'gws admin users.makeAdmin',
    '\\bterraform\\s+destroy\\b': 'terraform destroy',
    '\\bpulumi\\s+destroy\\b': 'pulumi destroy',
    '\\bserverless\\s+remove\\b': 'serverless remove',
    '\\bsls\\s+remove\\b': 'sls remove',
    '\\bsam\\s+delete\\b': 'sam delete',
    '\\bpulumi\\s+up\\b': 'pulumi up',
    '\\b(serverless|sls)\\s+deploy\\b': 'serverless deploy',
    '\\bsam\\s+deploy\\b': 'sam deploy',
    '\\bcdk\\s+deploy\\b': 'cdk deploy',
    '\\bansible-playbook\\b': 'ansible-playbook',
    '\\bagentwire\\s+email\\b': 'agentwire email',
    '\\bagentwire\\s+quo\\b': 'agentwire quo',
    '\\btwilio\\s+api[:\\w.]*messages[:\\w.]*create\\b': 'twilio api:core:messages:create --to +15551234567 --body hi',
    '\\baws\\s+ses(v2)?\\s+send-email\\b': 'aws ses send-email --to a@b.c',
    '\\baws\\s+sns\\s+publish\\b': 'aws sns publish',
    '\\bsendmail\\b': 'sendmail',
    '\\b(mail|mailx)\\s+(-[^\\s]*\\s+)*-s\\b': 'mail -s subject user@example.com',
    '\\bcargo\\s+publish\\b': 'cargo publish',
    '\\bpoetry\\s+publish\\b': 'poetry publish',
    '\\btwine\\s+upload\\b': 'twine upload',
    '\\b(pnpm|yarn)\\s+publish\\b': 'pnpm publish',
    '\\bgem\\s+push\\b': 'gem push',
    '\\bmvn\\s+(deploy|.*\\bdeploy:deploy)\\b': 'mvn deploy',
}


class TestEveryAnchoredRuleStillRefusesItsDangerousForm:
    def test_every_anchored_rule_has_a_companion_sample(self):
        missing = [
            e["pattern"] for _, e in ANCHORED if e["pattern"] not in DANGEROUS_SAMPLE
        ]
        assert not missing, (
            "anchored rules with no companion dangerous-form test — every "
            f"anchoring decision needs one (#915): {missing}"
        )

    @pytest.mark.parametrize(
        "filename,entry", ANCHORED,
        ids=[f"{f}:{e['pattern'][:44]}" for f, e in ANCHORED],
    )
    def test_rule_alone_refuses_its_sample(self, bash_hook, filename, entry):
        """Solo config: no other rule can take the credit for the refusal."""
        sample = DANGEROUS_SAMPLE[entry["pattern"]]
        result = bash_hook.check_command(sample, _solo_config(entry))
        assert result["decision"] in REFUSED, (
            f"{filename}: anchoring {entry['pattern']!r} lets {sample!r} "
            f"through — the rule no longer matches its own dangerous form"
        )

    @pytest.mark.parametrize("sample", sorted(set(DANGEROUS_SAMPLE.values())))
    def test_full_ruleset_refuses_sample(self, bash_hook, bundled_config, sample):
        result = bash_hook.check_command(sample, bundled_config)
        assert result["decision"] in REFUSED, (
            f"{sample!r} was ALLOWED by the full bundled rule set"
        )


# Anchoring normalizes quoting/escaping/indirection of the COMMAND, so these
# spellings must not slip past either.
OBFUSCATED = [
    'r"m" -rf /tmp/build',
    '"rm" -rf /tmp/build',
    "R=rm; $R -rf /tmp/build",
    'bash -c "rm -rf /tmp/build"',
    'sh -c "kubectl delete namespace prod"',
    "git status && rm -rf /tmp/build",
    "echo ok; terraform destroy",
    "true | helm uninstall release",
    't"e"rraform destroy',
    # a quoted argument WITH spaces beside a real dangerous command: the
    # argument is masked, the command still matches
    'rm -rf "/tmp/some dir/build"',
    'git commit -m "wip" && rm -rf /tmp/build',
]


class TestObfuscatedDangerousCommandsStillRefused:
    @pytest.mark.parametrize("command", OBFUSCATED)
    def test_refused(self, bash_hook, bundled_config, command):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] in REFUSED, (
            f"{command!r} was ALLOWED — quoting/indirection defeated the guard"
        )


# ---------------------------------------------------------------------------
# remote.yaml — previously zero coverage anywhere in the suite
# ---------------------------------------------------------------------------

# Masking only blanks a fully-quoted token that CONTAINS WHITESPACE, so a
# one-word payload (``ssh prod "reboot"``) or a space-free one
# (``--eval "db.dropDatabase()"``) survives anchoring by accident. Splitting the
# corpus on that line keeps the mutation class honest: MASKED_* forms are the
# ones anchoring actually destroys, SURVIVES_* are covered but prove nothing
# about the anchoring decision, so they are asserted and excluded from mutation.

SSH_WRAPPED_MASKED = [
    'ssh prod "sudo rm -rf /var/lib"',
    'ssh prod "rm -rf /srv/data"',
    'ssh prod "mkfs.ext4 /dev/sda1"',
    'ssh prod "dd if=/dev/zero of=/dev/sda"',
    'ssh prod "dropdb production"',
    'ssh prod "mysqladmin drop production"',
    'ssh prod "redis-cli FLUSHALL"',
    'ssh prod "docker system prune -a"',
    'ssh prod "docker rm -f web"',
    'ssh prod "reboot now"',
    'ssh prod "shutdown -h now"',
    'ssh prod "systemctl stop nginx"',
]

SSH_WRAPPED_SURVIVES = [
    'ssh prod "reboot"',              # one word — not masked
    "ssh prod sudo rm -rf /var/lib",  # unquoted — not masked
    "ssh prod dropdb production",
]


class TestSshWrappedCommandsStillRefused:
    """remote.yaml's OWN 12 rules — its intentional ssh coverage — still hold.

    SCOPE WARNING: this corpus is exactly those 12 forms. It says nothing about
    ssh-wrapped commands generally, ~125 of which this PR demotes to allowed —
    see TestSshWrappedCoverageReduction and #924. remote.yaml previously had no
    coverage in the suite at all, which is why these 12 are asserted here; that
    is not the same as ssh being handled.
    """

    @pytest.mark.parametrize("command", SSH_WRAPPED_MASKED + SSH_WRAPPED_SURVIVES)
    def test_refused(self, bash_hook, bundled_config, command):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] in REFUSED, (
            f"{command!r} was ALLOWED — the remote half went unmatched"
        )


CLIENT_WRAPPED_MASKED = [
    'psql -c "DROP TABLE users"',
    'psql -c "DROP DATABASE production"',
    'psql -c "TRUNCATE TABLE users"',
    'psql -c "DELETE FROM users;"',
    'psql -h db -c "INSERT INTO users VALUES (1)"',
    'mysql -e "DROP DATABASE production"',
    'mysql -e "UPDATE users SET admin = 1"',
    'mongosh --eval "db.users.deleteMany({ })"',
    "perl -e 'unlink \"/srv/data\"'",
]

CLIENT_WRAPPED_SURVIVES = [
    # Covered by _EXEC_SURFACES now (`python3 -c` is a table entry), so it
    # holds even under blanket anchoring and the mutation below is INERT
    # for it. That is a genuine strengthening, not an inconvenience: it no
    # longer depends on payloads.yaml staying unanchored. Asserted as
    # refused above, excluded from mutation so that class stays honest.
    'python3 -c "import shutil; shutil.rmtree(\'/srv\')"',
    'mongosh --eval "db.dropDatabase()"',        # no whitespace — not masked
    'mongosh --eval "db.users.deleteMany({})"',
]


class TestInterpreterAndSqlPayloadsStillRefused:
    @pytest.mark.parametrize(
        "command", CLIENT_WRAPPED_MASKED + CLIENT_WRAPPED_SURVIVES
    )
    def test_refused(self, bash_hook, bundled_config, command):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] in REFUSED, (
            f"{command!r} was ALLOWED — the quoted payload went unmatched"
        )


class TestGenericRmBackstopSurvivesAnchoring:
    """#913 interaction: anchoring must not narrow the generic rm backstop.

    A global option before the subcommand bypasses the specific rule
    (``docker --context prod volume rm x`` misses ``containers.docker-volume-rm``
    — that is #913), leaving only the generic ``core.rm-file-deletion`` rule
    holding it. This PR anchors that rule, so the question is whether the
    backstop survives.

    It does, and the reason is precise: masking blanks a quoted token only when
    it CONTAINS WHITESPACE. These commands have no such token, so the masked
    subcommand is byte-identical to the raw command and the rule matches through
    both haystacks. The narrowing this PR applies is confined to quoted argument
    CONTENT — which is the whole point.
    """

    GLOBAL_OPTION_BYPASSED = [
        "docker --context prod volume rm pgdata",
        "aws --profile prod s3 rm s3://bucket --recursive",
        "docker --context prod volume rm 'my data'",
    ]

    @pytest.mark.parametrize("command", GLOBAL_OPTION_BYPASSED)
    def test_still_blocked_by_the_generic_rule(
        self, bash_hook, bundled_config, command
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] in REFUSED, (
            f"{command!r} lost its last backstop — the specific rule is "
            f"global-option-bypassed (#913) and the generic rm rule no longer "
            f"reaches it"
        )

    def test_masked_form_is_identical_when_nothing_is_quoted(self, bash_hook):
        """The mechanism behind the assertion above, asserted directly."""
        command = "docker --context prod volume rm pgdata"
        assert bash_hook.masked_subcommands(command) == [command]


class TestComposedWithGitNormalization:
    """The cross-PR row neither #913 nor #915 could assert alone.

    #918 added ``git_normalized_haystacks`` — additive, derived from the MASKED
    tokens, fed to BOTH routings. That last property is what makes it compose
    with anchoring: an anchored ``git.yaml`` rule is matched against masked
    subcommands, and the normalized haystack is built from those same tokens, so
    stripping ``-C <path>`` exposes the subcommand to a rule that would
    otherwise never see it.

    Before both landed, ``git -C /repo push --force`` was ALLOW: #913's bypass
    hid it from ``\\bgit\\s+push``, and anchoring alone does not help because
    ``-C /repo`` stays inline in the masked subcommand.

    The rule ID is asserted, not just the verdict — a block from the generic
    deletion rule or from an unrelated ask rule would satisfy a verdict-only
    assertion while the git rule stayed bypassed.
    """

    FORCE = "--" + "force"

    def test_forced_push_behind_dash_c_blocks_via_the_git_rule(
        self, bash_hook, bundled_config
    ):
        result = bash_hook.check_command(
            f"git -C /repo push {self.FORCE} origin main", bundled_config
        )
        assert result["decision"] == "block", (
            f"the cross-PR case is {result['decision']} — #913's bypass is open "
            f"again, or normalization stopped reaching anchored rules"
        )
        assert result["id"] == "git.git-push-force-use-force-with-lease", (
            f"blocked, but by {result['id']!r} rather than the git rule — the "
            f"git rule is still bypassed and something else took the credit"
        )

    @pytest.mark.parametrize(
        "command,rule_id",
        [
            ("git -C /repo reset --hard HEAD~3", "git.git-reset-hard-use-soft-or-stash"),
            ("git -C /repo clean -fd", "git.git-clean-with-force-directory-flags"),
            ("git -C /repo stash clear", "git.git-stash-clear-deletes-all-stashes"),
            ("git -C /repo filter-branch", "git.git-filter-branch-rewrites-entire-history"),
        ],
    )
    def test_other_anchored_git_rules_also_reached(
        self, bash_hook, bundled_config, command, rule_id
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "block"
        assert result["id"] == rule_id

    def test_safe_form_behind_dash_c_stays_ask_not_block(
        self, bash_hook, bundled_config
    ):
        """The two-sided expectation: normalization must not rewrite the command
        before the ``(?!-with-lease)`` lookahead sees it. A block here would
        catch the SAFE form and train everyone toward the plain flag."""
        result = bash_hook.check_command(
            "git -C /repo push --force-with-lease origin main", bundled_config
        )
        assert result["decision"] == "ask", (
            f"--force-with-lease behind -C decided {result['decision']}; must be "
            f"ask — not allow (bypass survives), not block (lookahead defeated)"
        )

    def test_payload_prose_survives_normalization(self, bash_hook, bundled_config):
        """#918 derives its haystack from the MASKED tokens, so quoted argument
        text cannot become matchable — #915's fix is not undone by it."""
        for body in (
            "I did not rm the file",
            "git reset --hard was refused by damage-control",
        ):
            cmd = f'agentwire msg send --to orch --kind done "{body}"'
            assert bash_hook.check_command(cmd, bundled_config)["decision"] == "allow"


class TestComposedWithPathScopedGrants:
    """Composition with #917's path-scoped `unattended_allow` grants.

    #917 evaluates a scope only for a rule that MATCHED and resolved to ``ask``
    under ``AGENTWIRE_UNATTENDED=1``. This PR changes WHICH rules match. The
    hazard is therefore specific: a rule that stops matching because of
    anchoring never reaches grant evaluation at all, so the scope check silently
    does not run for it — and the command lands on ``allow`` for the *absence*
    of a rule rather than for a grant anyone authored.

    THE SETS INTERSECT AT EXACTLY ONE RULE, measured rather than assumed.
    ``DEFAULT_UNATTENDED_ALLOW`` names six ids; five are tooldef-derived
    (``git.add``, ``git.add-u``, ``git.commit``, ``git.push``, ``gh.pr-create``)
    and were already anchored by #675, so this PR does not touch them. The sixth,
    ``outbound.agentwire-email``, lives in ``outbound.yaml`` and IS newly
    anchored here. That one row is the whole intersection.
    """

    GRANTED_HAND_WRITTEN_RULE = "outbound.agentwire-email"

    def test_the_intersection_is_exactly_one_rule(self, bash_hook, bundled_config):
        """Pin the premise — if a future grant names another hand-written rule,
        this composition needs re-measuring rather than assuming."""
        granted = {
            e.get("id") if isinstance(e, dict) else e
            for e in bash_hook.DEFAULT_UNATTENDED_ALLOW
        }
        hand_ids = {
            p.get("id") for p in bundled_config["bashToolPatterns"]
            if p.get("source") != "tooldef"
        }
        assert granted & hand_ids == {self.GRANTED_HAND_WRITTEN_RULE}, (
            f"the #915/#917 intersection changed: {sorted(granted & hand_ids)}. "
            f"Re-measure the composition before trusting this class."
        )

    def test_granted_rule_still_matches_so_scope_evaluation_is_reached(
        self, bash_hook, bundled_config
    ):
        """The load-bearing assertion. Anchoring must not stop the granted rule
        matching, or #917's scope check never runs for it."""
        result = bash_hook.check_command(
            "agentwire email --to a@b.c --subject hi --body hi", bundled_config
        )
        assert result["decision"] == "ask", (
            f"the granted rule resolved {result['decision']}, not ask — #917 "
            f"evaluates a scope only on an ask, so the grant is now bypassed "
            f"in whichever direction this went"
        )
        assert result["id"] == self.GRANTED_HAND_WRITTEN_RULE

    def test_prose_naming_the_granted_command_no_longer_matches_it(
        self, bash_hook, bundled_config
    ):
        """The #915 fix, on the one rule that carries a grant.

        This is a genuine behaviour change and it is the desirable direction: a
        report that MENTIONS sending an email no longer consumes the grant path
        at all. It lands on allow for having no rule, which is correct here
        because nothing is being sent.
        """
        result = bash_hook.check_command(
            'agentwire msg send --to orch --kind done '
            '"agentwire email was refused, so the owner was not notified"',
            bundled_config,
        )
        assert result["decision"] == "allow"
        assert result.get("id") != self.GRANTED_HAND_WRITTEN_RULE


class TestQuotedCommandSubstitutionIsNotContent:
    """A dangerous command INSIDE a quoted substitution must still refuse.

    The hole this closes, found in review: ``git commit -m "$(rm -rf /x)"`` went
    BLOCK -> ALLOW **including under AGENTWIRE_UNATTENDED=1**, removing the
    fail-closed guarantee that is the entire point of the unattended tier.

    Three things had to line up and no one of them does it alone:

    1. this PR anchors ``core.rm-*``, so the rules match the MASKED haystack;
    2. masking blanks a fully-quoted whitespace-containing token — and
       ``"$(rm -rf /x)"`` is exactly that — so the payload became invisible;
    3. #917 ships ``git.commit`` in ``DEFAULT_UNATTENDED_ALLOW`` **unscoped**,
       so the residual ``ask`` resolved to ``allow`` with no human. The scope
       evaluator does return *unscopeable* for a substitution, but an unscoped
       grant never consults it — bypassed, not defeated.

    On main step 1 is absent, so the raw haystack still caught it. Nothing went
    red because no test anywhere covered a dangerous command inside a QUOTED
    substitution — the earlier falsification corpus was the mirror arrangement
    (``rm -rf "$(cat f)"``, dangerous verb OUTSIDE the quotes), which survives
    masking and always did.

    Fix: ``is_content`` no longer masks a quoted token containing ``$(`` or a
    backtick. Strictly more inclusive, so it cannot weaken any rule.
    """

    RM = "rm -" + "rf"
    TF = "terraform " + "destroy"

    #: (command, the rule family that must own the refusal)
    GRANTED_CARRIER_CASES = [
        (f'git commit -m "$({RM} /tmp/x)"', "core.rm-with-recursive-or-force-flags"),
        (f'git commit -m "$({TF})"',
         "infrastructure.terraform-destroy-destroys-all-infrastructure"),
        ('git commit -m "$(gh repo delete o/r)"',
         "cloud-hosting.gh-repo-delete-deletes-repository"),
        ('git commit -m "$(kubectl delete namespace prod)"',
         "containers.kubectl-delete-namespace"),
        (f'git commit -m `{RM} /tmp/x`', "core.rm-with-recursive-or-force-flags"),
    ]

    @pytest.mark.parametrize("command,rule_id", GRANTED_CARRIER_CASES)
    def test_refused_and_by_the_rule_that_owns_it(
        self, bash_hook, bundled_config, command, rule_id
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "block", (
            f"{command!r} decided {result['decision']} — the payload is hidden "
            f"from the anchored rules again"
        )
        assert result["id"] == rule_id, (
            f"{command!r} blocked via {result['id']!r}, not the rule that owns "
            f"the payload — something else took the credit"
        )

    @pytest.mark.parametrize("command,_rule", GRANTED_CARRIER_CASES)
    def test_unattended_column_specifically(
        self, bash_hook, bundled_config, command, _rule
    ):
        """The column that actually mattered.

        An interactive ``ask`` looks harmless and is what hid this: the carrier
        holds an unscoped grant, so ``ask`` becomes ``allow`` with no human. A
        hard ``block`` is the only verdict that survives the unattended
        resolver, so assert the tier rather than merely "not allow".
        """
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "block", (
            f"{command!r} is {result['decision']}, not block — an ask on a "
            f"carrier holding an unscoped grant resolves to ALLOW unattended"
        )

    def test_echo_contrast_proves_the_grant_is_load_bearing(
        self, bash_hook, bundled_config
    ):
        """Same shape, no grant. It failed closed even while `git commit` did
        not, which is what identified the grant as the third ingredient."""
        result = bash_hook.check_command(
            f'echo "$({self.RM} /tmp/x)"', bundled_config
        )
        assert result["decision"] == "block"

    def test_mirror_arrangement_still_refused(self, bash_hook, bundled_config):
        """Dangerous verb OUTSIDE the quotes — the earlier corpus. Never broke,
        asserted so the two arrangements stay distinguishable in the record."""
        result = bash_hook.check_command(
            f'{self.RM} "$(cat /tmp/targets)"', bundled_config
        )
        assert result["decision"] == "block"

    def test_prose_without_a_substitution_is_still_masked(
        self, bash_hook, bundled_config
    ):
        """The #915 fix survives the repair — a plain report still sends."""
        result = bash_hook.check_command(
            'agentwire msg send --to orch --kind done '
            '"the merge could not be completed -- I did not rm the file"',
            bundled_config,
        )
        assert result["decision"] == "allow"

    def test_single_quoted_substitution_is_inert_and_stays_masked(
        self, bash_hook, bundled_config
    ):
        """EXPECTED NON-BLOCK, and the reason must travel with the row.

        Single quotes suppress expansion, so ``git commit -m '$(rm -rf /x)'``
        commits the literal text and runs nothing. There is no payload to catch,
        and "fixing" this into a block would be a false positive on a string the
        shell never executes.

        My first version of the fix keyed on the RESOLVED token text, which has
        already lost the quote type — so it blocked this. The scanner now
        reports whether the substitution sat in an EXPANDING span, which is the
        distinction that matters.

        It lands on ``ask`` rather than ``allow`` only because ``$(`` in the raw
        command trips the pre-existing obfuscation fallback — unrelated to this
        PR, and the conservative direction.
        """
        result = bash_hook.check_command(
            f"git commit -m '$({self.RM} /tmp/x)'", bundled_config
        )
        assert result["decision"] != "block", (
            "a single-quoted substitution is inert — blocking it is a false "
            "positive on text the shell never runs"
        )
        masked = bash_hook.masked_subcommands(f"git commit -m '$({self.RM} /x)'")
        assert self.RM not in masked[0], (
            "the single-quoted token should still be masked as content"
        )

    def test_masked_form_keeps_the_substitution_visible(self, bash_hook):
        """The mechanism, asserted directly rather than via a verdict."""
        masked = bash_hook.masked_subcommands(f'git commit -m "$({self.RM} /x)"')
        assert masked == [f"git commit -m $({self.RM} /x)"]
        # and a substitution-free quoted token is still masked
        plain = bash_hook.masked_subcommands('git commit -m "a plain message"')
        assert "a plain message" not in plain[0]

    @pytest.mark.parametrize("command,_rule", GRANTED_CARRIER_CASES)
    def test_mutation_reverting_the_fix_makes_these_go_red(
        self, bash_hook, bundled_config, command, _rule
    ):
        """Re-mask quoted substitutions and every row above must fall through.

        Without this the rows could be passing for an unrelated reason; with it,
        the fix is shown to be what carries them.
        """
        original = bash_hook.anchored_match_entries

        def remasked(cmd):
            # the pre-fix behaviour: blank any fully-quoted whitespace token,
            # substitution or not — emulated by stripping the substitution
            # spans before the anchored haystacks are built, so `is_content`
            # reverts to its old verdict. (anchored_match_entries is the seam
            # the anchored path reads since #922's position enforcement.)
            import re as _re
            return original(_re.sub(r"\$\([^)]*\)|`[^`]*`", "PLACEHOLDER TEXT", cmd))

        bash_hook.anchored_match_entries = remasked
        try:
            result = bash_hook.check_command(command, bundled_config)
        finally:
            bash_hook.anchored_match_entries = original
        assert result["decision"] != "block", (
            f"mutation is inert for {command!r} — it still blocks with the "
            f"substitution masked, so the shipped row proves nothing"
        )


class TestMutationProvesTheAssertionsHaveTeeth:
    """MEANING INVERTED BY #924 — read before editing.

    This mutation (flip every rule to ``anchored: true``) used to make every
    wrapper case go through, proving the wrapped coverage lived on the
    unanchored haystacks alone. The #924 wrapper-payload rescan made that
    coverage STRUCTURAL: an ssh payload is re-scanned as a command in its own
    right and a client payload (``psql -c``) is emitted as exec-surface text,
    so anchored rules reach both. The assertion is therefore now the opposite:
    blanket anchoring must NOT lose the wrapped forms anymore. (The per-file
    do-not-anchor policy is still enforced by TestAnchoringIsFileWide — it
    protects the rules' OWN matching, not the wrapper coverage.)
    """

    @pytest.fixture(scope="class")
    def all_anchored(self, bundled_config):
        mutated = dict(bundled_config)
        mutated["bashToolPatterns"] = [
            {**p, "anchored": True} for p in bundled_config["bashToolPatterns"]
        ]
        return mutated

    # Rows whose refusal now SURVIVES blanket anchoring. Two mechanisms, both
    # #924: an ssh payload with a rule of its own is re-scanned as a command
    # (anchored rules reach it at real command positions), and a client
    # payload whose rule matches the STATEMENT alone (DROP TABLE, TRUNCATE…)
    # is emitted as payload text, matched anywhere.
    STRUCTURAL = [
        c for c in SSH_WRAPPED_MASKED
        if c not in (
            'ssh prod "reboot now"',
            'ssh prod "shutdown -h now"',
            'ssh prod "systemctl stop nginx"',
        )
    ] + [
        'psql -c "DROP TABLE users"',
        'psql -c "DROP DATABASE production"',
        'psql -c "TRUNCATE TABLE users"',
        'psql -c "DELETE FROM users;"',
        'mysql -e "DROP DATABASE production"',
    ]

    # Rows still carried ONLY by the unanchored haystacks: ssh payloads with
    # no rule of their own (reboot/shutdown/service-stop — remote.yaml is
    # their whole coverage), and rules that pair the CLIENT NAME with the
    # statement in one regex (db.psql-write, mysql UPDATE, mongosh, perl
    # unlink) — the payload-text entry lacks the client name, so anchoring
    # those rules loses the guard. This is exactly why remote.yaml and
    # payloads.yaml stay unanchored per file, and this mutation keeps that
    # policy's teeth.
    HAYSTACK_ONLY = [
        'ssh prod "reboot now"',
        'ssh prod "shutdown -h now"',
        'ssh prod "systemctl stop nginx"',
        'psql -h db -c "INSERT INTO users VALUES (1)"',
        'mysql -e "UPDATE users SET admin = 1"',
        'mongosh --eval "db.users.deleteMany({ })"',
        "perl -e 'unlink \"/srv/data\"'",
    ]

    @pytest.mark.parametrize("command", STRUCTURAL)
    def test_blanket_anchoring_no_longer_loses_the_structural_rows(
        self, bash_hook, bundled_config, all_anchored, command
    ):
        assert bash_hook.check_command(command, bundled_config)["decision"] in REFUSED
        mutated = bash_hook.check_command(command, all_anchored)["decision"]
        assert mutated in REFUSED, (
            f"{command!r} is {mutated} under blanket anchoring — the #924 "
            f"rescan no longer carries the wrapped form on its own"
        )

    @pytest.mark.parametrize("command", HAYSTACK_ONLY)
    def test_blanket_anchoring_still_loses_the_haystack_only_rows(
        self, bash_hook, bundled_config, all_anchored, command
    ):
        assert bash_hook.check_command(command, bundled_config)["decision"] in REFUSED
        mutated = bash_hook.check_command(command, all_anchored)["decision"]
        assert mutated not in REFUSED, (
            f"mutation is inert for {command!r} (still {mutated}) — the shipped "
            f"assertion for it proves nothing about the anchoring decision"
        )

    def test_the_two_groups_cover_the_whole_corpus(self):
        assert sorted(self.STRUCTURAL + self.HAYSTACK_ONLY) == sorted(
            SSH_WRAPPED_MASKED + CLIENT_WRAPPED_MASKED
        )

    def test_unanchoring_reintroduces_the_reported_bug(self, bash_hook, bundled_config):
        """The other direction: drop anchoring and #915 comes straight back."""
        mutated = dict(bundled_config)
        mutated["bashToolPatterns"] = [
            {**p, "anchored": False} for p in bundled_config["bashToolPatterns"]
        ]
        command = (
            'agentwire msg send --to memory-manager --kind done '
            '"the merge could not be completed -- I did not rm the file"'
        )
        assert bash_hook.check_command(command, bundled_config)["decision"] == "allow"
        assert bash_hook.check_command(command, mutated)["decision"] in REFUSED


# ---------------------------------------------------------------------------
# The reported symptom — the small half
# ---------------------------------------------------------------------------

# The two real 2026-08-06 failures were `--kind done` reports describing a
# blocked deletion — the load-bearing kind that dead-letters to the owner.
PAYLOADS = [
    "the merge could not be completed -- I did not rm the file or route around it",
    "file deletion is blocked by damage-control, so cleanup was skipped",
    "damage-control refused rm -rf on the stale worktree; left it in place",
    "I did not run git reset --hard; the branch is untouched",
    "tried terraform destroy on the sandbox stack and it was refused",
    "kubectl delete namespace was blocked, so the test namespace is still up",
    "the probe listed dropdb and gh repo delete as TEST DATA, nothing ran",
    "chmod 777 was rejected by the hook -- permissions unchanged",
    "docker volume rm and helm uninstall both refused; nothing was removed",
    "npm unpublish and history -c are on the blocked list, as expected",
]


class TestReportBackPayloadsAreDelivered:
    @pytest.mark.parametrize("body", PAYLOADS)
    def test_msg_send_done_is_allowed(self, bash_hook, bundled_config, body):
        command = f'agentwire msg send --to memory-manager --kind done "{body}"'
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "allow", (
            f"a report-back was refused for its own text: {result['reason']}"
        )

    @pytest.mark.parametrize("body", PAYLOADS)
    def test_notify_parent_is_allowed(self, bash_hook, bundled_config, body):
        command = f'agentwire notify-parent --to orchestrator "{body}"'
        assert bash_hook.check_command(command, bundled_config)["decision"] == "allow"


class TestItIsNotOnlyMsgSend:
    """Any command whose ARGUMENTS discuss a guarded operation — including the
    tooling you would use to audit the guard itself."""

    # (carrier template, guarded-op payload). The assertion is PARITY: the same
    # carrier with an innocuous payload must get the same verdict. Some carriers
    # are ask-tier tooldef commands in their own right (`git commit`,
    # `gh issue comment`) — that is by design and has nothing to do with #915,
    # so asserting a bare `allow` would be asserting the wrong thing.
    CARRIERS = [
        ('echo "{}"', "the rm -rf was refused by damage-control"),
        ('grep -rn "{}" rules/', "rm file deletion (use git clean or manual cleanup)"),
        ('grep -rn "{}" docs/', "terraform destroy"),
        ('git commit -m "{}"', "note: rm -rf of the build dir was blocked"),
        ('gh issue comment 915 --body "{}"', "damage-control refused rm -rf here"),
        ("gh pr create --body-file - <<EOF\n{}\nEOF", "rm -rf was refused here"),
        ('agentwire msg send --to orch --kind done "{}"',
         "damage-control refused rm -rf on the stale worktree"),
    ]
    INNOCUOUS = "everything went fine and nothing needed attention"


    @pytest.mark.parametrize(
        "template,payload", CARRIERS, ids=[t[:28] for t, _ in CARRIERS]
    )
    def test_payload_text_does_not_change_the_verdict(
        self, bash_hook, bundled_config, template, payload
    ):
        loaded = bash_hook.check_command(template.format(payload), bundled_config)
        plain = bash_hook.check_command(
            template.format(self.INNOCUOUS), bundled_config
        )
        assert loaded["decision"] == plain["decision"], (
            f"{template!r} changed verdict on its payload alone: "
            f"{plain['decision']} with innocuous text, {loaded['decision']} with "
            f"{payload!r} ({loaded['reason']}) — that is #915."
        )
        # and the reason must not be a rule about the operation being described
        assert loaded.get("id") == plain.get("id"), (
            f"{template!r} attributed to a different rule on payload alone: "
            f"{plain.get('id')} -> {loaded.get('id')}"
        )

    def test_reading_an_unprotected_repo_path_is_not_blocked_by_bash_rules(
        self, bash_hook, bundled_config
    ):
        """Named for what it proves, which is narrower than it looks.

        All three commands read a path under the REPO, so no path ladder
        engages — this exercises the bashToolPatterns half only. The original
        reported command reads a PROTECTED directory and still fails
        (mechanism 2, #922); see TestRemainingPayloadMechanisms.
        """
        for command in (
            "diff ~/.agentwire/damage-control/core.yaml "
            "agentwire/hooks/damage-control/rules/core.yaml",
            'grep -n "rm -rf" agentwire/hooks/damage-control/rules/core.yaml',
            'rg --fixed-strings "git reset --hard" agentwire/',
        ):
            result = bash_hook.check_command(command, bundled_config)
            assert result["decision"] == "allow", (
                f"reading the rules is refused by the rules: {command!r} "
                f"-> {result['reason']}"
            )


# ---------------------------------------------------------------------------
# What this PR does NOT fix — tracked as #922
# ---------------------------------------------------------------------------


class TestRemainingPayloadMechanisms:
    """The payload bug had THREE mechanisms; #922 closes the other two.

    History: #915's anchoring fixed mechanism 1 (bashToolPatterns matching
    quoted prose), and this class used to pin mechanisms 2 and 3 as
    still-refused ON PURPOSE so a green suite could not read as "the reported
    symptom is fixed". #922 fixed them, so the pins now assert the FIX — and,
    since both fixes are guard-weakenings, each released read carries a
    companion destructive form asserted to still refuse by its own mechanism.

    - **Mechanism 2 — the path ladders.** ``readOnlyPaths`` / ``noDeletePaths``
      (and the protected control plane) now match their verb patterns against
      masked haystacks with path-mentioning operands kept visible
      (``path_ladder_haystacks``), so a read whose SEARCH STRING mentions a
      deletion no longer refuses. ``zeroAccessPaths`` is untouched: it blocks a
      MENTION regardless of any verb, by design.
    - **Mechanism 3 — masking was keyed on WHITESPACE.** Anchored rules now
      match only at COMMAND POSITION (``anchored_match_entries``), so a
      single-word quoted operand can no longer supply the verb. Rules that
      deliberately match argument content (payloads.yaml, remote.yaml) are
      unanchored and unaffected.
    """

    # These rows assert on ``~``-form protected paths, so they need $HOME to
    # look like a real home — the same reason and the same marker as
    # test_damage_control_bypass.py (a tmp HOME under /tmp is allowlisted
    # `allow: all`, which outranks the ladders and makes the rows meaningless).
    pytestmark = pytest.mark.real_agentwire_home

    # mechanism 2 — the literal incident from #915's body, in its literal form,
    # now released. Destructive companions asserted below.
    LADDER_READS = [
        'grep -rn "rm -rf" ~/.agentwire/',
        'rg "rm -rf" ~/.claude/hooks/',
        # `.git/` is a RELATIVE noDeletePath — no $HOME and no tmp prefix, so
        # this row holds regardless of how the suite redirects HOME.
        'grep -rn "rm -rf" .git/',
        # the #922 acceptance command, verbatim
        'grep -rn "rm file deletion" ~/.agentwire/damage-control/',
    ]

    # The guard half: a real destructive verb against the same paths must still
    # refuse — by the mechanism that owns it, not incidentally.
    LADDER_DESTRUCTIVE = [
        ("shred ~/.agentwire/sessions.json", "noDeletePath"),
        ("unlink .git/HEAD", "noDeletePath"),
        # quoted path operand with internal whitespace: the ladder
        # keep-predicate is what keeps this token visible
        ('shred "~/.agentwire/dead letters.json"', "noDeletePath"),
    ]

    @pytest.mark.parametrize("command", LADDER_READS)
    def test_path_ladder_releases_a_read_that_mentions_a_deletion(
        self, bash_hook, bundled_config, command
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "allow", (
            f"{command!r} refused via {result.get('pattern')} — mechanism 2 "
            f"(#922) has regressed: a read is refused for what its search "
            f"string SAYS"
        )

    @pytest.mark.parametrize("command,via", LADDER_DESTRUCTIVE)
    def test_path_ladder_still_refuses_the_destructive_form(
        self, bash_hook, bundled_config, command, via
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] in REFUSED, (
            f"{command!r} now passes — the ladder fix released a real delete"
        )
        assert via in str(result.get("pattern", "")), (
            f"expected refusal via {via}, got {result.get('pattern')}"
        )

    @pytest.mark.parametrize(
        "ladder,command,expected",
        [
            # verb-pairing ladders: prose can no longer supply the verb
            ("noDeletePaths", 'grep -rn "rm -rf" /srv/protected/', "allow"),
            ("readOnlyPaths", 'grep -rn "rm -rf" /srv/readonly/', "allow"),
            # zeroAccess blocks the MENTION — no verb involved, deliberately
            # unchanged by #922
            ("zeroAccessPaths", 'grep -rn "rm -rf" /srv/secret/', "block"),
        ],
    )
    def test_each_ladder_step_on_a_read_naming_its_search_string(
        self, bash_hook, ladder, command, expected
    ):
        """One row per ladder step, against literal paths — the verb-pairing
        steps release the read, the mention-keyed step still refuses it."""
        cfg = {
            "bashToolPatterns": [],
            "zeroAccessPaths": [],
            "readOnlyPaths": [],
            "noDeletePaths": [],
            "allowedPaths": [],
            "safety": dict(SAFETY),
        }
        cfg[ladder] = [command.rsplit(" ", 1)[-1]]
        result = bash_hook.check_command(command, cfg)
        assert result["decision"] == expected, (
            f"{ladder}: {command!r} -> {result['decision']}, expected "
            f"{expected} ({result.get('reason')})"
        )

    def test_protected_control_plane_releases_a_read(self, bash_hook):
        """Ladder step 0, with an EMPTY allowlist passed explicitly.

        The read is released; the write/delete spellings against the same
        paths — quoting tricks and interpreter forms included — must all
        still refuse.
        """
        blocked, _ = bash_hook.check_protected_command(
            'rg "rm -rf" ~/.claude/hooks/', []
        )
        assert not blocked, (
            "a read of the control plane is still refused for what its search "
            "string says — mechanism 2 (#922) regressed at step 0"
        )
        for command in (
            "rm ~/.claude/hooks/idle-handler.sh",
            "r''m ~/.claude/hooks/idle-handler.sh",
            "echo x > ~/.claude/settings.json",
            "sed -i s/x/y/ ~/.claude/hooks/idle-handler.sh",
            'shred "~/.claude/hooks/idle handler.sh"',
            "python3 -c 'open(p, \"w\").write(x)' ~/.agentwire/damagecontrol.yml",
        ):
            blocked, reason = bash_hook.check_protected_command(command, [])
            assert blocked, (
                f"{command!r} no longer refused — the #922 ladder fix "
                f"released a control-plane write"
            )
            assert reason

    def test_allowlist_outranks_the_ladders(self, bash_hook):
        """Pin the trap itself, since it has cost two sessions a red CI.

        An `allow: all` entry short-circuits BOTH ladders. Asserted on a REAL
        delete now that the read form is released without any allowlist."""
        cfg = {
            "bashToolPatterns": [],
            "zeroAccessPaths": [],
            "readOnlyPaths": [],
            "noDeletePaths": ["/srv/protected/"],
            "allowedPaths": [],
            "safety": dict(SAFETY),
        }
        command = "unlink /srv/protected/state.json"
        assert bash_hook.check_command(command, cfg)["decision"] in REFUSED
        cfg["allowedPaths"] = [{"path": "/srv/*", "allow": "all"}]
        assert bash_hook.check_command(command, cfg)["decision"] == "allow", (
            "an allow:all entry no longer outranks noDeletePaths — the trap "
            "that made this file's CI red has changed shape"
        )

    # mechanism 3 — prose operands are released; live payload carriers stay
    # refused because THEIR rules (payloads.yaml, remote.yaml) are unanchored
    # by invariant and never position-gated.
    @pytest.mark.parametrize(
        "command,expected",
        [
            ('agentwire msg send --to orch --kind done "rmdir"', "allow"),
            ("true  # note: git reset --hard was blocked", "allow"),
            ('ssh prod "reboot"', "ask"),
            ('mongosh --eval "db.dropDatabase()"', "block"),
        ],
    )
    def test_single_word_operand_verbs_are_position_gated(
        self, bash_hook, bundled_config, command, expected
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == expected, (
            f"{command!r} -> {result['decision']}, expected {expected} "
            f"({result.get('reason')})"
        )

    def test_the_masking_control_case_does_pass(self, bash_hook, bundled_config):
        """Two words, so it IS masked — the boundary mechanism 3 sat on."""
        assert bash_hook.check_command(
            'echo "terraform destroy"', bundled_config
        )["decision"] == "allow"


class TestSshWrappedCoverageReduction:
    """#924 LANDED — this class was the expected-fail disclosure of the gap.

    Before the wrapper-payload rescan, ~125 of 151 ssh-wrapped dangerous forms
    were allowed (only ``remote.yaml``'s hand-written twins were intentional
    coverage). The rescan extracts ssh's remote command and re-scans it as a
    command in its own right, so every rule applies wrapped — asserted here
    with the RULE ID, because the point is that the payload's OWN rule now
    catches it, not an ssh twin.
    """

    # One per rule family; the id each unwrapped payload blocks under.
    NOW_REFUSED = [
        ('ssh prod "terraform destroy"', "infrastructure"),
        ('ssh prod "gh repo delete owner/repo"', "cloud-hosting"),
        ('ssh prod "aws ec2 terminate-instances --instance-ids i-1"', "aws"),
        ('ssh prod "gcloud projects delete my-proj"', "gcp"),
        ('ssh prod "kubectl delete namespace prod"', "containers"),
        ('ssh prod "helm uninstall release"', "containers"),
        ('ssh prod "docker volume rm pgdata"', "containers"),
        ('ssh prod "npm unpublish my-pkg"', "cloud-hosting"),
        ('ssh prod "chmod 777 /srv"', "core"),
        ('ssh prod "tmux kill-server"', "agentwire"),
        ('ssh prod "prisma migrate reset"', "db"),
        ('ssh prod "history -c"', "core"),
    ]

    @pytest.mark.parametrize("command,id_prefix", NOW_REFUSED)
    def test_ssh_wrapped_form_refused_by_the_payloads_own_rule(
        self, bash_hook, bundled_config, command, id_prefix
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] in REFUSED, f"{command!r} → {result['decision']}"
        rule_id = str(result.get("id") or "")
        assert rule_id.split(".")[0] == id_prefix and not rule_id.startswith(
            "remote."
        ), (
            f"{command!r} refused by {rule_id!r}, expected the payload's own "
            f"{id_prefix}.* rule — an ssh twin taking the credit means the "
            f"rescan is not what carries this form"
        )

    def test_remote_yaml_intentional_coverage_is_what_survives(
        self, bash_hook, bundled_config
    ):
        """The 12 forms remote.yaml exists for are unaffected — that is the
        line between 'demoted incidental coverage' and 'broke the guard'."""
        for command in SSH_WRAPPED_MASKED:
            assert bash_hook.check_command(command, bundled_config)[
                "decision"
            ] in REFUSED, f"{command!r} — remote.yaml's own coverage broke"


class TestExecSurfacePayloadsStayVisible:
    """A quoted payload on an EXEC SURFACE must not be masked away (#924 class).

    Anchoring the rule files stops the raw haystack being consulted, so masking
    a fully-quoted multi-word token took NINE measured commands from BLOCK to
    ``allow`` **with no rule matching at all** — no grant consulted, and no
    ``core.ambiguous-command`` backstop either, because with no ``$(`` present
    the ambiguity detector never fires. Measured across real trees per revision
    with ``_core`` imported from inside each, so neither side is a hybrid.

    WHY THE OBVIOUS FIXES ARE WRONG, since all three were tried:

    - ``$(``/backtick syntax (the fix one class up) misses every row here.
    - Adding these names to ``_SHELL_NAMES`` is semantically false: that set
      means "this payload IS a shell command" — true for ``sh -c``, false for
      ``python3 -c``, where the payload is source that merely contains one.
    - The additive-haystack pattern — emit every masked token as its own entry —
      fixes all nine AND reinstates #915 for all prose, because a report payload
      becomes a command-position entry again. The pattern that was right for
      #918 and #932 is wrong here.

    So the distinguisher is POSITION: is this token the value of an exec-surface
    option? That needs a table, and ``_EXEC_SURFACES`` is the minimal instance
    of #924's eventual one. A tool missing from it degrades to today's
    behaviour, not worse.

    Rule IDs are asserted, not verdicts — a block from some other rule satisfies
    a verdict-only row while the payload stays invisible.
    """

    REGRESSED = [
        ("tmux -c 'rm -rf /tmp/x' new-session", "core.rm-with-recursive-or-force-flags"),
        ("tmux new-session -d 'rm -rf /tmp/x'", "core.rm-with-recursive-or-force-flags"),
        ("""python3 -c 'import os; os.system("rm -rf /tmp/x")'""", "core.rm-with-recursive-or-force-flags"),
        ("""perl -e 'system("rm -rf /tmp/x")'""", "core.rm-with-recursive-or-force-flags"),
        ("""node -e 'require("child_process").execSync("rm -rf /tmp/x")'""", "core.rm-with-recursive-or-force-flags"),
        ("""ruby -e 'system("rm -rf /tmp/x")'""", "core.rm-with-recursive-or-force-flags"),
        ("""awk 'BEGIN{system("rm -rf /tmp/x")}'""", "core.rm-with-recursive-or-force-flags"),
        ("watch -n1 'rm -rf /tmp/x'", "core.rm-with-recursive-or-force-flags"),
        ("make -c 'rm -rf /tmp/x'", "core.rm-with-recursive-or-force-flags"),
    ]

    @pytest.mark.parametrize("command,rule_id", REGRESSED)
    def test_regressed_row_refuses_by_its_own_rule(
        self, bash_hook, bundled_config, command, rule_id
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "block", (
            f"{command!r} is {result['decision']} — an exec-surface payload is "
            f"masked away again. On main this blocks; anchored without the "
            f"table it lands on allow with NO rule matching at all."
        )
        assert result["id"] == rule_id, (
            f"{command!r} blocked via {result['id']!r}, not {rule_id!r} — "
            f"something else took the credit and the payload is still hidden"
        )

    # HELD rows: correct before this change and must not move. A "fix" that
    # works by widening the table until everything matches breaks these.
    HELD = [
        "timeout 5 sh -c 'rm -rf /tmp/x'",
        "env FOO=1 sh -c 'rm -rf /tmp/x'",
        "nohup sh -c 'rm -rf /tmp/x'",
        "xargs -I{} sh -c 'rm -rf /tmp/x'",
        "find . -exec sh -c 'rm -rf /tmp/x' ;",
        "sh -c 'rm -rf /tmp/x'",
        "bash -c 'rm -rf /tmp/x'",
        "su -c 'rm -rf /tmp/x' root",
        "ssh prod 'rm -rf /tmp/x'",
        'psql -c "DROP TABLE users"',
    ]

    @pytest.mark.parametrize("command", HELD)
    def test_held_row_unchanged(self, bash_hook, bundled_config, command):
        assert bash_hook.check_command(command, bundled_config)["decision"] in REFUSED

    def test_prose_is_still_masked(self, bash_hook, bundled_config):
        """The bound on this fix: a report payload is NOT an exec surface.

        This is the row that fails if anyone repairs the class with the additive
        pattern instead of the table.
        """
        result = bash_hook.check_command(
            "agentwire msg send --to orch --kind done "
            '"the merge failed -- I did not rm -rf the file"',
            bundled_config,
        )
        assert result["decision"] == "allow"

    def test_a_missing_exec_surface_degrades_to_today_not_worse(
        self, bash_hook, bundled_config
    ):
        """A tool absent from the table behaves exactly as it does now."""
        assert "notatool" not in bash_hook._EXEC_SURFACES
        result = bash_hook.check_command(
            "notatool -c 'rm -rf /tmp/x'", bundled_config
        )
        assert result["decision"] == "allow"


class TestPayloadRescanAndHaystackSynthesisDisagreeOnPurpose:
    """The any-word rescan must NOT be copied into ``_strip_global_options``.

    Two loops in ``_core.py`` look alike and want opposite answers:

    - the payload rescan (this PR) scans ANY word for a shell name, because a
      wrapper prefix puts the shell in the middle and the payload is already an
      isolated quoted token — a false positive costs a rescan of prose;
    - ``_strip_global_options`` (#919) scans only ``_WRAPPER_PREFIXES`` and
      documents the resulting gap, because it SYNTHESISES a new command-position
      haystack — scan any word there and ``echo git -C /r push --force``
      emits ``echo git push --force``, matching the force-push rule on an
      ECHO. That is #675/#915 re-opened by the guard that exists because prose
      was being blocked.

    Sitting in one file those read as an inconsistency to harmonise, and the
    obvious harmonisation is the harmful direction. This pins the property so
    the comment is not the only thing defending it.
    """

    ECHOES = [
        "echo git -C /repo push --force",
        'echo "git -C /repo push --force was refused by damage-control"',
        'agentwire msg send --to orch --kind done '
        '"git -C /repo push --force was refused"',
    ]

    @pytest.mark.parametrize("command", ECHOES)
    def test_prose_naming_a_global_option_command_is_not_blocked(
        self, bash_hook, bundled_config, command
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "allow", (
            f"{command!r} is {result['decision']} — a synthesised haystack is "
            f"matching prose. If _strip_global_options was widened to scan any "
            f"word, revert that: the two loops disagree on purpose."
        )

    def test_the_real_command_still_blocks(self, bash_hook, bundled_config):
        """The other side of the asymmetry — narrow there must not cost this."""
        result = bash_hook.check_command(
            "git -C /repo push --force origin main", bundled_config
        )
        assert result["decision"] == "block"
        assert result["id"] == "git.git-push-force-use-force-with-lease"
