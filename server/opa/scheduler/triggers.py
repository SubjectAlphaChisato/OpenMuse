"""Calendar-change relevance (AUTO-02). Pure functions over normalized events.

A change is relevant when the event was, or now is, within the watch window (default next 48 h)
and it was added, cancelled, moved, or renamed/relocated. A version already in the baseline is not
a change, so redelivered or resynced events never fire twice. Versions the automation produced
itself are folded into the baseline without firing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from opa.broker.ops import parse_time

RETAIN_BEFORE = timedelta(days=1)
RETAIN_AFTER = timedelta(days=60)


def _instant(t: dict[str, Any], field: str) -> datetime:
    kind, v = parse_time(t, field)
    if kind == "timed":
        return v
    return datetime.combine(v, datetime.min.time(), tzinfo=ZoneInfo(t["time_zone"])).astimezone(timezone.utc)


def snapshot(ev: dict[str, Any]) -> dict[str, Any] | None:
    if ev.get("status") == "cancelled":
        return None
    return {"etag": ev.get("etag"), "summary": ev.get("summary"), "location": ev.get("location"),
            "start": ev["start"], "end": ev["end"],
            "start_utc": _instant(ev["start"], "start").isoformat(), "end_utc": _instant(ev["end"], "end").isoformat()}


def _in_window(snap: dict[str, Any] | None, now: datetime, window: timedelta) -> bool:
    if snap is None:
        return False
    return datetime.fromisoformat(snap["start_utc"]) < now + window and datetime.fromisoformat(snap["end_utc"]) > now


def _brief(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if snap is None else {k: snap[k] for k in ("summary", "location", "start", "end")}


def baseline_from(events: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    return prune({e["id"]: s for e in events if (s := snapshot(e)) is not None}, now)


def prune(baseline: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {k: v for k, v in baseline.items()
            if datetime.fromisoformat(v["end_utc"]) > now - RETAIN_BEFORE and datetime.fromisoformat(v["start_utc"]) < now + RETAIN_AFTER}


def missing_as_cancelled(baseline: dict[str, Any], full_list: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """After a full resync, events we knew about that are absent (and not yet over) were deleted."""
    present = {e["id"] for e in full_list}
    return [{"id": k, "etag": f"missing:{v['etag']}", "status": "cancelled"} for k, v in baseline.items()
            if k not in present and datetime.fromisoformat(v["end_utc"]) > now]


def evaluate(baseline: dict[str, Any], events: list[dict[str, Any]], now: datetime, window: timedelta,
             own_versions: set[tuple[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    new = dict(baseline)
    changes: list[dict[str, Any]] = []
    for ev in events:
        key = ev["id"]
        old = baseline.get(key)
        cur = snapshot(ev)
        if old is not None and cur is not None and old["etag"] == cur["etag"]:
            continue  # same version: redelivery or resync
        if old is None and cur is None:
            continue  # cancellation of an event we never tracked
        if cur is None:
            new.pop(key, None)
        else:
            new[key] = cur
        if (key, str(ev.get("etag"))) in own_versions:
            continue  # the automation's own change
        if not (_in_window(old, now, window) or _in_window(cur, now, window)):
            continue
        if cur is None:
            kind = "cancelled"
        elif old is None:
            kind = "added"
        elif (old["start_utc"], old["end_utc"]) != (cur["start_utc"], cur["end_utc"]):
            kind = "moved"
        elif (old["summary"], old["location"]) != (cur["summary"], cur["location"]):
            kind = "renamed" if old["summary"] != cur["summary"] else "relocated"
        else:
            continue  # a field we do not watch changed (e.g. a description); keep the new version silently
        changes.append({"event_id": key, "kind": kind, "version_key": f"{'cancelled' if cur is None else 'v'}:{ev.get('etag')}",
                        "before": _brief(old), "after": _brief(cur)})
    return changes, prune(new, now)
