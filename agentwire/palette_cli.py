"""CLI for user-defined command-palette items (#676).

Users declare personal palette entries in ``~/.agentwire/config.yaml``:

.. code-block:: yaml

    palette:
      items:
        - id: quicktask
          label: "Quick task"
          icon: "⚡"
          keywords: "quicktask task worktree quick"
          run: "agentwire worktree {name} -p {project}"
          fields:
            - { name: name,    label: "Branch/task name" }
            - { name: project, label: "Project", default: "agentwire-dev" }

``agentwire palette list`` returns the validated items; ``agentwire palette
run <id> --field k=v`` substitutes the field values into the ``run`` template
and executes it. The portal wraps both (CLI is the SSOT).

Security model: the ``run`` template is owner-authored host config —
``config.yaml`` is hook-protected control plane (#466), and ``palette.`` is an
execution-plane prefix in ``config_cli`` so it can never be added to the
``agentwire config set`` allowlist. Field *values* arrive from the portal form
and are untrusted: each is shell-quoted before substitution, so a value can
never inject additional shell syntax.
"""

from __future__ import annotations

import re
import shlex
import string
import subprocess

from .core import _output_json, _output_result, load_config

# Palette items run on the portal machine; keep a hard ceiling so a hung
# command can't wedge the caller forever.
RUN_TIMEOUT_SECONDS = 300

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_item(raw: object) -> tuple[dict | None, str | None]:
    """Normalize one raw config entry → (item, None) or (None, reason)."""
    if not isinstance(raw, dict):
        return None, "item is not a mapping"
    item_id = str(raw.get("id") or "").strip()
    if not _ID_RE.match(item_id):
        return None, f"invalid or missing id: {raw.get('id')!r}"
    label = str(raw.get("label") or "").strip()
    if not label:
        return None, f"item {item_id!r}: missing label"
    run = str(raw.get("run") or "").strip()
    if not run:
        return None, f"item {item_id!r}: missing run command"

    fields = []
    for f in raw.get("fields") or []:
        if not isinstance(f, dict):
            return None, f"item {item_id!r}: field is not a mapping"
        name = str(f.get("name") or "").strip()
        if not _FIELD_NAME_RE.match(name):
            return None, f"item {item_id!r}: invalid field name {f.get('name')!r}"
        fields.append({
            "name": name,
            "label": str(f.get("label") or name),
            "default": str(f.get("default", "")),
        })

    # Every placeholder in the run template must be a declared field.
    field_names = {f["name"] for f in fields}
    for _, placeholder, _, _ in string.Formatter().parse(run):
        if placeholder is None:
            continue
        if placeholder not in field_names:
            return None, f"item {item_id!r}: run references undeclared field {{{placeholder}}}"

    return {
        "id": item_id,
        "label": label,
        "icon": str(raw.get("icon") or ""),
        "keywords": str(raw.get("keywords") or ""),
        "run": run,
        "fields": fields,
    }, None


def load_palette_items() -> tuple[list[dict], list[str]]:
    """Load and validate palette items from config.yaml → (items, errors)."""
    palette = load_config().get("palette") or {}
    raw_items = palette.get("items") or []
    if not isinstance(raw_items, list):
        return [], ["palette.items is not a list"]
    items, errors, seen = [], [], set()
    for raw in raw_items:
        item, err = _validate_item(raw)
        if err:
            errors.append(err)
            continue
        if item["id"] in seen:
            errors.append(f"duplicate item id {item['id']!r}")
            continue
        seen.add(item["id"])
        items.append(item)
    return items, errors


def cmd_palette_list(args) -> int:
    items, errors = load_palette_items()
    if args.json:
        _output_json({"success": True, "items": items, "errors": errors})
        return 0
    if not items and not errors:
        print("No palette items configured (palette.items in ~/.agentwire/config.yaml).")
    for it in items:
        fields = ", ".join(f["name"] for f in it["fields"])
        print(f"{it['id']}: {it['label']}" + (f"  (fields: {fields})" if fields else ""))
    for err in errors:
        print(f"invalid: {err}")
    return 0


def cmd_palette_run(args) -> int:
    items, _errors = load_palette_items()
    item = next((it for it in items if it["id"] == args.id), None)
    if item is None:
        return _output_result(False, args.json, f"Unknown palette item: {args.id}")

    values = {f["name"]: f["default"] for f in item["fields"]}
    for pair in args.field or []:
        if "=" not in pair:
            return _output_result(False, args.json, f"--field expects name=value (got {pair!r})")
        name, value = pair.split("=", 1)
        if name not in values:
            return _output_result(False, args.json, f"Unknown field {name!r} for item {args.id!r}")
        values[name] = value

    missing = [n for n, v in values.items() if not v]
    if missing:
        return _output_result(False, args.json, f"Missing value for field(s): {', '.join(missing)}")

    # Untrusted form values are shell-quoted; the template itself is trusted
    # owner config, so its own shell syntax (pipes, &&) passes through as-is.
    command = item["run"].format(**{n: shlex.quote(v) for n, v in values.items()})
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _output_result(
            False, args.json,
            f"Palette item {args.id!r} timed out after {RUN_TIMEOUT_SECONDS}s",
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    success = proc.returncode == 0
    if args.json:
        _output_json({
            "success": success,
            "exit_code": proc.returncode,
            "output": output,
            "command": command,
        })
        return 0 if success else 1
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    return 0 if success else 1


def register_palette_parser(subparsers) -> None:
    palette_parser = subparsers.add_parser(
        "palette", help="User-defined command-palette items (portal Cmd/Ctrl+K)")
    palette_sub = palette_parser.add_subparsers(dest="palette_command", required=True)

    list_parser = palette_sub.add_parser("list", help="List configured palette items")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")
    list_parser.set_defaults(func=cmd_palette_list)

    run_parser = palette_sub.add_parser("run", help="Run a configured palette item")
    run_parser.add_argument("id", help="Palette item id (from palette.items)")
    run_parser.add_argument("--field", action="append", metavar="NAME=VALUE",
                            help="Field value for the item's run template (repeatable)")
    run_parser.add_argument("--json", action="store_true", help="Output as JSON")
    run_parser.set_defaults(func=cmd_palette_run)
