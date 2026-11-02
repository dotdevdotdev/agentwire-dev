"""The one read-modify-write path for a harness's JSON settings file (#1209 M5, section 5).

Generalizes what #1220 fixed in agentwire. The file belongs to the user and to other tools,
never to us, so every edit goes through :func:`edit_json_config`:

* **Unparseable input is a refusal, never a rewrite** (:class:`ConfigError`). Replacing a file
  we could not read with ``{}`` deletes every other setting the user has.
* **Structural merge.** The caller's ``mutate`` edits the parsed dict in place and returns True
  when it changed something; nothing here knows what a hook is.
* **The user's formatting survives**: BOM, CRLF, indent width and a missing trailing newline are
  detected and re-applied, so an edit changes the lines it means to change.
* **Atomic write**: temp file in the same directory, ``fsync``, ``os.replace``, ``fsync`` of the
  directory. A crash (or ``kill -9``) leaves the old file or the new one, never a partial one.
* **Concurrent-writer detection**: the bytes are re-read just before the replace and again
  after it. If another writer got in between, the edit is recomputed from the new bytes once,
  then refused.
* **Backup** of the original, once, never overwritten.

Standard library only.
"""

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

_ATTEMPTS = 2


class ConfigError(RuntimeError):
    """The config file could not be safely edited; nothing was written."""


class EditResult(NamedTuple):
    changed: bool
    created: bool  # the file did not exist before this edit
    sha256: Optional[str]  # of the bytes now on disk (None if the file is absent)


class Style(NamedTuple):
    bom: bool = False
    crlf: bool = False
    indent: int = 2
    trailing_newline: bool = True


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(path: Path) -> Path:
    """Edit through a symlink (dotfile managers) instead of replacing it."""
    return path.resolve() if path.is_symlink() else path


def read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e


def _strict_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse(raw: Optional[bytes], path: Path) -> Dict[str, Any]:
    """The JSON object in ``raw`` ({} for absent or empty). Anything else is a refusal."""
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8-sig"), parse_constant=_strict_constant)
    except ValueError as e:  # JSONDecodeError and UnicodeDecodeError
        raise ConfigError(f"{path} is not valid JSON ({e}); left untouched. Fix the file and re-run.") from e
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path} is not a JSON object; left untouched. Fix the file and re-run.")
    return parsed


def read_config(path: Path) -> Tuple[Optional[bytes], Dict[str, Any]]:
    """``(raw bytes or None if absent, parsed object)``. Read-only; refuses what it cannot parse."""
    raw = read_bytes(resolve(path))
    return raw, parse(raw, path)


def detect_style(raw: Optional[bytes]) -> Style:
    if not raw or not raw.strip():
        return Style()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    crlf = "\r\n" in text
    indent = 2
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            indent = len(line) - len(stripped)
            break
    return Style(bom=bom, crlf=crlf, indent=indent, trailing_newline=text.endswith("\n"))


def render(settings: Dict[str, Any], style: Style) -> bytes:
    text = json.dumps(settings, indent=style.indent, ensure_ascii=False)
    if style.trailing_newline:
        text += "\n"
    if style.crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    return b"\xef\xbb\xbf" + data if style.bom else data


def atomic_replace(path: Path, data: bytes, expected: Optional[bytes]) -> bool:
    """Write ``data`` over ``path`` atomically iff its bytes are still ``expected``.

    Returns False (writing nothing) when the file changed since it was read. Preserves the
    existing file's mode. The temp file never outlives the call.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        if read_bytes(path) != expected:
            return False
        os.replace(tmp, path)
        _fsync_dir(path.parent)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _backup_once(path: Path, raw: Optional[bytes], backup: Optional[Path]) -> None:
    if backup is None or raw is None or backup.exists():
        return
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)


def edit_json_config(
    path: Path,
    mutate: Callable[[Dict[str, Any]], bool],
    backup: Optional[Path] = None,
) -> EditResult:
    """Apply ``mutate`` to the parsed config at ``path`` and persist it if it reports a change.

    Raises :class:`ConfigError` (writing nothing) for unparseable input, a read-only file,
    or a file that keeps changing underneath the edit. A write is re-read and verified.
    """
    path = resolve(Path(path))
    for _ in range(_ATTEMPTS):
        raw = read_bytes(path)
        settings = parse(raw, path)
        if raw is not None and not os.access(path, os.W_OK):
            raise ConfigError(f"{path} is not writable; left untouched.")
        if not mutate(settings):
            return EditResult(False, False, None if raw is None else sha256_hex(raw))
        data = render(settings, detect_style(raw))
        _backup_once(path, raw, backup)
        try:
            replaced = atomic_replace(path, data, raw)
        except OSError as e:
            raise ConfigError(f"cannot write {path}: {e}; nothing written.") from e
        if not replaced:
            continue  # changed since we read it: recompute from the new bytes
        if read_bytes(path) != data:
            raise ConfigError(f"{path} was changed by another writer right after the edit; re-run to verify.")
        return EditResult(True, raw is None, sha256_hex(data))
    raise ConfigError(f"{path} kept changing while it was being edited; nothing written. Re-run.")


def restore_bytes(path: Path, data: Optional[bytes], expected_sha256: str) -> bool:
    """Put ``data`` back (or delete the file if ``data`` is None) iff the file is still what we wrote.

    The byte-exact half of uninstall. Returns False when the file has changed since
    ``expected_sha256``, so the caller falls back to a structural removal.
    """
    path = resolve(Path(path))
    current = read_bytes(path)
    if current is None or sha256_hex(current) != expected_sha256:
        return False
    if data is None:
        if read_bytes(path) != current:
            return False
        path.unlink()
        return True
    return atomic_replace(path, data, current)
