"""The one loader for agentwire's env files (#1184).

python-dotenv's ``load_dotenv()`` never overrides a variable that already
exists — and an EMPTY one exists. A shell profile that does
``export OPENAI_API_KEY=`` therefore silently shadowed the real key in
``~/.agentwire/.env``, and every consumer then reported the key "not set" while
pointing at the very file that held it.

The rule here: an inherited value still wins over a file **unless it is empty
(or whitespace)**, in which case a non-empty file value fills it. Nothing in
this module prints or logs a value.

Deliberately free of agentwire imports: ``__main__`` runs it before importing
anything that might read the environment at import time.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, find_dotenv

#: The one blessed spot for secrets (docs/wiki/security/secrets.md).
GLOBAL_ENV_FILE = Path.home() / ".agentwire" / ".env"
_GLOBAL_ENV_DISPLAY = "~/.agentwire/.env"


def load_env_file(path: "str | Path") -> list[str]:
    """Merge one env file into ``os.environ``; return the keys it un-shadowed.

    A key absent from the environment is set. A key present with a non-empty
    value is left alone. A key present but empty is replaced by a non-empty
    file value — and its name returned, so a caller can say so.
    """
    if not path or not Path(path).is_file():
        return []
    unshadowed = []
    for key, value in dotenv_values(path).items():
        if value is None:
            continue
        current = os.environ.get(key)
        if current is None:
            os.environ[key] = value
        elif not current.strip() and value.strip():
            os.environ[key] = value
            unshadowed.append(key)
    return unshadowed


def load_env_files() -> list[str]:
    """Project ``.env`` first, then the global one — the CLI's startup load."""
    unshadowed = load_env_file(find_dotenv())
    unshadowed += load_env_file(GLOBAL_ENV_FILE)
    return unshadowed


def missing_env_message(name: str) -> str:
    """Why *name* has no usable value — distinguishing absent from exported-empty.

    The empty case is the one worth naming: after :func:`load_env_files` it
    means the secrets file had no non-empty value to fill it with, and a user
    who knows the key IS in the file would otherwise go looking in the wrong
    place.
    """
    if name in os.environ:
        return (
            f"{name} is exported as an empty string in this environment, and "
            f"{_GLOBAL_ENV_DISPLAY} has no non-empty value to fill it. Add it there "
            f"(chmod 600), or remove the empty `export {name}=` from your shell profile."
        )
    return f"{name} is not set. Add it to {_GLOBAL_ENV_DISPLAY} (chmod 600) — the one blessed spot for secrets."
