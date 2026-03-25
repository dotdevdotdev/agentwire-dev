"""Template expansion for task configurations.

Supports two types of variable expansion:
- {{ var }} - Mustache-style variables from TemplateContext (error on undefined)
- ${ENV_VAR} - Shell-style environment variables (pass through if undefined)
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime


class TemplateError(Exception):
    """Raised when template expansion fails."""

    pass


@dataclass
class TemplateContext:
    """Context for template variable expansion.

    Contains built-in variables and task-specific variables populated
    during execution.
    """

    # Task identity
    session: str = ""
    task: str = ""
    project_root: str = ""

    # Execution state
    attempt: int = 1

    # Result variables (populated after completion)
    status: str = ""  # complete, incomplete, failed
    summary: str = ""
    summary_file: str = ""
    output: str = ""

    # Branch management (populated when starting_ref is configured)
    work_branch: str = ""
    pr_url: str = ""

    # Pre-command outputs (dynamically populated)
    pre_outputs: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        """Get a variable value by name.

        Checks in order:
        1. Built-in date/time variables
        2. Dataclass attributes
        3. Pre-command outputs

        Args:
            key: Variable name

        Returns:
            Variable value as string, or None if not found
        """
        # Built-in date/time variables (computed on access)
        now = datetime.now()
        if key == "date":
            return now.strftime("%Y-%m-%d")
        if key == "time":
            return now.strftime("%H:%M:%S")
        if key == "datetime":
            return now.isoformat()

        # Dataclass attributes
        if hasattr(self, key) and key != "pre_outputs":
            value = getattr(self, key)
            if value is not None:
                return str(value)

        # Pre-command outputs
        if key in self.pre_outputs:
            return self.pre_outputs[key]

        return None

    def has(self, key: str) -> bool:
        """Check if a variable exists in context."""
        return self.get(key) is not None

    def set_pre_output(self, name: str, value: str) -> None:
        """Set a pre-command output variable."""
        self.pre_outputs[name] = value


def expand_template(text: str, ctx: TemplateContext) -> str:
    """Expand {{ var }} template variables using context.

    Args:
        text: Template string with {{ var }} placeholders
        ctx: TemplateContext with variable values

    Returns:
        Expanded string

    Raises:
        TemplateError: If a {{ var }} variable is undefined
    """
    # Pattern: {{ var_name }} with optional whitespace
    pattern = r"\{\{\s*(\w+)\s*\}\}"

    def replace(match: re.Match) -> str:
        var_name = match.group(1)
        value = ctx.get(var_name)
        if value is None:
            raise TemplateError(f"Undefined variable: {{{{{var_name}}}}}")
        return value

    return re.sub(pattern, replace, text)


def expand_env_vars(text: str) -> str:
    """Expand ${ENV_VAR} environment variables.

    Undefined environment variables are passed through unchanged
    (let the shell handle them or error).

    Args:
        text: String with ${ENV_VAR} placeholders

    Returns:
        Expanded string
    """
    # Pattern: ${VAR_NAME}
    pattern = r"\$\{(\w+)\}"

    def replace(match: re.Match) -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is not None:
            return value
        # Pass through undefined - shell will handle
        return match.group(0)

    return re.sub(pattern, replace, text)


def expand_all(text: str, ctx: TemplateContext) -> str:
    """Expand both {{ var }} and ${ENV_VAR} in text.

    Expansion order:
    1. {{ var }} from context (errors on undefined)
    2. ${ENV_VAR} from environment (passes through undefined)

    Args:
        text: Template string
        ctx: TemplateContext with variable values

    Returns:
        Fully expanded string

    Raises:
        TemplateError: If a {{ var }} variable is undefined
    """
    # First expand template variables (strict)
    text = expand_template(text, ctx)
    # Then expand environment variables (permissive)
    text = expand_env_vars(text)
    return text


def preview_template(text: str, ctx: TemplateContext | None = None) -> str:
    """Preview template expansion without erroring on undefined variables.

    Used for --dry-run mode where pre-commands haven't run yet.
    Undefined {{ var }} variables show as <pre:var> placeholders.

    Args:
        text: Template string
        ctx: Optional TemplateContext (uses empty if None)

    Returns:
        Partially expanded string with placeholders for undefined
    """
    if ctx is None:
        ctx = TemplateContext()

    # Pattern: {{ var_name }} with optional whitespace
    pattern = r"\{\{\s*(\w+)\s*\}\}"

    def replace(match: re.Match) -> str:
        var_name = match.group(1)
        value = ctx.get(var_name)
        if value is not None:
            return value
        # Show placeholder for undefined (likely pre-command output)
        return f"<pre:{var_name}>"

    text = re.sub(pattern, replace, text)
    # Also expand known env vars
    text = expand_env_vars(text)
    return text
