"""The host-supplied half of the damage-control engine (#1209 M3, section 4.2).

The engine knows what is dangerous in general; a HOST (an application that embeds it)
adds what is dangerous *around itself*: its own config and control-plane files, the
commands that must never run through an agent, the env vars its scheduler stamps, the
rule ids it is willing to grant unattended. A ``Profile`` is that whole surface, as
plain data, so the engine has no host names in its logic.

``Profile`` only ever ADDS. The generic control plane (the project ``.damagecontrol.yml``,
``~/.claude/settings.json``, ``~/.claude/hooks/*``, the host's policy file) is a kernel
that ``install_profile`` (in ``_core.py``) puts first and a profile cannot remove or
reorder: the control-plane and protected-command checks run before the kill switch and
nothing a profile supplies can loosen them.

A profile reaches a hook as DATA: the host's installer writes it with ``dump_profile`` to a
protected file and the hook's command line names it (``--profile``), so the hook never imports
the host. Standard library only; plain ``NamedTuple``s rather than dataclasses.
"""

import json
from pathlib import Path
from typing import Any, Dict, NamedTuple, Tuple


class GroupSpec(NamedTuple):
    """One rule group or opt-in pack: which bundled files it owns (#1141)."""

    name: str
    kind: str  # "group" | "pack"
    description: str
    rules: Tuple[str, ...] = ()
    tooldefs: Tuple[str, ...] = ()
    kernel: bool = False
    binaries: Tuple[str, ...] = ()  # vendor CLIs whose presence suggests a pack
    # Rule files shipped by a host layer (``Profile.extra_rules_dirs``). Owned by the
    # SAME entry as the generic file they were split from, so an opt-out or kernel pin
    # still covers every entry that group covered before the split.
    layer_rules: Tuple[str, ...] = ()

    def files(self, section: str) -> Tuple[str, ...]:
        """Every bundled file this entry owns in ``section`` ("rules" | "tooldefs")."""
        return self.rules + self.layer_rules if section == "rules" else self.tooldefs


class McpCommandTemplate(NamedTuple):
    """How an outbound MCP tool maps onto the shell command it runs under the hood.

    The synthesized command must match the same rule the Bash shell-out matches. The
    body is omitted on purpose (privacy, audit clarity): rules key on the verb.
    ``argv`` is the fixed prefix; each ``flags`` entry is ``(tool_input key, flag, repeat)``:
    an empty value omits the flag; ``repeat`` lets a list value emit the flag once per item,
    otherwise the value is rendered as one ``str()``.
    """

    tool: str  # short tool name, e.g. "email_send" (the part after the last "__")
    argv: Tuple[str, ...]
    flags: Tuple[Tuple[str, str, bool], ...] = ()


class AuditSpec(NamedTuple):
    """How the host's sessions are named in an audit row, and where the rows live."""

    # Under the config home (``Profile.config_dir``).
    log_dir: str = "logs/damage-control"
    # Env vars the host stamps on a session; empty = the host has none.
    session_env: str = ""
    agent_env: str = ""
    # Directory under the config home of ``<session name>/metadata.json`` records, each
    # carrying a ``conversation_ids`` chain; the entry naming the hook's conversation id
    # names the session. Empty = the host keeps no such records.
    sessions_dir: str = ""


