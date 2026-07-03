"""CLI for reading and (allowlist-gated) writing ~/.agentwire/config.yaml.

``config.yaml`` is hook-protected control plane (#466): agents cannot edit the
file directly because execution-plane fields (``services.*.healthcheck``,
``executables.*``) feed agentwire's own shell. ``agentwire config`` is the
blessed narrow write path (#670): the CLI — not the hook — gatekeeps writes to
an explicit in-code allowlist of benign preference fields, each with a typed
validator so nothing command-shaped can be smuggled through an allowed key.

The allowlist lives in this module ONLY. It must never be config-driven —
loosening it is a host-side code change, preserving the #466 invariant.
"""

from __future__ import annotations

import re

import yaml

from .core import CONFIG_DIR, _atomic_write, _output_json, _output_result

# Dotted-key prefixes whose values reach a shell, subprocess, or agent command
# line. No allowlist entry may ever live under these — `doctor` asserts it.
EXECUTION_PLANE_PREFIXES = (
    "services.",
    "executables.",
    "agent.",
    "pi.",
    "hooks.",
    "dev.",
)

# Value shapes that are command-shaped regardless of section.
_EXECUTION_PLANE_LEAVES = ("command", "healthcheck", "binary", "script", "shell")


# --- typed validators: return the canonical value or raise ValueError -------

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def _validate_email(value: str):
    if not _EMAIL_RE.match(value):
        raise ValueError(f"not a valid email address: {value!r}")
    return value


def _validate_from_address(value: str):
    """Bare email, or RFC-ish display form: ``Name <email@host>``."""
    m = re.match(r"^([^<>]+)<([^<>]+)>$", value)
    if m:
        _validate_email(m.group(2).strip())
        return value.strip()
    return _validate_email(value.strip())


def _validate_enum(*choices: str):
    def check(value: str):
        if value not in choices:
            raise ValueError(f"must be one of {', '.join(choices)} (got {value!r})")
        return value

    return check


_VOICE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_voice(value: str):
    if not _VOICE_RE.match(value):
        raise ValueError(
            f"voice names may only contain letters, digits, '.', '_', '-' (got {value!r})"
        )
    return value


def _validate_positive_number(value: str):
    try:
        num = float(value)
    except ValueError:
        raise ValueError(f"must be a number (got {value!r})") from None
    if num <= 0:
        raise ValueError(f"must be positive (got {value!r})")
    return int(num) if num == int(num) else num


def _validate_bool(value: str):
    lowered = str(value).strip().lower()
    if lowered in ("true", "yes", "on", "1"):
        return True
    if lowered in ("false", "no", "off", "0"):
        return False
    raise ValueError(f"must be a boolean (true/false, got {value!r})")


# --- the allowlist -----------------------------------------------------------
# key -> (validator, description). In-code only; never config-driven.

EDITABLE_KEYS: dict = {
    "channels.email.default_to": (
        _validate_email,
        "Default recipient for outbound email (email address)",
    ),
    "channels.email.from_address": (
        _validate_from_address,
        "Outbound email From (email or 'Name <email>')",
    ),
    "tts.backend": (
        _validate_enum("default", "custom"),
        "TTS tier (default|custom)",
    ),
    "tts.default_voice": (
        _validate_voice,
        "Default TTS voice name",
    ),
    "stt.backend": (
        _validate_enum("default", "auto", "moonshine", "whisper"),
        "STT backend (default|auto|moonshine|whisper)",
    ),
    "server.activity_threshold_seconds": (
        _validate_positive_number,
        "Seconds of quiet before a session counts as idle (positive number)",
    ),
    "channels.telegram.voice_replies": (
        _validate_bool,
        "Reply with voice notes on Telegram (bool)",
    ),
}


def execution_plane_violations() -> list:
    """Allowlist keys that touch the execution plane. Must be empty.

    Checked by ``agentwire doctor`` so a future edit to EDITABLE_KEYS can't
    silently reopen the #466 confused-deputy hole.
    """
    bad = []
    for key in EDITABLE_KEYS:
        if key.startswith(EXECUTION_PLANE_PREFIXES):
            bad.append(key)
        elif key.rsplit(".", 1)[-1] in _EXECUTION_PLANE_LEAVES:
            bad.append(key)
    return bad


# --- config file access ------------------------------------------------------


def _config_path():
    return CONFIG_DIR / "config.yaml"


def _load_raw() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_raw(data: dict) -> None:
    text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def validate(tmp_path):
        with open(tmp_path) as f:
            reparsed = yaml.safe_load(f)
        if reparsed != data:
            raise ValueError("config round-trip mismatch; write aborted")

    _atomic_write(_config_path(), text, validate=validate)


