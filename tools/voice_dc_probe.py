#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Empirical damage-control probe for the voice layer's write path.

Answers one question with measurements rather than with an argument about
``settings.json``: **is the buddy's write path guarded?**

Four probes, because either direction alone is consistent with the wrong
conclusion:

  A  the hook, fed a synthetic PreToolUse payload for a ``msg send`` whose
     BODY discusses a guarded operation                        → expect BLOCK
  B  the identical argv via ``subprocess.run`` from plain Python — exactly what
     ``mcp_core.run_agentwire_cmd`` does from the bridge        → expect exit 0
  C  control: an innocuous ``msg send`` through the hook        → expect ALLOW
  D  control: a genuinely destructive command through the hook  → expect BLOCK

Without C, probe A does not distinguish "the rule fired" from "the hook refused
for some other reason". Without D, it does not distinguish "the rules work" from
"everything is blocked".

Two traps this script exists to avoid, both of which cost real time:

1. **Launching the probe through Claude Code's Bash tool blocks the probe.**
   The hook matches the guarded prose in the command line (#915), so the probe
   never runs. Hence: the pattern is assembled from FRAGMENTS here, in a file,
   and never appears on a command line.
2. **Running the hook under bare ``python3`` returns exit 2 with
   "pyyaml unavailable — cannot load rules".** That is the FAIL-CLOSED path, not
   a rule firing, and exit 2 is the same code a real block returns. Recording it
   as "the rule fired" is a false positive of exactly the
   verified-against-the-wrong-thing class this repo keeps hitting. So the hook
   is invoked through its own ``uv run --script`` shebang, and any result whose
   reason mentions pyyaml is reported as INVALID rather than as a block.

Per #916 the rule set and the tooldef set drift independently, so both are
fingerprinted and printed: a safety claim that does not name what it was
measured against is not a claim.

Usage:  ./tools/voice_dc_probe.py [--json]
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
HOOK = HOME / ".claude" / "hooks" / "damage-control" / "bash-tool-damage-control.py"
HOOK_ALT = HOME / ".agentwire" / "hooks" / "damage-control" / "bash-tool-damage-control.py"
RULES_DIR = HOME / ".agentwire" / "damage-control"
TOOLDEFS_DIR = HOME / ".agentwire" / "tooldefs"
SETTINGS = HOME / ".claude" / "settings.json"

#: The probe recipient. Throwaway, and purged at the end.
TARGET = "voice-dc-probe-scratch"

# The guarded-operation prose, assembled from fragments so that neither this
# file's own command line nor any log line contains the pattern verbatim.
_FRAG = ("r", "m", " ", "-", "r", "f")
GUARDED_PROSE = (
    "reporting back: the cleanup step was refused because it wanted to run "
    + "".join(_FRAG)
    + " on the build directory, so I left it alone"
)
INNOCUOUS_PROSE = "reporting back: all green, the branch is ready for review"


def hook_path() -> Path:
    for candidate in (HOOK, HOOK_ALT):
        if candidate.exists():
            return candidate
    raise SystemExit(f"no damage-control hook found at {HOOK} or {HOOK_ALT}")


def fingerprint(directory: Path) -> dict:
    """A stable identity for a policy directory, so a claim can name it."""
    if not directory.exists():
        return {"path": str(directory), "present": False}
    files = sorted(p for p in directory.glob("*.yaml") if p.is_file())
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return {
        "path": str(directory),
        "present": True,
        "files": [p.name for p in files],
        "count": len(files),
        "sha256": digest.hexdigest()[:16],
    }


def run_hook(command: str) -> dict:
    """Feed the hook a synthetic PreToolUse payload THROUGH ITS OWN SHEBANG."""
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "permission_mode": "default",
        }
    )
    # Executed directly so the `#!/usr/bin/env -S uv run --script` line applies
    # and the script gets its declared dependencies. Invoking `python3 <hook>`
    # instead is trap 2 in the module docstring.
    result = subprocess.run(
        [str(hook_path())],
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
    )
    stderr = result.stderr.strip()
    verdict = "ALLOW" if result.returncode == 0 else "BLOCK"
    invalid = "pyyaml" in stderr.lower()
    if invalid:
        verdict = "INVALID (fail-closed, not a rule)"
    reason = ""
    for line in stderr.splitlines():
        if line.startswith("SECURITY:"):
            reason = line.split(":", 2)[-1].strip()
            break
    return {
        "verdict": verdict,
        "exit": result.returncode,
        "reason": reason,
        "valid": not invalid,
    }


