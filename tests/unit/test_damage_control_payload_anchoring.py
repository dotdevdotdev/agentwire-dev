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
    assert len(hand) == 178, f"expected 178 hand-written rules, got {len(hand)}"
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
        original = bash_hook.masked_subcommands

        def remasked(cmd):
            # the pre-fix behaviour: blank any fully-quoted whitespace token,
            # substitution or not — emulated by stripping the substitution
            # spans before masking so `is_content` reverts to its old verdict
            import re as _re
            return original(_re.sub(r"\$\([^)]*\)|`[^`]*`", "PLACEHOLDER TEXT", cmd))

        bash_hook.masked_subcommands = remasked
        try:
            result = bash_hook.check_command(command, bundled_config)
        finally:
            bash_hook.masked_subcommands = original
        assert result["decision"] != "block", (
            f"mutation is inert for {command!r} — it still blocks with the "
            f"substitution masked, so the shipped row proves nothing"
        )


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
    """The payload bug has THREE mechanisms; anchoring fixes ONE.

    These are asserted as still-refused ON PURPOSE. A green suite must not be
    readable as "the reported symptom is fixed" — it is not, and #915's own
    headline example (a read whose search string mentions a deletion) is in
    here. The day one of these starts passing, the assertion fails and someone
    has to come update the story deliberately.

    - **Mechanism 2 — the path ladders.** ``zeroAccessPaths`` /
      ``readOnlyPaths`` / ``noDeletePaths`` iterate the RAW haystacks and have
      no ``anchored`` concept at all, so a read whose SEARCH STRING mentions a
      deletion is refused when the directory being read is protected.
    - **Mechanism 3 — masking is keyed on WHITESPACE.** ``masked_subcommands``
      blanks a fully-quoted token only when it contains whitespace, so a
      single-word quoted payload is never masked.

    Both are #922. They are deliberately out of scope here because this is a
    guard-WEAKENING change and the governing question is *what else did this
    just permit* — the path ladders gate reads of secrets, not just deletions,
    so widening the blast radius to three ladder steps in the same diff is the
    wrong trade.
    """

    # These rows assert on ``~``-form protected paths, so they need $HOME to look
    # like a real home — the same reason and the same marker as
    # test_damage_control_bypass.py. The #893 redirect points HOME at a pytest
    # tmp dir, which on Linux is under ``/tmp``, which core.yaml allowlists
    # ``allow: all`` — and an allowlist entry OUTRANKS both noDeletePaths and
    # protectedControlPlane (``check_command`` consults it inside each ladder).
    # So the rows resolved to ``allow`` on CI for a reason with nothing to do
    # with the mechanism. macOS temp is /private/var/folders, not allowlisted,
    # which is why a local run could not see it.
    #
    # NOT a skip: conftest honours the marker by handing back the real paths, so
    # the rows run and assert for real. Reads only; the session-scoped audit
    # backstop still fails the run on any write.
    pytestmark = pytest.mark.real_agentwire_home

    # mechanism 2 — the literal incident from #915's body, in its literal form
    LADDER_CASES = [
        ('grep -rn "rm -rf" ~/.agentwire/', "noDeletePath"),
        ('rg "rm -rf" ~/.claude/hooks/', "protectedControlPlane"),
        # `.git/` is a RELATIVE noDeletePath — no $HOME and no tmp prefix, so
        # this row holds regardless of how the suite redirects HOME.
        ('grep -rn "rm -rf" .git/', "noDeletePath"),
    ]

    # The same three ladder steps against explicit non-allowlisted literals, so
    # at least one row per step is independent of $HOME *and* of the allowlist.
    EXPLICIT_LADDER_CASES = [
        ("noDeletePaths", 'grep -rn "rm -rf" /srv/protected/'),
        ("readOnlyPaths", 'grep -rn "rm -rf" /srv/readonly/'),
        ("zeroAccessPaths", 'grep -rn "rm -rf" /srv/secret/'),
    ]

    # mechanism 3 — a one-word payload in the exact reported carrier
    WHITESPACE_CASES = [
        'agentwire msg send --to orch --kind done "rmdir"',
        'ssh prod "reboot"',
        'mongosh --eval "db.dropDatabase()"',
        # masking works on trailing comments no better than on one-word tokens
        "true  # note: git reset --hard was blocked",
    ]

    @pytest.mark.parametrize("command,via", LADDER_CASES)
    def test_path_ladder_still_refuses_a_read_that_mentions_a_deletion(
        self, bash_hook, bundled_config, command, via
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] in REFUSED, (
            f"{command!r} now passes — mechanism 2 looks fixed. If that is "
            f"intentional (#922), update this test and #915's story."
        )
        assert via in str(result.get("pattern", "")), (
            f"expected refusal via {via}, got {result.get('pattern')} — the "
            f"mechanism changed even though the verdict did not"
        )

    @pytest.mark.parametrize("ladder,command", EXPLICIT_LADDER_CASES)
    def test_each_ladder_step_refuses_a_read_on_its_search_string(
        self, bash_hook, ladder, command
    ):
        """One row per ladder step, against literal paths.

        Three steps, three different predicates — which is why one
        anchored-style flag cannot serve them and why they are #922 rather than
        an extension of this PR.
        """
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
        assert result["decision"] in REFUSED, (
            f"{ladder} no longer refuses {command!r} — mechanism 2 looks fixed "
            f"(#922). The ladder has no `anchored` concept; if it grew one, "
            f"update this row and #915's story."
        )

    def test_protected_control_plane_step_refuses_a_read(self, bash_hook):
        """Ladder step 0, with an EMPTY allowlist passed explicitly.

        Deliberately not done with a tmp HOME. `tmp_path` is under `/tmp` on
        Linux, which core.yaml allowlists `allow: all`, and the allowlist
        outranks the protected control plane — so a tmp HOME is the one thing
        guaranteed to make this row lie. Passing `allowed=[]` states the
        precondition instead of depending on an environment to supply it.
        """
        command = 'rg "rm -rf" ~/.claude/hooks/'
        blocked, reason = bash_hook.check_protected_command(command, [])
        assert blocked, (
            f"{command!r} no longer refused by the protected control plane — "
            f"step 0 of mechanism 2 looks fixed (#922)"
        )
        assert reason

    def test_allowlist_outranks_the_ladders(self, bash_hook):
        """Pin the trap itself, since it has now cost two sessions a red CI.

        An `allow: all` entry short-circuits BOTH ladders. That is why a tmp
        HOME under /tmp turns these rows green-to-allow, and it is worth an
        assertion rather than a comment.
        """
        cfg = {
            "bashToolPatterns": [],
            "zeroAccessPaths": [],
            "readOnlyPaths": [],
            "noDeletePaths": ["/srv/protected/"],
            "allowedPaths": [],
            "safety": dict(SAFETY),
        }
        command = 'grep -rn "rm -rf" /srv/protected/'
        assert bash_hook.check_command(command, cfg)["decision"] in REFUSED
        cfg["allowedPaths"] = [{"path": "/srv/*", "allow": "all"}]
        assert bash_hook.check_command(command, cfg)["decision"] == "allow", (
            "an allow:all entry no longer outranks noDeletePaths — the trap "
            "that made this file's CI red has changed shape"
        )

    @pytest.mark.parametrize("command", WHITESPACE_CASES)
    def test_single_word_payload_is_never_masked(
        self, bash_hook, bundled_config, command
    ):
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] in REFUSED, (
            f"{command!r} now passes — mechanism 3 looks fixed (#922)."
        )

    def test_the_masking_control_case_does_pass(self, bash_hook, bundled_config):
        """Two words, so it IS masked — the boundary mechanism 3 sits on."""
        assert bash_hook.check_command(
            'echo "terraform destroy"', bundled_config
        )["decision"] == "allow"


