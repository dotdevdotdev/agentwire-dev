"""MCP tools — handoff domain."""

from .core import run_agentwire_cmd
from .mcp_core import (
    mcp,
)


@mcp.tool()
def handoff_init(title: str = "") -> str:
    """Create a handoff bundle dir + pre-filled ai-handoff.md template.

    The agent must then edit ai-handoff.md to fill in summary, decisions,
    journey, theme — everything the agent knows from the conversation.
    After editing, call handoff_render to produce show-the-story.html.

    Args:
        title: Optional short title hint, used in the bundle slug.

    Returns:
        Bundle dir path and the path to the ai-handoff.md template to edit.
    """
    cmd = ["handoff", "init"]
    if title:
        cmd.extend(["--title", title])
    data = run_agentwire_cmd(cmd)
    if not data.get("success"):
        return f"Failed to init handoff: {data.get('error', 'Unknown error')}"

    return (
        f"Handoff bundle initialized.\n\n"
        f"  Bundle dir: {data.get('bundle_dir')}\n"
        f"  Edit:       {data.get('ai_handoff_path')}\n\n"
        f"Now: fill in the {{ ... }} placeholders in ai-handoff.md, then call "
        f"handoff_render with bundle_dir."
    )


@mcp.tool()
def handoff_render(bundle_dir: str, story: bool = True) -> str:
    """Render show-the-story.html from an existing ai-handoff.md.

    Call this after editing ai-handoff.md to produce the human-readable
    one-pager presentation. The HTML is self-contained — opens offline,
    can be emailed or pasted into another LLM.

    Args:
        bundle_dir: Path to the bundle dir (or directly to ai-handoff.md).
        story: If True (default), render show-the-story.html. If False, just
            validate the markdown without producing HTML.

    Returns:
        Paths to the rendered artifacts.
    """
    cmd = ["handoff", "render", bundle_dir]
    if not story:
        cmd.append("--no-story")
    data = run_agentwire_cmd(cmd)
    if not data.get("success"):
        return f"Failed to render handoff: {data.get('error', 'Unknown error')}"

    lines = ["Handoff rendered.", f"  Bundle: {data.get('bundle_dir')}"]
    if path := data.get("show_the_story_path"):
        lines.append(f"  HTML:   {path}")
    if path := data.get("ai_handoff_path"):
        lines.append(f"  MD:     {path}")
    return "\n".join(lines)


@mcp.tool()
def handoff_list() -> str:
    """List past handoff bundles in ~/.agentwire/artifacts/.

    Returns:
        Bundle names with creation date and title hints, or a message
        indicating none exist.
    """
    data = run_agentwire_cmd(["handoff", "list"])
    if not data.get("success"):
        return f"Failed to list handoffs: {data.get('error', 'Unknown error')}"

    bundles = data.get("bundles", [])
    if not bundles:
        return "No handoff bundles found."

    lines = [f"Handoff bundles ({len(bundles)}):"]
    for b in bundles:
        flags = []
        if b.get("ai_handoff_exists"):
            flags.append("md")
        if b.get("show_the_story_exists"):
            flags.append("html")
        title = b.get("title_hint") or "(no title)"
        lines.append(f"  - {b.get('name')} [{','.join(flags) or '-'}] {title}")
    return "\n".join(lines)
