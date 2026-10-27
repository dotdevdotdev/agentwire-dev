"""An extension glob (``*.key``) must not fire on JS property access or a jq path.

Two halves, both load-bearing: expression-shaped tokens are released, and every
shape a real secret read takes keeps matching — including the ones a careless
release would hide (a second token behind a released one, brace expansion, a
quoted filename, a path).
"""

import pytest

from agentwire.safety._core import matches_path_in_command

pytestmark = pytest.mark.real_agentwire_home


# Code, not a path argument: nothing here names a file.

# A secret-file read in every spelling that must keep blocking.
STILL_MATCHES = {
    "cat": "cat server.key",
    "cat-path": "cat ./certs/server.key",
    "quoted": "python3 -c \"open('server.key').read()\"",
    "openssl": "openssl rsa -in a.key -out b.pem",
    "scp": "scp host:k.key .",
    "released-then-real": "(e.key a.key",
    "real-after-released": "if (event.key !== x) cat server.key",
    "redirect": "tee < private.key",
}

# Known limit, not asserted either way: a bare ``x.key`` after a space (``not e.key``)
# has the exact shape of ``cat a.key`` and keeps matching.


def test_real_secret_reads_still_match():
    missed = [n for n, c in STILL_MATCHES.items() if not matches_path_in_command("*.key", c)]
    assert not missed, missed
