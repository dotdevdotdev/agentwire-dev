"""Rule groups and packs: which bundled rule/tooldef files belong to what (#1141).

Every bundled ``rules/*.yaml`` and ``tooldefs/*.yaml`` belongs to exactly one
entry here. ``kind="group"`` entries are default-on ("os-focused"); a human may
opt one out. ``kernel`` groups can never be opted out. ``kind="pack"`` entries
are vendor/tool sets that stay off until a human enables them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # "group" | "pack"
    description: str
    rules: Tuple[str, ...] = ()
    tooldefs: Tuple[str, ...] = ()
    kernel: bool = False
    binaries: Tuple[str, ...] = ()  # vendor CLIs whose presence suggests a pack


_ENTRIES = (
    # --- default groups -----------------------------------------------------
    Entry("control-plane", "group", "Protected damage-control/agent config paths", ("control-plane.yaml",), kernel=True),
    Entry("core", "group", "Filesystem, process and history destruction", ("core.yaml",), kernel=True),
    Entry("git", "group", "Destructive git operations, gh repo delete + git/gh tooldefs", ("git.yaml",), ("git.yaml", "gh.yaml")),
    Entry("uv", "group", "uv run/publish ask-tier (own group: opting out of git must not drop it)", (), ("uv.yaml",)),
    Entry("registry", "group", "npm unpublish + npm publish ask-tier", ("registry.yaml",), ("npm.yaml",)),
    Entry("agentwire", "group", "AgentWire state and session destruction", ("agentwire.yaml",)),
    Entry("payloads", "group", "Rules scanning payloads wrapped in other commands", ("payloads.yaml",)),
    Entry("remote", "group", "ssh-only remote hazards (reboot, service stop)", ("remote.yaml",)),
    Entry("mcp-tools", "group", "Per-tool policy for MCP tool calls", ("mcp-tools.yaml",)),
    Entry("outbound", "group", "Outward-facing sends (email, SMS, push)", ("outbound.yaml",)),
    # --- packs --------------------------------------------------------------
    Entry("aws", "pack", "AWS CLI", ("aws.yaml",), ("aws.yaml",), binaries=("aws",)),
    Entry("gcp", "pack", "gcloud", ("gcp.yaml",), ("gcp.yaml",), binaries=("gcloud",)),
    Entry("firebase", "pack", "Firebase CLI", ("firebase.yaml",), binaries=("firebase",)),
    Entry("cloud-hosting", "pack", "Vercel, Netlify, Cloudflare, Heroku, Fly, DO, Supabase", ("cloud-hosting.yaml",), binaries=("vercel", "netlify", "wrangler", "heroku", "flyctl", "doctl", "supabase")),
    Entry("databases", "pack", "Database CLIs and SQL", ("databases.yaml",), binaries=("psql", "mysql", "mongosh", "redis-cli", "prisma", "rails", "alembic", "flyway", "knex", "sequelize")),
    Entry("containers", "pack", "Docker and Kubernetes", ("containers.yaml",), ("docker.yaml", "kubectl.yaml"), binaries=("docker", "kubectl")),
    Entry("infrastructure", "pack", "Terraform, Pulumi, Serverless, SAM", ("infrastructure.yaml",), ("terraform.yaml",), binaries=("terraform", "pulumi", "serverless", "sam")),
    Entry("publish", "pack", "cargo/poetry/twine/pnpm/yarn/gem/mvn publish", ("publish.yaml",), binaries=("cargo", "poetry", "twine", "pnpm", "yarn", "gem", "mvn")),
)

REGISTRY: Dict[str, Entry] = {e.name: e for e in _ENTRIES}


def bundled_files(package_dir: Path) -> Dict[str, set]:
    """The ``{"rules": {...}, "tooldefs": {...}}`` file names actually bundled."""
    return {
        "rules": {p.name for p in (package_dir / "hooks" / "damage-control" / "rules").glob("*.yaml")},
        "tooldefs": {p.name for p in (package_dir / "tooldefs").glob("*.yaml")},
    }


def unclassified(package_dir: Path) -> Dict[str, list]:
    """Bundled files owned by no entry, or by more than one, per section."""
    bundled = bundled_files(package_dir)
    out: Dict[str, list] = {}
    for section in ("rules", "tooldefs"):
        owners: Dict[str, list] = {}
        for e in _ENTRIES:
            for f in getattr(e, section):
                owners.setdefault(f, []).append(e.name)
        bad = [f"{f}: owned by {owners.get(f, [])}" for f in sorted(bundled[section] | set(owners))
               if f not in bundled[section] or len(owners.get(f, [])) != 1]
        if bad:
            out[section] = bad
    return out
