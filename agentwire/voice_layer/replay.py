"""The #1179 replay: the failed owner exchange, as a fixture and a judge.

The first real owner session asked "what's left to work on in agentwire-dev".
The buddy fired five fleet reads in parallel, spoke after the FIRST result,
called the other four "still running" (they had returned in about a second),
then refused to re-run them because they were "already running". Two defects
composed there: the client created a response per tool output, and the persona
narrated instead of answering.

This module holds that exchange in one place so two consumers can share it:

- ``tests/unit/test_voice_tool_batch.py`` drives the REAL client JS through the
  batch and asserts the protocol half — every output lands before the one
  ``response.create``. Offline, under a second.
- :func:`main` replays the utterance against the live Realtime API with the
  real instructions and tool definitions, answers every call from
  :data:`CANNED_OUTPUTS`, and lets :func:`judge` score the model half — one
  batched call round, one short answer. Needs ``OPENAI_API_KEY`` and costs a
  few cents in text mode, so it is never part of the test suite::

      uv run python -m agentwire.voice_layer.replay

  Exit 0 means the replay passed; exit 1 prints what the judge objected to.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

from .project_read import UNTRUSTED_NOTE as _UNTRUSTED_NOTE

#: What the owner said.
UTTERANCE = "What's left to work on in agentwire-dev?"

#: The batch the buddy fired (from the transcript), in its order.
FAILED_BATCH = (
    "fleet_sessions",
    "fleet_worktrees",
    "fleet_dangling",
    "fleet_scheduler",
    "fleet_projects",
)

#: What it said after the first output landed — the narration this replay
#: exists to keep out. Paraphrased from the transcript summary in #1179.
FAILED_ANSWER = (
    "I've got the session list back — there are a handful of sessions running. "
    "The worktree, dangling PR, scheduler and project checks are still running, "
    "so I'll let you know once they come back. I can't check GitHub issues from "
    "here — a session in the project directory would need to query that for you."
)

#: Canned results, shaped like the CLI's JSON and small enough to read in one
#: breath. A call outside this table gets :data:`NO_DATA` — the replay judges
#: the shape of the turn, not the model's tool taste.
CANNED_OUTPUTS: dict[str, dict] = {
    "fleet_sessions": {
        "success": True,
        "sessions": [
            {"name": "agentwire", "role": "orchestrator", "activity": "idle"},
            {"name": "agentwire-dev-portal-fix", "role": "worker", "activity": "active",
             "parent": "agentwire"},
        ],
        "services": "4 services healthy",
    },
    "fleet_worktrees": {
        "success": True,
        "worktrees": [
            {"session": "agentwire-dev-portal-fix", "branch": "portal-fix", "state": "live"},
        ],
    },
    "fleet_dangling": {
        "success": True,
        "dangling": [
            {"session": "agentwire-dev-taskbar", "pr": 1177, "title": "taskbar restore"},
        ],
    },
    "fleet_scheduler": {"success": True, "due": [], "failed": [], "gated": ["weekly-stars"]},
    # project_lookup is keyed "<tool>:<lookup>" — see canned().
    "project_lookup:issues": {
        "success": True, "project": "agentwire-dev", "repo": "dotdevdotdev/agentwire-dev", "note": _UNTRUSTED_NOTE,
        "untrusted_content": [
            {"number": 1179, "title": "buddy: terse, batched, project-aware", "labels": []},
            {"number": 1168, "title": "portal: taskbar flicker on resize",
             "labels": [{"name": "area:bug"}]},
        ],
    },
    "project_lookup:prs": {
        "success": True, "project": "agentwire-dev", "repo": "dotdevdotdev/agentwire-dev", "note": _UNTRUSTED_NOTE,
        "untrusted_content": [{"number": 1177, "title": "taskbar restore", "isDraft": True}],
    },
    "fleet_projects": {
        "success": True,
        "projects": [{"name": "agentwire-dev", "path": "~/projects/agentwire-dev"}],
    },
}

NO_DATA = {"success": True, "note": "no data in this replay"}


def canned(name: str, arguments: str) -> dict:
    """The replay's answer to one call: by ``<tool>:<lookup>`` when the tool
    takes a lookup, else by tool name, else :data:`NO_DATA`."""
    try:
        lookup = json.loads(arguments or "{}").get("lookup")
    except (json.JSONDecodeError, AttributeError):
        lookup = None
    return CANNED_OUTPUTS.get(f"{name}:{lookup}") or CANNED_OUTPUTS.get(name) or NO_DATA

#: A short spoken answer. Two sentences of ordinary speech is ~30-40 words.
MAX_ANSWER_WORDS = 45

#: Phrases that narrate work in flight. Every output is in before the batch's
#: one response is created, so any of these is false by construction.
_PENDING_RX = re.compile(
    r"\b(still (running|loading|coming back)|once (they|those|it) "
    r"(come|comes|finish|finishes|return|returns)|already running)\b",
    re.IGNORECASE,
)


def judge(turns: list[dict]) -> list[str]:
    """Score a replayed exchange. Returns the problems; empty means it passed.

    *turns* is the model's responses in order, each
    ``{"calls": [tool names], "said": spoken text}``. Passing means: the FIRST
    response is one batch of two or more calls and says nothing yet; exactly
    one later response answers, carries no further calls, stays under
    :data:`MAX_ANSWER_WORDS`, and narrates nothing as pending.
    """
    problems: list[str] = []
    if not turns:
        return ["no response at all"]
    first = turns[0]
    if len(first["calls"]) < 2:
        problems.append(f"first response batched {len(first['calls'])} call(s), want 2+")
    if first["said"].strip():
        problems.append(f"spoke before the results were in: {first['said']!r}")
    rest = turns[1:]
    call_rounds = [t for t in rest if t["calls"]]
    if call_rounds:
        problems.append(
            f"{len(call_rounds)} further call round(s) after the batch: "
            + ", ".join(",".join(t["calls"]) for t in call_rounds)
        )
    answers = [t["said"] for t in rest if t["said"].strip()]
    if len(answers) != 1:
        problems.append(f"{len(answers)} spoken answers, want exactly 1")
    for said in answers:
        words = len(said.split())
        if words > MAX_ANSWER_WORDS:
            problems.append(f"answer is {words} words, want <= {MAX_ANSWER_WORDS}: {said!r}")
        if _PENDING_RX.search(said):
            problems.append(f"narrates work as pending: {said!r}")
    return problems


# ---------------------------------------------------------------------------
# The live replay
# ---------------------------------------------------------------------------

_WS_URL = "wss://api.openai.com/v1/realtime?model={model}"
_MAX_ROUNDS = 4


async def _next_done(ws) -> dict:
    """Read events until a ``response.done``; raise on a server error."""
    async for msg in ws:
        event = json.loads(msg.data)
        if event.get("type") == "error":
            raise RuntimeError(json.dumps(event.get("error")))
        if event.get("type") == "response.done":
            return event["response"]
    raise RuntimeError("socket closed before response.done")


def _turn(response: dict) -> tuple[dict, list[dict]]:
    calls = [i for i in response.get("output", []) if i.get("type") == "function_call"]
    said = " ".join(
        part.get("text") or part.get("transcript") or ""
        for item in response.get("output", []) if item.get("type") == "message"
        for part in item.get("content", [])
    ).strip()
    return {"calls": [c["name"] for c in calls], "said": said}, calls


async def replay(api_key: str) -> list[dict]:
    """Run :data:`UTTERANCE` once against the live API; return the turns.

    Answers calls exactly as the client does since #1179: every output
    submitted, then ONE ``response.create``.
    """
    import aiohttp

    from . import instructions, realtime, tools

    turns: list[dict] = []
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(
            _WS_URL.format(model=realtime.DEFAULT_MODEL),
            headers={"Authorization": f"Bearer {api_key}"},
        ) as ws:
            await ws.send_json({"type": "session.update", "session": {
                "type": "realtime",
                "instructions": instructions.build_instructions(),
                "tools": tools.realtime_tool_defs(),
                "output_modalities": ["text"],
            }})
            await ws.send_json({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": UTTERANCE}],
            }})
            await ws.send_json({"type": "response.create"})
            for _ in range(_MAX_ROUNDS):
                turn, calls = _turn(await _next_done(ws))
                turns.append(turn)
                if not calls:
                    break
                for c in calls:
                    await ws.send_json({"type": "conversation.item.create", "item": {
                        "type": "function_call_output", "call_id": c["call_id"],
                        "output": json.dumps(canned(c["name"], c.get("arguments", ""))),
                    }})
                await ws.send_json({"type": "response.create"})
    return turns


def main() -> int:
    from pathlib import Path

    from dotenv import load_dotenv

    # Same load the CLI entry point does — the one blessed secrets file.
    load_dotenv(Path.home() / ".agentwire" / ".env")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY is not set (~/.agentwire/.env)", file=sys.stderr)
        return 2
    turns = asyncio.run(replay(api_key))
    for i, t in enumerate(turns):
        print(f"turn {i}: calls={t['calls']} said={t['said']!r}")
    problems = judge(turns)
    for p in problems:
        print(f"FAIL: {p}")
    print("PASS" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
