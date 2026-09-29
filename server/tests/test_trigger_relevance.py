"""AUTO-02 relevance rules as pure functions."""
from datetime import datetime, timedelta, timezone

from opa.scheduler.triggers import baseline_from, evaluate, missing_as_cancelled

NOW = datetime(2026, 9, 28, 19, 0, tzinfo=timezone.utc)  # 12:00 PDT
W = timedelta(hours=48)


def ev(eid, start, end, etag="e1", summary="Meeting", **kw):
    return {"id": eid, "etag": etag, "summary": summary, "location": None, "status": "confirmed",
            "start": {"date_time": start, "date": None, "time_zone": "America/Los_Angeles"},
            "end": {"date_time": end, "date": None, "time_zone": "America/Los_Angeles"}, **kw}


BASE = [ev("a", "2026-09-29T10:00:00", "2026-09-29T11:00:00"), ev("far", "2026-10-05T10:00:00", "2026-10-05T11:00:00")]


def test_initial_baseline_and_same_version_is_not_a_change():
    base = baseline_from(BASE, NOW)
    changes, _ = evaluate(base, BASE, NOW, W, set())
    assert changes == []


def test_move_inside_window_fires_once():
    base = baseline_from(BASE, NOW)
    moved = ev("a", "2026-09-29T14:00:00", "2026-09-29T15:00:00", etag="e2")
    changes, base2 = evaluate(base, [moved], NOW, W, set())
    assert [(c["event_id"], c["kind"]) for c in changes] == [("a", "moved")]
    assert changes[0]["before"]["start"]["date_time"] == "2026-09-29T10:00:00"
    again, _ = evaluate(base2, [moved], NOW, W, set())
    assert again == []


def test_changes_outside_window_are_ignored_but_moves_into_or_out_of_it_count():
    base = baseline_from(BASE, NOW)
    far_moved = ev("far", "2026-10-06T10:00:00", "2026-10-06T11:00:00", etag="e2")
    assert evaluate(base, [far_moved], NOW, W, set())[0] == []
    into = ev("far", "2026-09-29T16:00:00", "2026-09-29T17:00:00", etag="e3")
    assert [c["kind"] for c in evaluate(base, [into], NOW, W, set())[0]] == ["moved"]
    out = ev("a", "2026-10-09T10:00:00", "2026-10-09T11:00:00", etag="e4")
    assert [c["kind"] for c in evaluate(base, [out], NOW, W, set())[0]] == ["moved"]


def test_cancel_add_rename_and_unwatched_fields():
    base = baseline_from(BASE, NOW)
    cancel = {"id": "a", "etag": "e9", "status": "cancelled"}
    added = ev("new", "2026-09-29T09:00:00", "2026-09-29T09:30:00")
    renamed = ev("a", "2026-09-29T10:00:00", "2026-09-29T11:00:00", etag="e5", summary="Design review v2")
    desc_only = ev("a", "2026-09-29T10:00:00", "2026-09-29T11:00:00", etag="e6", description="notes")
    assert [c["kind"] for c in evaluate(base, [cancel], NOW, W, set())[0]] == ["cancelled"]
    assert [c["kind"] for c in evaluate(base, [added], NOW, W, set())[0]] == ["added"]
    assert [c["kind"] for c in evaluate(base, [renamed], NOW, W, set())[0]] == ["renamed"]
    changes, b = evaluate(base, [desc_only], NOW, W, set())
    assert changes == [] and b["a"]["etag"] == "e6"


def test_own_changes_are_suppressed_but_tracked():
    base = baseline_from(BASE, NOW)
    mine = ev("a", "2026-09-29T12:00:00", "2026-09-29T13:00:00", etag="mine1")
    changes, b = evaluate(base, [mine], NOW, W, {("a", "mine1")})
    assert changes == [] and b["a"]["etag"] == "mine1"


def test_full_resync_detects_deletions_without_refiring_unchanged_events():
    base = baseline_from(BASE, NOW)
    full = [BASE[1]]  # "a" disappeared from the full list
    synth = missing_as_cancelled(base, full, NOW)
    changes, _ = evaluate(base, full + synth, NOW, W, set())
    assert [(c["event_id"], c["kind"]) for c in changes] == [("a", "cancelled")]
