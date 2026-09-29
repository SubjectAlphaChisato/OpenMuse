"""Wall-clock schedule math (AUTO-01). Pure functions; every instant is timezone-aware UTC.

Rules from the spec:
  * schedules are local wall-clock times in the automation's saved IANA zone;
  * spring-forward gap: run at the first valid local time afterward;
  * fall-back repeated hour: run once, at the first occurrence (fold=0);
  * one candidate per local date, so a repeated hour can never produce two runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LONG_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    kind: str  # once | daily | weekly
    time_zone: str
    local_time: str  # HH:MM
    weekdays: tuple[int, ...] = ()
    run_date: str | None = None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)

    @property
    def wall(self) -> time:
        h, m = self.local_time.split(":")
        return time(int(h), int(m))


def zone_or_error(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ScheduleError(f"{name!r} is not an IANA time zone such as America/Los_Angeles") from None


def parse_local_time(value: str) -> str:
    try:
        h, m = value.strip().split(":")
        t = time(int(h), int(m))
    except (ValueError, AttributeError):
        raise ScheduleError("local_time must be HH:MM in 24-hour time, e.g. 18:00") from None
    return f"{t.hour:02d}:{t.minute:02d}"


def parse_weekdays(values: list[str | int]) -> tuple[int, ...]:
    out = set()
    for v in values:
        if isinstance(v, int) and 0 <= v <= 6:
            out.add(v)
        elif isinstance(v, str) and v.strip().lower()[:3] in WEEKDAY_NAMES:
            out.add(WEEKDAY_NAMES.index(v.strip().lower()[:3]))
        else:
            raise ScheduleError(f"unknown weekday {v!r}; use mon..sun")
    if not out:
        raise ScheduleError("choose at least one weekday")
    return tuple(sorted(out))


def exists(local: datetime) -> bool:
    """False for wall times inside a spring-forward gap."""
    back = local.astimezone(timezone.utc).astimezone(local.tzinfo)
    return back.replace(tzinfo=None) == local.replace(tzinfo=None)


def resolve(d: date, wall: time, zone: ZoneInfo) -> datetime:
    """The UTC instant for a wall time on a date: gap -> first valid minute after; repeat -> first occurrence."""
    local = datetime.combine(d, wall, tzinfo=zone).replace(fold=0)
    steps = 0
    while not exists(local):
        local = (local.replace(tzinfo=None) + timedelta(minutes=1)).replace(tzinfo=zone, fold=0)
        steps += 1
        if steps > 24 * 60:  # pragma: no cover - no zone has a day-long gap
            raise ScheduleError("could not resolve a valid local time")
    return local.astimezone(timezone.utc)


def _dates_from(s: Schedule, start: date):
    d = start
    while True:
        if s.kind == "daily" or (s.kind == "weekly" and d.weekday() in s.weekdays):
            yield d
        d += timedelta(days=1)


def next_after(s: Schedule, after: datetime) -> datetime | None:
    """The first scheduled instant strictly after `after` (UTC-aware)."""
    if s.kind == "once":
        at = resolve(date.fromisoformat(s.run_date), s.wall, s.zone)
        return at if at > after else None
    start = after.astimezone(s.zone).date() - timedelta(days=1)
    for i, d in enumerate(_dates_from(s, start)):
        at = resolve(d, s.wall, s.zone)
        if at > after:
            return at
        if i > 400:  # pragma: no cover
            raise ScheduleError("no future run found")
    return None


def slots_through(s: Schedule, first: datetime, now: datetime, cap: int = 10_000) -> list[datetime]:
    """Every scheduled instant from `first` (itself a slot) through `now`, oldest first."""
    out: list[datetime] = []
    cur: datetime | None = first
    while cur is not None and cur <= now and len(out) < cap:
        out.append(cur)
        cur = next_after(s, cur)
    return out


def describe(s: Schedule) -> str:
    at = f"{s.local_time} ({s.time_zone})"
    if s.kind == "once":
        return f"Once on {s.run_date} at {at}"
    if s.kind == "daily":
        return f"Every day at {at}"
    if s.weekdays == (0, 1, 2, 3, 4):
        return f"Every weekday at {at}"
    if s.weekdays == (5, 6):
        return f"Every weekend day at {at}"
    return f"Every {', '.join(LONG_NAMES[d] for d in s.weekdays)} at {at}"


def upcoming(s: Schedule, now: datetime, n: int = 3) -> list[datetime]:
    out: list[datetime] = []
    cur = now
    while len(out) < n:
        nxt = next_after(s, cur)
        if nxt is None:
            break
        out.append(nxt)
        cur = nxt
    return out


def local_label(at: datetime, tz: str) -> str:
    local = at.astimezone(ZoneInfo(tz))
    return local.strftime("%a %Y-%m-%d %H:%M ") + (local.tzname() or tz)
