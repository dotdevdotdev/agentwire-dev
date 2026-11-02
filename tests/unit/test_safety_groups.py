"""Every bundled rule/tooldef file belongs to exactly one group or pack (#1141)."""

from damage_control import groups


def test_every_bundled_file_has_exactly_one_owner():
    bad = groups.unclassified()
    assert not bad, bad