def _dig(data: dict, dotted: str):
    """Return (found, value) for a dotted key."""
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _put(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _flatten(data, prefix: str = "") -> dict:
    out: dict = {}
    if isinstance(data, dict) and data:
        for k, v in data.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    else:
        out[prefix.rstrip(".")] = data
    return out


def _refusal(key: str) -> str:
    if key.startswith(EXECUTION_PLANE_PREFIXES) or key.rsplit(".", 1)[-1] in _EXECUTION_PLANE_LEAVES:
        return (
            f"refused: {key!r} is execution-plane (its value can reach a shell). "
            "It is never agent-editable — edit ~/.agentwire/config.yaml host-side."
        )
    return (
        f"refused: {key!r} is not agent-editable. "
        "See `agentwire config list --editable` for the allowlist; "
        "other fields are edited host-side."
    )


# --- commands ----------------------------------------------------------------


def cmd_config_get(args) -> int:
    json_mode = getattr(args, "json", False)
    found, value = _dig(_load_raw(), args.key)
    if not found:
        return _output_result(False, json_mode, f"key not found: {args.key}", key=args.key,
                              error=f"key not found: {args.key}")
    if json_mode:
        _output_json({"success": True, "key": args.key, "value": value})
        return 0
    if isinstance(value, dict):
        print(yaml.safe_dump(value, default_flow_style=False, sort_keys=False).rstrip())
    else:
        print(value)
    return 0


def cmd_config_set(args) -> int:
    json_mode = getattr(args, "json", False)
    key, raw = args.key, args.value

    if key not in EDITABLE_KEYS:
        msg = _refusal(key)
        return _output_result(False, json_mode, msg, key=key, error=msg)

    validator, _desc = EDITABLE_KEYS[key]
    try:
        value = validator(raw)
    except ValueError as e:
        msg = f"invalid value for {key}: {e}"
        return _output_result(False, json_mode, msg, key=key, error=msg)

    data = _load_raw()
    _put(data, key, value)
    _save_raw(data)
    return _output_result(True, json_mode, f"{key} = {value}", key=key, value=value)


def cmd_config_list(args) -> int:
    json_mode = getattr(args, "json", False)
    data = _load_raw()

    if getattr(args, "editable", False):
        rows = []
        for key, (_validator, desc) in sorted(EDITABLE_KEYS.items()):
            found, value = _dig(data, key)
            rows.append({"key": key, "value": value if found else None,
                         "set": found, "description": desc})
        if json_mode:
            _output_json({"success": True, "editable": rows})
            return 0
        print("Agent-editable keys (agentwire config set <key> <value>):")
        for row in rows:
            current = repr(row["value"]) if row["set"] else "(unset)"
            print(f"  {row['key']} = {current}")
            print(f"      {row['description']}")
        return 0

    flat = _flatten(data)
    if json_mode:
        _output_json({"success": True, "config": flat})
        return 0
    for key in sorted(flat):
        marker = "*" if key in EDITABLE_KEYS else " "
        print(f"{marker} {key} = {flat[key]!r}")
    if flat:
        print("\n(* = agent-editable via `agentwire config set`)")
    return 0


def register_config_parser(subparsers) -> None:
    """Register the `config` command group (#670)."""
    parser = subparsers.add_parser(
        "config",
        help="Read config.yaml; write allowlisted preference fields",
        description=(
            "Field-allowlisted access to the hook-protected ~/.agentwire/config.yaml. "
            "`get`/`list` read anything; `set` writes only benign, typed preference "
            "fields — execution-plane keys are always refused."
        ),
    )
    config_sub = parser.add_subparsers(dest="config_command")

    p_get = config_sub.add_parser("get", help="Print a config value by dotted key")
    p_get.add_argument("key", help="Dotted key, e.g. channels.email.default_to")
    p_get.add_argument("--json", action="store_true", help="JSON output")
    p_get.set_defaults(func=cmd_config_get)

    p_set = config_sub.add_parser("set", help="Set an allowlisted config field")
    p_set.add_argument("key", help="Dotted key (must be on the allowlist)")
    p_set.add_argument("value", help="New value (typed; validated per key)")
    p_set.add_argument("--json", action="store_true", help="JSON output")
    p_set.set_defaults(func=cmd_config_set)

    p_list = config_sub.add_parser("list", help="List config keys and values")
    p_list.add_argument("--editable", action="store_true",
                        help="Show only the agent-editable allowlist")
    p_list.add_argument("--json", action="store_true", help="JSON output")
    p_list.set_defaults(func=cmd_config_list)
