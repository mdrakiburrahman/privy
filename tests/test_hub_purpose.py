from concurrent.futures import ThreadPoolExecutor

import pytest

from privy.hub.purpose import MAX_PURPOSE_CHARS, RETENTION_S, PurposeHistory, normalize_purpose


@pytest.mark.parametrize("value", [None, "", " \t\n "])
def test_empty_purpose_is_unknown(value):
    assert normalize_purpose(value) is None


def test_normalization_and_character_boundary():
    assert normalize_purpose("  privy:  Fix\n the hub. ") == "privy: Fix the hub."
    assert normalize_purpose("é" * MAX_PURPOSE_CHARS) == "é" * MAX_PURPOSE_CHARS


@pytest.mark.parametrize("value", [12, False, [], "\0bad", "\x1b[31m", "\ud800", "x" * 241])
def test_invalid_purpose_is_rejected(value):
    with pytest.raises(ValueError, match="purpose"):
        normalize_purpose(value)


@pytest.fixture
def history():
    now = [1_000_000.0]
    return PurposeHistory(clock=lambda: now[0]), now


def record(store, now, value, target="dev/one"):
    now[0] += 10
    store.record(target, value)


def test_consecutive_groups_and_unknown_breaks(history):
    store, now = history
    for purpose in ("A", "A", "B", "A", None, None, "A"):
        record(store, now, purpose)
    page = store.history("dev/one")
    assert [g["purpose"] for g in page["groups"]] == ["A", None, "A", "B", "A"]
    assert [g["count"] for g in page["groups"]] == [1, 2, 1, 1, 2]
    assert page["groups"][-1]["last_at"] > page["groups"][-1]["first_at"]


def test_unknown_does_not_erase_last_report_and_never_tagged_stays_blank(history):
    store, now = history
    record(store, now, "A")
    record(store, now, None)
    record(store, now, None, "dev/blank")
    summaries = store.summaries(["dev/one", "dev/blank"], start=now[0] - 100, end=now[0])
    assert summaries["dev/one"]["latest"] == "A"
    assert summaries["dev/one"]["latest_unknown"] is True
    assert summaries["dev/one"]["segments"][-1]["kind"] == "unknown"
    assert summaries["dev/blank"]["latest"] is None
    assert summaries["dev/blank"]["segments"] == []


def test_same_short_name_in_other_relay_is_independent(history):
    store, now = history
    record(store, now, "A", "dev/one")
    record(store, now, "B", "lab/one")
    assert store.history("dev/one")["groups"][0]["purpose"] == "A"
    assert store.history("lab/one")["groups"][0]["purpose"] == "B"


def test_expiry_preserves_latest_for_process_lifetime_but_new_store_resets(history):
    store, now = history
    record(store, now, "A")
    record(store, now, "B")
    now[0] += RETENTION_S + 1
    record(store, now, None)
    summary = store.summaries(["dev/one"], start=now[0] - 60, end=now[0])["dev/one"]
    assert summary["latest"] == "B"
    assert summary["latest_unknown"]
    assert "A" not in [g["purpose"] for g in store.history("dev/one")["groups"]]
    fresh = PurposeHistory(clock=lambda: now[0])
    assert fresh.history("dev/one")["groups"] == []
    assert fresh.summaries(["dev/one"], start=now[0] - 60, end=now[0])["dev/one"]["latest"] is None


def test_pagination_is_stable_and_cursor_is_scoped(history):
    store, now = history
    for purpose in ("A", "B", "C", "D"):
        record(store, now, purpose)
    first = store.history("dev/one", limit=2)
    record(store, now, "E")
    second = store.history("dev/one", cursor=first["next_cursor"], limit=2)
    assert [g["purpose"] for g in first["groups"]] == ["D", "C"]
    assert [g["purpose"] for g in second["groups"]] == ["B", "A"]
    assert second["next_cursor"] is None
    for target, cursor in [("lab/one", first["next_cursor"]), ("dev/one", "invalid")]:
        with pytest.raises(ValueError, match="cursor"):
            store.history(target, cursor=cursor)
    with pytest.raises(ValueError, match="cursor"):
        PurposeHistory().history("dev/one", cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="cursor"):
        store.history("dev/one", range_name="7d", cursor=first["next_cursor"])


def test_snapshots_are_detached_and_invalid_reads_fail(history):
    store, now = history
    record(store, now, "A")
    store.history("dev/one")["groups"][0]["purpose"] = "changed"
    assert store.history("dev/one")["groups"][0]["purpose"] == "A"
    for kwargs in ({"limit": 0}, {"limit": 201}, {"range_name": "forever"}):
        with pytest.raises(ValueError):
            store.history("dev/one", **kwargs)


def test_timeline_clips_to_window_and_now(history):
    store, now = history
    record(store, now, "A")
    record(store, now, None)
    record(store, now, "B")
    now[0] += 10
    start, end = now[0] - 25, now[0]
    segments = store.summaries(["dev/one"], start=start, end=end)["dev/one"]["segments"]
    assert [s["kind"] for s in segments] == ["purpose", "unknown", "purpose"]
    assert segments[0]["start"] == start and segments[-1]["end"] == end
    assert all(start <= s["start"] <= s["end"] <= end for s in segments)


def test_dense_overview_is_bounded_and_does_not_invent_dominant_purpose(history):
    store, now = history
    start = now[0]
    for i in range(500):
        record(store, now, str(i % 2))
    summary = store.summaries(["dev/one"], start=start, end=now[0])["dev/one"]
    assert len(summary["segments"]) <= 120
    assert any(s["kind"] == "mixed" for s in summary["segments"])
    assert len(store.history("dev/one", limit=200)["groups"]) == 200


def test_concurrent_equal_timestamp_reports_are_not_lost(history):
    store, now = history
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.record("dev/one", "A"), range(200)))
    assert store.history("dev/one")["groups"][0]["count"] == 200
    store.record("dev/one", None)
    assert [g["purpose"] for g in store.history("dev/one")["groups"]] == [None, "A"]
