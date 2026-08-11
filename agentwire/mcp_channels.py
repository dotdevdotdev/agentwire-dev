"""MCP tools — channels domain."""

import json

from .core import run_agentwire_cmd
from .mcp_core import (
    mcp,
)


@mcp.tool()
def channels_list() -> str:
    """List all registered communication channels with their type and status.

    Returns:
        JSON list of channels with name, type, configured status, and builtin flag.
    """
    data = run_agentwire_cmd(["channels", "list"], json_output=True)
    if data.get("success"):
        return json.dumps(data["channels"], indent=2)
    return data.get("error", "Failed to list channels")


@mcp.tool()
def email_send(
    body: str,
    to: str | list[str] | None = None,
    subject: str | None = None,
    attachments: list[str] | None = None,
    plain_text: bool = False,
) -> str:
    """Send a branded email notification via Resend.

    Supports markdown in the body. Uses the HTML email template.

    Args:
        body: Email body (markdown supported)
        to: Recipient email(s). Accepts a single address, a comma-separated
            string, or a list (default: from config).
        subject: Email subject line (optional)
        attachments: List of file paths to attach (optional)
        plain_text: Send plain text only, no HTML template (default: false)

    Returns:
        Success message or error description.
    """
    args = ["email", "--body", body]
    if to:
        recipients = to if isinstance(to, list) else [to]
        for addr in recipients:
            args.extend(["--to", addr])
    if subject:
        args.extend(["--subject", subject])
    if attachments:
        for path in attachments:
            args.extend(["--attach", path])
    if plain_text:
        args.append("--plain")

    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        # Accepted by Resend ≠ delivered to the inbox — the provider boundary is
        # a genuine async one, so don't claim more than acceptance (#444).
        return "Email accepted by provider (Resend)."
    return f"Failed to send email: {data.get('error', 'Unknown error')}"


@mcp.tool()
def quo_send(body: str, to: str | None = None) -> str:
    """Send an SMS via Quo (OpenPhone).

    Args:
        body: Message text (max 1600 chars)
        to: Recipient phone number in +E.164 format (default: from config)

    Returns:
        Success message or error description.
    """
    args = ["quo", "--body", body]
    if to:
        args.extend(["--to", to])

    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        # Accepted by OpenPhone ≠ delivered to the handset — async provider
        # boundary, so claim only acceptance (#444).
        return "SMS accepted by provider (Quo/OpenPhone)."
    return f"Failed to send Quo SMS: {data.get('error', 'Unknown error')}"
