"""Every bundled rule/tooldef file belongs to exactly one group or pack (#1141)."""

from pathlib import Path

import agentwire
from agentwire.safety import groups


def test_every_bundled_file_has_exactly_one_owner():
    bad = groups.unclassified(Path(agentwire.__file__).parent)
    assert not bad, bad


