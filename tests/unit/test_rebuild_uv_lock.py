"""#1246: `uv cache clean` must not hang rebuild while a `uv run` holds the cache lock."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest

import agentwire.system_cli as sys_mod

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")

_HOLDER = (
    "import fcntl, sys, time\n"
    "f = open(sys.argv[1] + '/.lock', 'w')\n"
    "fcntl.flock(f, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "time.sleep(60)\n"
)


def test_clean_is_bounded_while_another_process_holds_the_lock(tmp_path, monkeypatch, capsys):
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", _HOLDER, str(tmp_path)],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(sys_mod, "UV_CACHE_CLEAN_TIMEOUT", 2.0)

        start = time.monotonic()
        sys_mod._clean_uv_cache_for_agentwire()
        elapsed = time.monotonic() - start
    finally:
        holder.kill()
        holder.wait()

    out = capsys.readouterr().out
    assert elapsed < 30, elapsed
    assert "lock is held" in out, out
    assert str(holder.pid) in out, out
