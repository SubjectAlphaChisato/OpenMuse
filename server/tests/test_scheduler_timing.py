"""AUTO-01 schedule math: DST gap and repeated hour, weekday sets, catch-up enumeration."""
from datetime import datetime, timezone

import pytest

from opa.scheduler.timing import Schedule, ScheduleError, describe, next_after, parse_local_time, parse_weekdays, slots_through, upcoming

UTC = timezone.utc
LA = "America/Los_Angeles"


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_spring_gap_runs_at_first_valid_time_afterward():
    s = Schedule("daily", LA, "02:30")
    # 2026-03-08: clocks jump 02:00 PST -> 03:00 PDT, so 02:30 does not exist
    assert next_after(s, utc(2026, 3, 8, 8, 0)) == utc(2026, 3, 8, 10, 0)  # 03:00 PDT
    assert next_after(s, utc(2026, 3, 8, 10, 0)) == utc(2026, 3, 9, 9, 30)  # 02:30 PDT next day
    assert next_after(s, utc(2026, 3, 7, 0, 0)) == utc(2026, 3, 7, 10, 30)  # 02:30 PST the day before


def test_fall_repeat_runs_once_at_first_occurrence():
    s = Schedule("daily", LA, "01:30")
    # 2026-11-01: 01:00-02:00 happens twice (PDT, then PST)
    first = next_after(s, utc(2026, 11, 1, 0, 0))
    assert first == utc(2026, 11, 1, 8, 30)  # 01:30 PDT
    assert next_after(s, first) == utc(2026, 11, 2, 9, 30)  # skips the 01:30 PST repeat; next day 01:30 PST


def test_one_run_per_local_day_across_both_transitions():
    s = Schedule("daily", LA, "01:30")
    runs = slots_through(s, next_after(s, utc(2026, 10, 30, 0, 0)), utc(2026, 11, 4, 0, 0))
    local_dates = [r.astimezone(s.zone).date().isoformat() for r in runs]
    assert local_dates == ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03"]


def test_weekdays_and_once():
    s = Schedule("weekly", LA, "18:00", parse_weekdays(["mon", "tue", "wed", "thu", "fri"]))
    runs = upcoming(s, utc(2026, 10, 2, 12, 0), 3)  # Friday 05:00 PDT
    assert [r.astimezone(s.zone).strftime("%a %H:%M") for r in runs] == ["Fri 18:00", "Mon 18:00", "Tue 18:00"]
    assert describe(s) == "Every weekday at 18:00 (America/Los_Angeles)"
    once = Schedule("once", "Asia/Tokyo", "10:00", run_date="2026-10-05")
    assert next_after(once, utc(2026, 10, 1)) == utc(2026, 10, 5, 1, 0)
    assert next_after(once, utc(2026, 10, 6)) is None


def test_catch_up_enumeration_counts_missed_slots():
    s = Schedule("daily", "UTC", "09:00")
    slots = slots_through(s, utc(2026, 10, 1, 9), utc(2026, 10, 4, 12))
    assert slots == [utc(2026, 10, d, 9) for d in (1, 2, 3, 4)]


@pytest.mark.parametrize("bad", ["25:00", "9", "nine", "12:61"])
def test_invalid_local_time(bad):
    with pytest.raises(ScheduleError):
        parse_local_time(bad)


def test_invalid_weekdays():
    with pytest.raises(ScheduleError):
        parse_weekdays([])
    with pytest.raises(ScheduleError):
        parse_weekdays(["funday"])
