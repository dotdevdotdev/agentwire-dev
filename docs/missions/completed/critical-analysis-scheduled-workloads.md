⏺ Critical Analysis: Scheduled Workloads Mission

Strengths

1. Clear problem definition. The five failure modes (session doesn't exist, crashed,
   busy, no data gathering, no result handling) are real problems that affect
   cron-based agent workflows.

2. Good separation of concerns. The ensure command handles session lifecycle; tasks
   handle workflow definition. These are orthogonal and composable.

3. Non-goals are well-scoped. "No built-in scheduler" is the right call - leverage
   existing cron/launchd. "No approval workflows" keeps it simple.

4. Test cases are concrete. The morning briefing example is a legitimate use case
   that validates the design.

---

Issues & Gaps

1. Idle detection is unreliable for "completion"

The spec says "Wait for session to go idle (completion)" but idle ≠ complete. An
agent might:

- Pause to think (goes idle briefly)
- Wait for user input on an ambiguous instruction
- Hit an error and give up

Missing: How do you distinguish "task finished" from "agent confused and stopped"?
Need success/failure signals, not just idle timeout.

Recommendation: Add explicit completion markers. Either:

- Agent writes to a sentinel file when done
- Last N lines of output parsed for "TASK COMPLETE" / "TASK FAILED"
- Timeout + output heuristics

---

2. pre command failure handling is underspecified

Spec says "Fail fast on command failure" but doesn't define:

- What exit code counts as failure?
- What happens to the session that was just created?
- Is there a pre.on_failure hook?

Example problem:
pre:
api_data: curl -s https://api.example.com/data # API is down
The curl exits 0 but returns empty string. The prompt gets {{ api_data }} = empty.
Agent runs anyway with garbage input.

Recommendation: Add pre.required: true option to validate non-empty output, or
pre.validate: "jq . > /dev/null" for structured validation.

---

3. Variable collision with shell syntax

{{ variable }} is fine, but:
pre:
data: echo "{{ something }}" # This runs BEFORE templating

The pre commands run first to produce variables. But the spec shows {{ }} syntax in
pre commands too. This is circular.

Clarify: Pre commands cannot use {{ }} variables (they produce them). Only prompt:
and post: consume variables.

---

4. No retry logic

If the session crashes mid-task, there's no recovery. The task just fails.

Consider adding:
tasks:
critical-job:
retries: 2
retry_delay: 30

---

5. Output file ownership ambiguity

output:
file: .agentwire/results/briefing.md

Who creates this file? The agent writes to it, but:

- Does ensure create the parent directory?
- What if agent doesn't write there? (Instruction following isn't 100%)
- Is the path relative to project or session cwd?

Recommendation: ensure should create the directory, and output.file should be
relative to project root (where .agentwire.yml lives).

---

6. No concurrency control

Two cron jobs running ensure for the same session simultaneously:

1. Both check if session exists → yes
2. Both check if idle → yes
3. Both send prompts → race condition

Missing: Locking mechanism or queue semantics.

Options:

- Flock-based lock per session
- --queue flag to append to session's work queue
- Advisory: document that users shouldn't schedule overlapping jobs

---

7. Webhook security

notify: webhook https://slack.com/webhook/SECRET

The secret ends up in .agentwire.yml, which might be committed to git.

Recommendation: Support environment variable expansion: webhook ${SLACK_WEBHOOK_URL}

---

8. MCP tool ensure() signature is confusing

def ensure(session: str, prompt: str | None = None, task: str | None = None)

If both prompt and task are provided, what happens? Error? Task wins? Prompt
appended?

Recommendation: Make them mutually exclusive with clear error message.

---

Minor Observations

- Phase ordering: Phase 4 (Templating) should probably come before Phase 3 (Pre
  Phase) in terms of implementation, since you need the templating engine to be
  defined first for testing pre outputs.
- --dry-run scope: Does it skip pre commands too? They might have side effects
  (writing temp files). Clarify what "dry run" means for each phase.
- No task inheritance: Can't define a base task and override. Low priority but might
  be useful for similar tasks with slight variations.

---

Overall Assessment

The design is solid for the 80% case. Main risks are:

1. Idle-as-completion is fragile - needs explicit success/failure signaling
2. No concurrency handling - will bite power users
3. Pre command validation - garbage-in-garbage-out without it

I'd prioritize addressing the completion detection problem before Phase 5 (Post
Phase), since everything downstream depends on knowing whether the task actually
succeeded.