class TestSshWrappedCoverageReduction:
    """DISCLOSURE: anchoring demotes ~125 of 151 ssh-wrapped dangerous forms
    from refused to allowed, and this PR does not fix that. #924.

    Read this before reading TestSshWrappedCommandsStillRefused, whose corpus
    is EXACTLY the 12 forms ``remote.yaml`` protects — a corpus shaped to the
    survivors, which proves nothing about the rest. That is the fixture-shaped
    blind spot this file's own docstring calls out in the #675 test, reproduced
    one level up.

    Why it is a demotion rather than a regression in design: the pre-change
    blocking of `ssh prod "<anything dangerous>"` was INCIDENTAL — it came from
    the same match-anywhere behaviour that IS the bug. ``remote.yaml``'s 12
    rules are the *intentional* ssh coverage and they are untouched. But the
    coverage was real while it lasted, so it is stated, not buried.

    The fix is #924 — extend the ``_SHELL_NAMES`` rescan to
    ``ssh <host> "<payload>"`` so every rule applies to the payload, after
    which ``remote.yaml`` becomes DELETABLE. It lives in ``masked_subcommands``,
    which the orchestrator assigned to #913's plumbing, not here. Widening
    ``remote.yaml`` to ~120 ssh twins is explicitly NOT the answer: it
    duplicates the whole rule set, which is the second-thing-to-keep-in-sync
    this PR exists to avoid.
    """

    # Representative sample of the demoted class, one per rule family.
    DEMOTED = [
        'ssh prod "terraform destroy"',
        'ssh prod "gh repo delete owner/repo"',
        'ssh prod "aws ec2 terminate-instances --instance-ids i-1"',
        'ssh prod "gcloud projects delete my-proj"',
        'ssh prod "kubectl delete namespace prod"',
        'ssh prod "helm uninstall release"',
        'ssh prod "docker volume rm pgdata"',
        'ssh prod "npm unpublish my-pkg"',
        'ssh prod "chmod 777 /srv"',
        'ssh prod "tmux kill-server"',
        'ssh prod "prisma migrate reset"',
        'ssh prod "history -c"',
    ]

    @pytest.mark.parametrize("command", DEMOTED)
    def test_ssh_wrapped_form_is_knowingly_allowed(
        self, bash_hook, bundled_config, command
    ):
        """Expected-fail row. Green here means the gap is still open (#924)."""
        result = bash_hook.check_command(command, bundled_config)
        assert result["decision"] not in REFUSED, (
            f"{command!r} is now refused — #924 may have landed. If so, delete "
            f"this row and update the disclosure in the PR body and the wiki."
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
