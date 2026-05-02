"""Unit tests for scheduler pure parsing helpers (no I/O, no mocks)."""

from datetime import datetime

import pytest

from agentwire.scheduler import _day_matches, _parse_duration, _parse_time


@pytest.mark.parametrize("text,expected", [
    ("30s", 30),
    ("30m", 1800),
    ("2h", 7200),
    ("1d", 86400),
    ("3600", 3600),  # bare integer means seconds
    (None, None),
    ("", None),
    ("foo", None),
])
def test_parse_duration(text, expected):
    assert _parse_duration(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("08:00", (8, 0)),
    ("20:30", (20, 30)),
    ("8:00", (8, 0)),
    (None, None),
    ("not-a-time", None),
])
def test_parse_time(text, expected):
    assert _parse_time(text) == expected


# Reference dates for day-of-week tests.
MONDAY = datetime(2026, 2, 16, 10, 0)
SATURDAY = datetime(2026, 2, 21, 10, 0)


@pytest.mark.parametrize("when,every,except_days,expected", [
    (MONDAY, "day", None, True),
    (MONDAY, "weekday", None, True),
    (SATURDAY, "weekday", None, False),
    (SATURDAY, "weekend", None, True),
    (MONDAY, "monday", None, True),
    (MONDAY, "tuesday", None, False),
    # except_days excludes the matching day even when "day" wildcard
    (SATURDAY, "day", ["saturday"], False),
    # Duration-style schedules also respect except_days
    (SATURDAY, "4h", ["saturday"], False),
    (MONDAY, "4h", None, True),
])
def test_day_matches(when, every, except_days, expected):
    assert _day_matches(when, every, except_days) is expected
