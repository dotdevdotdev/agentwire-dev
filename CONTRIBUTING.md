# Contributing to AgentWire

Thank you for your interest in contributing to AgentWire!

## Contributor License Agreement

By submitting a pull request, you agree to the terms of our [Contributor License Agreement](CLA.md). This allows us to maintain our dual-licensing model (AGPL v3 + commercial) while accepting community contributions.

## Development Setup

### Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) - Fast Python package manager
- tmux

### Quick Start

```bash
# Clone the repository
git clone https://github.com/dotdevdotdev/agentwire-dev.git
cd agentwire-dev

# Install in development mode
uv pip install -e .

# Run in development mode (picks up code changes instantly)
agentwire portal start --dev

# After structural changes (pyproject.toml, new files)
agentwire rebuild
```

### Development Workflow

```bash
# Start development session
agentwire dev

# Run linter
uvx ruff check agentwire/

# Run with auto-fix
uvx ruff check agentwire/ --fix
```

## Code Style

### Linting

We use [ruff](https://github.com/astral-sh/ruff) for linting. Configuration is in `pyproject.toml`:

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "N", "W"]
```

### Docstrings

Use Google-style docstrings for all public functions:

```python
def function_name(arg1: str, arg2: int = 10) -> dict:
    """Brief one-line description.

    Longer description if needed explaining purpose
    and important details.

    Args:
        arg1: Description of arg1.
        arg2: Description of arg2. Defaults to 10.

    Returns:
        Description of return value.

    Raises:
        ValueError: When arg1 is empty.
    """
```

## Code Patterns

### Use Utility Modules

Common operations are centralized in `agentwire/utils/`:

```python
# Subprocess execution
from agentwire.utils import run_command, run_command_check

result = run_command(["ls", "-la"])
if result.success:
    print(result.stdout)

# File I/O
from agentwire.utils import load_json, save_json, load_yaml

config = load_json(config_path, default={})
save_json(config_path, data)

# Paths
from agentwire.utils import agentwire_dir, config_path, logs_dir

base = agentwire_dir()  # ~/.agentwire/
```

### Error Handling

Use the structured error classes in `agentwire/errors.py`:

```python
from agentwire.errors import AgentWireError

raise AgentWireError(
    what="Session not found",
    why="No tmux session exists with that name",
    how="Create a new session with 'agentwire new -s name'"
)
```

### Configuration

Use dataclasses for configuration (see `agentwire/config.py`):

```python
@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
```

### Validation

Use structured validation with suggestions (see `agentwire/validation.py`):

```python
def validate_config(config: Config) -> tuple[list[ConfigError], list[ConfigWarning]]:
    """Returns errors and warnings with fix suggestions."""
```

## CLI First

All session/machine logic lives in CLI commands (`__main__.py`). The web portal is a thin wrapper that:

1. Calls CLI via `run_agentwire_cmd(["command", "args"])`
2. Parses JSON output (`--json` flag)
3. Adds WebSocket/real-time features

When adding new functionality:
1. Implement in CLI first with `--json` output
2. Portal calls CLI, doesn't duplicate logic
3. Never bypass CLI with direct tmux/subprocess calls

## Project Structure

```
agentwire/
├── __main__.py      # CLI commands (entry point)
├── server.py        # WebSocket server
├── config.py        # Configuration dataclasses
├── errors.py        # Structured error classes
├── validation.py    # Config validation
├── utils/           # Shared utilities
│   ├── subprocess.py  # Command execution
│   ├── file_io.py     # JSON/YAML handling
│   └── paths.py       # Path management
├── agents/          # Agent implementations
├── tts/             # Text-to-speech backends
├── stt/             # Speech-to-text backends
├── hooks/           # Claude Code hooks
└── roles/           # Role instruction files
```

## Pull Request Guidelines

1. Create a branch from `main`
2. Make your changes
3. Run `uvx ruff check agentwire/` - ensure no errors
4. Commit with descriptive message
5. Open PR with description of changes

## Questions?

Open an issue or start a discussion on GitHub.