class Profile(NamedTuple):
    """Everything a host adds to the generic engine. Defaults are the generic engine."""

    name: str = "generic"

    # --- config home ------------------------------------------------------
    # Where the host keeps its protected policy file (``damagecontrol.yml``) and its
    # rule/tooldef overrides. ``config_dir_env`` (if set in the environment) relocates
    # the override dirs only — never the policy file, which is always protected.
    config_dir: str = "~/.damage-control"
    config_dir_env: str = "DAMAGE_CONTROL_HOME"

    # --- protected control plane (additive) -------------------------------
    protected_paths: Tuple[str, ...] = ()
    # Names a program may use for a control-plane location it builds at run time.
    control_plane_words: Tuple[str, ...] = ()
    # Extra regex alternatives for directories agents write by design (matched whole-segment).
    benign_path_patterns: Tuple[str, ...] = ()
    # Fixed stand-in for ``$HOME`` when judging what a ``~``-anchored entry covers.
    home_alias: str = "/home/damage-control-canonical"

    # --- protected commands (additive; no allowlist, no escape hatch, no kill switch) ---
    protected_commands: Tuple[Tuple[str, str], ...] = ()

    # --- unattended / headless --------------------------------------------
    # The mechanism and scope evaluation live in the engine; the host names the env vars
    # its scheduler stamps and the rule ids it grants by default (the engine ships none).
    unattended_env: str = "DAMAGE_CONTROL_UNATTENDED"
    unattended_allow_env: str = "DAMAGE_CONTROL_UNATTENDED_ALLOW"
    default_unattended_allow: Tuple[str, ...] = ()
    # First half of the sentence telling a blocked unattended run how to get a grant;
    # ``{rule_id}`` is filled in. Empty = say nothing.
    unattended_grant_hint: str = ""
    # argv run detached when an unattended action is blocked, with the event as JSON on stdin:
    # ``{"event": "unattended_block", "reason", "rule_id", "command"}``. Empty = no notification.
    unattended_block_notifier: Tuple[str, ...] = ()

    # --- audit ------------------------------------------------------------
    audit: AuditSpec = AuditSpec()

    # --- extra rules ------------------------------------------------------
    # Absolute directories of rule files (JSON, or YAML a human wrote) merged after the generic rules.
    extra_rules_dirs: Tuple[str, ...] = ()

    # --- MCP --------------------------------------------------------------
    mcp_command_templates: Tuple[McpCommandTemplate, ...] = ()

    # --- groups / packs ---------------------------------------------------
    groups: Tuple[GroupSpec, ...] = ()
    # ``{existing group name: layer rule files}`` the host adds to generic groups.
    group_layer_rules: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    default_packs: Tuple[str, ...] = ()

    def layer_rules_for(self, group: str) -> Tuple[str, ...]:
        return dict(self.group_layer_rules).get(group, ())

    def template_for(self, tool_name: str):
        short = tool_name.split("__")[-1]
        for t in self.mcp_command_templates:
            if t.tool == short:
                return t
        return None


GENERIC = Profile()


class ProfileError(ValueError):
    """A profile manifest that cannot be read as a profile. Never answered with the generic one."""


def dump_profile(profile: Profile) -> str:
    """``profile`` as the JSON a hook loads (tuples become lists; fields keep their names)."""
    return json.dumps(profile._asdict(), indent=1, ensure_ascii=False) + "\n"


def _tuple(value: Any) -> Any:
    """A JSON list as the tuple the profile's fields are, recursively."""
    return tuple(_tuple(v) for v in value) if isinstance(value, list) else value


def load_profile(path: Path) -> Profile:
    """Read a manifest written by :func:`dump_profile`. Raises :class:`ProfileError` on anything off."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise ProfileError(f"cannot read profile {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ProfileError(f"profile {path} is not a JSON object")
    unknown = sorted(set(raw) - set(Profile._fields))
    if unknown:
        raise ProfileError(f"profile {path} has unknown fields: {unknown}")
    fields: Dict[str, Any] = {k: _tuple(v) for k, v in raw.items()}
    fields["audit"] = AuditSpec(*fields.get("audit", ()))
    fields["groups"] = tuple(GroupSpec(*g) for g in fields.get("groups", ()))
    fields["mcp_command_templates"] = tuple(
        McpCommandTemplate(*t) for t in fields.get("mcp_command_templates", ()))
    try:
        return Profile(**fields)
    except TypeError as e:
        raise ProfileError(f"profile {path} is malformed: {e}") from e


__all__ = ["AuditSpec", "GENERIC", "GroupSpec", "McpCommandTemplate", "Profile", "ProfileError", "dump_profile", "load_profile"]
