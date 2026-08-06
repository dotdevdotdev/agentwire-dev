"""#915 — a report-back must not be refused because of what it SAYS.

``agentwire msg send --to X --kind done "<text>"`` runs its payload through the
Bash rules, so a message that merely *describes* a blocked operation was itself
blocked. It is not a ``msg send`` bug: any command whose ARGUMENTS discuss a
guarded operation is refused — ``echo``, ``grep`` for a rule's own reason text,
a probe script listing dangerous commands as test data. Reading the rules is
blocked by the rules.

#675 fixed this shape for tooldef-derived rules and for ``git.yaml`` with
``anchored: true`` (match masked command position, never quoted argument
content). This extends the same per-rule property to the rest of the bundled
set, and marks the rules that must NOT get it.

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
    cfg = bash_hook.load_config(RULES_DIR, None)
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
        assert len(remote) == 12
        assert all(e["anchored"] is False for e in remote)

    def test_payloads_rules_pin_their_ids(self):
        """The move must not churn ids that safety.disabled_rules may name."""
        payloads = [e for f, e in BUNDLED if f == "payloads.yaml"]
        assert len(payloads) == 15
        assert all(e.get("id") for e in payloads), (
            "every rule moved into payloads.yaml needs an explicit `id:` pinned "
            "to the id it had in its old file — otherwise the id is re-derived "
            "from the new filename and any config naming it breaks."
        )
        # ids are pinned to the ORIGINAL file, which is the point
        assert {e["id"].split(".")[0] for e in payloads} == {
            "core", "databases", "db", "containers",
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
    '\\brm\\s+.*\\.agentwire': 'rm -r ~/.agentwire/sessions',
    '\\brm\\s+.*~/.agentwire': 'rm -r ~/.agentwire',
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
    """The inner command arrives as a quoted arg — the reason remote.yaml
    stays unanchored. remote.yaml had ZERO coverage in the suite before this."""

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
    'python3 -c "import shutil; shutil.rmtree(\'/srv\')"',
    "perl -e 'unlink \"/srv/data\"'",
]

CLIENT_WRAPPED_SURVIVES = [
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


class TestMutationProvesTheAssertionsHaveTeeth:
    """Anchor the must-not-anchor class and the guard measurably disappears.

    Without this, the tests above could be green for the wrong reason. The
    mutation flips every rule to ``anchored: true`` — the blanket change this
    PR deliberately did NOT make — and asserts each wrapper case goes through.
    """

    @pytest.fixture(scope="class")
    def all_anchored(self, bundled_config):
        mutated = dict(bundled_config)
        mutated["bashToolPatterns"] = [
            {**p, "anchored": True} for p in bundled_config["bashToolPatterns"]
        ]
        return mutated

    @pytest.mark.parametrize("command", SSH_WRAPPED_MASKED + CLIENT_WRAPPED_MASKED)
    def test_blanket_anchoring_loses_the_wrapper_guard(
        self, bash_hook, bundled_config, all_anchored, command
    ):
        assert bash_hook.check_command(command, bundled_config)["decision"] in REFUSED
        mutated = bash_hook.check_command(command, all_anchored)["decision"]
        assert mutated not in REFUSED, (
            f"mutation is inert for {command!r} (still {mutated}) — the shipped "
            f"assertion for it proves nothing about the anchoring decision"
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

    CARRIERS = [
        'echo "the rm -rf was refused by damage-control"',
        'grep -rn "rm file deletion (use git clean or manual cleanup)" rules/',
        'grep -rn "terraform destroy" docs/',
        'git commit -m "note: rm -rf of the build dir was blocked"',
        'gh issue comment 915 --body "damage-control refused rm -rf here"',
        "gh pr create --body-file - <<EOF\nrm -rf was refused here\nEOF",
    ]

    # KNOWN LIMIT: masking works on quoted tokens and heredoc bodies, not on
    # trailing shell comments — `true  # git reset --hard was blocked` still
    # matches, because `# …` is left inline in the masked subcommand. Out of
    # scope here (it is a masked_subcommands change, owned by #913).
    UNFIXED_COMMENT_FORM = "true  # note: git reset --hard was blocked"

    @pytest.mark.parametrize("command", CARRIERS)
    def test_allowed(self, bash_hook, bundled_config, command):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] == "allow", (
            f"{command!r} refused for its argument text: {result['reason']}"
        )

    def test_trailing_shell_comment_is_a_known_remaining_hole(
        self, bash_hook, bundled_config
    ):
        """Documented, not fixed — asserted so the day it changes is visible."""
        result = bash_hook.check_command(self.UNFIXED_COMMENT_FORM, bundled_config)
        assert result["decision"] in REFUSED

    def test_reading_the_rules_is_not_blocked_by_the_rules(
        self, bash_hook, bundled_config
    ):
        """The sharpest live instance: auditing the guard tripped the guard."""
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