def run_direct(argv: list[str]) -> dict:
    """The bridge's own path: subprocess.run, list argv, no shell, no tool call."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit": -1, "stdout": "", "stderr": str(exc)}
    return {
        "exit": result.returncode,
        "stdout": result.stdout.strip()[:400],
        "stderr": result.stderr.strip()[:400],
    }


def purge() -> None:
    """Leave nothing queued. The probe writes a real message on purpose."""
    subprocess.run(
        ["agentwire", "msg", "purge", TARGET],
        capture_output=True,
        text=True,
        timeout=60,
    )
    box = HOME / ".agentwire" / "inbox" / TARGET
    if box.exists():
        # Explicit paths only — never a recursive delete.
        for entry in list(box.glob("*.json")):
            entry.unlink()
        for sub in ("dead", "ingest", "sent"):
            subdir = box / sub
            if subdir.exists():
                for entry in list(subdir.glob("*.json")):
                    entry.unlink()
                subdir.rmdir()
        for stray in list(box.glob(".*")):
            if stray.is_file():
                stray.unlink()
        try:
            box.rmdir()
        except OSError:
            pass


def main() -> int:
    as_json = "--json" in sys.argv
    guarded_cmd = f"agentwire msg send --to {TARGET} --kind note '{GUARDED_PROSE}'"
    innocuous_cmd = f"agentwire msg send --to {TARGET} --kind note '{INNOCUOUS_PROSE}'"
    destructive_cmd = "".join(_FRAG) + " /tmp/some-build-dir"

    report = {
        "measured_against": {
            "rules": fingerprint(RULES_DIR),
            "tooldefs": fingerprint(TOOLDEFS_DIR),
            "hook": str(hook_path()),
            "settings": str(SETTINGS),
        },
        "probes": {},
    }

    report["probes"]["A_hook_msg_send_guarded_prose"] = run_hook(guarded_cmd)
    report["probes"]["C_control_hook_msg_send_innocuous"] = run_hook(innocuous_cmd)
    report["probes"]["D_control_hook_destructive_command"] = run_hook(destructive_cmd)

    # Probe B: the bridge's actual path. A real send, then purged.
    argv = [
        "agentwire", "msg", "send",
        "--to", TARGET, "--from", "voice-dc-probe",
        "--kind", "note", GUARDED_PROSE,
    ]
    report["probes"]["B_bridge_subprocess_same_prose"] = run_direct(argv)
    purge()

    # Whether the MCP tool is in the matcher list at all (v2's correction).
    try:
        settings = json.loads(SETTINGS.read_text())
        matchers = [
            entry.get("matcher", "")
            for entry in settings.get("hooks", {}).get("PreToolUse", [])
        ]
    except (OSError, ValueError):
        matchers = []
    report["pretooluse_matchers"] = matchers
    report["msg_send_mcp_tool_is_matched"] = any(
        "msg_send" in m for m in matchers
    )

    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    rules, tooldefs = report["measured_against"]["rules"], report["measured_against"]["tooldefs"]
    print("Measured against")
    print(f"  rules:    {rules['path']} ({rules.get('count')} files, sha {rules.get('sha256')})")
    print(f"  tooldefs: {tooldefs['path']} ({tooldefs.get('count')} files, sha {tooldefs.get('sha256')})")
    print(f"  hook:     {report['measured_against']['hook']}")
    print()
    for name, probe in report["probes"].items():
        if "verdict" in probe:
            print(f"  {probe['verdict']:<8} {name}")
            if probe["reason"]:
                print(f"           <- {probe['reason']}")
        else:
            print(f"  exit {probe['exit']:<3} {name}")
            if probe["stdout"]:
                print(f"           -> {probe['stdout'].splitlines()[0]}")
    print()
    print(f"PreToolUse matchers: {', '.join(report['pretooluse_matchers'])}")
    print(f"mcp__agentwire__msg_send matched: {report['msg_send_mcp_tool_is_matched']}")

    a = report["probes"]["A_hook_msg_send_guarded_prose"]
    if not a["valid"]:
        print("\nPROBE A INVALID — the hook failed closed rather than matching a rule.")
        return 2
    return 0


if __name__ == "__main__":
    os.environ.setdefault("AGENTWIRE_UNATTENDED", "")
    sys.exit(main())
