"""Unit tests for sync state, the sync window and de-duplication."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from garmin_tcx_ai import sync_outputs, sync_state
from garmin_tcx_ai.models import (
    Activity,
    ParsedActivity,
    PrivacyInfo,
    SourceInfo,
    Trackpoint,
)

TAIPEI = timezone(timedelta(hours=8))


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=TAIPEI)


def _activity(
    start: datetime | None,
    file_name: str = "manual.tcx",
    tcx_id: str | None = None,
    trackpoints: int = 0,
) -> ParsedActivity:
    return ParsedActivity(
        source=SourceInfo(format="tcx", file_name=file_name, file_path=""),
        privacy=PrivacyInfo(gps_policy="keep"),
        activity=Activity(
            sport="Running",
            activity_id=tcx_id,
            start_time=start,
            total_time_seconds=1800.0,
            distance_meters=5000.0,
        ),
        trackpoints=[
            Trackpoint(trackpoint_index=i) for i in range(trackpoints)
        ],
    )


def _garmin(
    start: datetime | None,
    garmin_id: str,
    trackpoints: int = 0,
) -> ParsedActivity:
    return _activity(
        start,
        file_name=f"x_activity_{garmin_id}_running.tcx",
        tcx_id=start.isoformat() if start else None,
        trackpoints=trackpoints,
    )


# ---------------------------------------------------------------------------
# compute_sync_window
# ---------------------------------------------------------------------------


def test_window_first_run_starts_at_lookback_limit() -> None:
    window = sync_state.compute_sync_window(None, _at(2026, 10, 7))
    assert window == sync_state.SyncWindow(
        start_date=date(2026, 9, 7),
        end_date=date(2026, 10, 7),
        first_run=True,
        truncated=False,
    )


def test_window_applies_overlap_to_last_success() -> None:
    window = sync_state.compute_sync_window(
        _at(2026, 10, 5), _at(2026, 10, 7)
    )
    assert window.start_date == date(2026, 10, 3)
    assert window.end_date == date(2026, 10, 7)
    assert not window.first_run
    assert not window.truncated


def test_window_truncates_to_lookback_after_long_gap() -> None:
    window = sync_state.compute_sync_window(
        _at(2026, 8, 1), _at(2026, 10, 7)
    )
    assert window.start_date == date(2026, 9, 7)
    assert window.truncated


def test_window_exactly_at_lookback_is_not_truncated() -> None:
    window = sync_state.compute_sync_window(
        _at(2026, 9, 9), _at(2026, 10, 7), overlap_days=2
    )
    assert window.start_date == date(2026, 9, 7)
    assert not window.truncated


def test_window_uses_local_dates_not_utc() -> None:
    # 2026-10-06 23:30 UTC is already 2026-10-07 in Taipei.
    now = datetime(2026, 10, 6, 23, 30, tzinfo=UTC)
    window = sync_state.compute_sync_window(None, now)
    assert window.end_date == date(2026, 10, 7)
    utc_window = sync_state.compute_sync_window(None, now, "UTC")
    assert utc_window.end_date == date(2026, 10, 6)


def test_window_clock_skew_never_starts_after_today() -> None:
    window = sync_state.compute_sync_window(
        _at(2026, 10, 20), _at(2026, 10, 7)
    )
    assert window.start_date == date(2026, 10, 7)
    assert window.end_date == date(2026, 10, 7)


def test_window_custom_overlap_and_lookback() -> None:
    window = sync_state.compute_sync_window(
        _at(2026, 10, 5),
        _at(2026, 10, 7),
        overlap_days=0,
        max_lookback_days=1,
    )
    assert window.start_date == date(2026, 10, 6)
    assert window.truncated


@pytest.mark.parametrize(
    ("overlap", "lookback"), [(-1, 30), (2, 0)]
)
def test_window_rejects_invalid_limits(overlap: int, lookback: int) -> None:
    with pytest.raises(ValueError):
        sync_state.compute_sync_window(
            None,
            _at(2026, 10, 7),
            overlap_days=overlap,
            max_lookback_days=lookback,
        )


def test_window_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sync_state.compute_sync_window(None, datetime(2026, 10, 7))
    with pytest.raises(ValueError, match="timezone-aware"):
        sync_state.compute_sync_window(
            datetime(2026, 10, 5), _at(2026, 10, 7)
        )


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def test_missing_state_file_means_never_synced(tmp_path: Path) -> None:
    state = sync_state.load_sync_state(tmp_path / "sync_state.json")
    assert state.last_successful_sync is None


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "sync_state.json"
    state = sync_state.SyncState().with_success(_at(2026, 10, 7, 6))
    sync_state.save_sync_state(path, state)
    loaded = sync_state.load_sync_state(path)
    assert loaded.last_successful_sync == _at(2026, 10, 7, 6)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "last_successful_sync": "2026-10-07T06:00:00+08:00",
    }


def test_with_success_does_not_modify_original() -> None:
    original = sync_state.SyncState()
    updated = original.with_success(_at(2026, 10, 7))
    assert original.last_successful_sync is None
    assert updated.last_successful_sync == _at(2026, 10, 7)


def test_with_success_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sync_state.SyncState().with_success(datetime(2026, 10, 7))


def test_unsynced_state_is_saved_with_null(tmp_path: Path) -> None:
    path = tmp_path / "sync_state.json"
    sync_state.save_sync_state(path, sync_state.SyncState())
    assert sync_state.load_sync_state(path).last_successful_sync is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        "{}",
        '{"schema_version": "1"}',
        '{"schema_version": true}',
        '{"schema_version": 0}',
        '{"schema_version": 2, "last_successful_sync": null}',
        '{"schema_version": 1, "last_successful_sync": 5}',
        '{"schema_version": 1, "last_successful_sync": "garbage"}',
        '{"schema_version": 1, "last_successful_sync": "2026-10-07T06:00:00"}',
    ],
)
def test_untrusted_state_file_is_rejected_and_not_modified(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "sync_state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(sync_state.SyncStateError):
        sync_state.load_sync_state(path)
    assert path.read_text(encoding="utf-8") == content


def test_failed_run_leaves_state_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "sync_state.json"
    first = sync_state.SyncState().with_success(_at(2026, 10, 5))
    sync_state.save_sync_state(path, first)
    before = path.read_bytes()
    # A failed run never reaches save_sync_state; the file stays as is.
    state = sync_state.load_sync_state(path)
    assert state == first
    assert path.read_bytes() == before


def test_save_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "sync_state.json"
    sync_state.save_sync_state(path, sync_state.SyncState())
    assert [p.name for p in tmp_path.iterdir()] == ["sync_state.json"]


# ---------------------------------------------------------------------------
# deduplicate_activities
# ---------------------------------------------------------------------------


def test_dedupe_keeps_distinct_activities_in_order() -> None:
    first = _garmin(_at(2026, 10, 1), "111")
    second = _garmin(_at(2026, 10, 2), "222")
    result = sync_state.deduplicate_activities([first, second])
    assert result.kept == [first, second]
    assert result.dropped == []


def test_dedupe_removes_same_garmin_id() -> None:
    one = _garmin(_at(2026, 10, 1), "111")
    again = _garmin(_at(2026, 10, 1), "111")
    result = sync_state.deduplicate_activities([one, again])
    assert result.kept == [one]
    assert result.dropped == [again]


def test_dedupe_matches_manual_export_by_start_time() -> None:
    start = _at(2026, 10, 1)
    manual = _activity(start, tcx_id=start.astimezone(UTC).isoformat())
    downloaded = _garmin(start, "111")
    result = sync_state.deduplicate_activities([manual, downloaded])
    assert result.kept == [downloaded]
    assert result.dropped == [manual]


def test_dedupe_prefers_garmin_id_regardless_of_order() -> None:
    start = _at(2026, 10, 1)
    manual = _activity(start, tcx_id="manual")
    downloaded = _garmin(start, "111")
    forward = sync_state.deduplicate_activities([downloaded, manual])
    assert forward.kept == [downloaded]
    assert forward.dropped == [manual]


def test_dedupe_prefers_more_trackpoints_when_ids_tie() -> None:
    start = _at(2026, 10, 1)
    sparse = _garmin(start, "111", trackpoints=1)
    rich = _garmin(start, "111", trackpoints=5)
    result = sync_state.deduplicate_activities([sparse, rich])
    assert result.kept == [rich]
    assert result.dropped == [sparse]


def test_dedupe_ties_keep_the_first() -> None:
    start = _at(2026, 10, 1)
    first = _garmin(start, "111")
    second = _garmin(start, "111")
    result = sync_state.deduplicate_activities([first, second])
    assert result.kept == [first]
    assert result.dropped == [second]


def test_dedupe_ignores_sub_second_start_differences() -> None:
    start = _at(2026, 10, 1)
    one = _activity(start, tcx_id="a")
    other = _activity(start + timedelta(microseconds=400), tcx_id="b")
    assert len(sync_state.deduplicate_activities([one, other]).kept) == 1


def test_dedupe_never_merges_activities_without_any_identity() -> None:
    one = _activity(None, tcx_id=None)
    two = _activity(None, tcx_id=None)
    result = sync_state.deduplicate_activities([one, two])
    assert result.kept == [one, two]


def test_dedupe_keeps_different_ids_with_different_starts() -> None:
    one = _activity(_at(2026, 10, 1), tcx_id="a")
    two = _activity(_at(2026, 10, 1, 13), tcx_id="b")
    assert len(sync_state.deduplicate_activities([one, two]).kept) == 2


def test_dedupe_handles_empty_input() -> None:
    result = sync_state.deduplicate_activities([])
    assert result.kept == [] and result.dropped == []


def test_dedupe_result_feeds_activities_csv() -> None:
    start = _at(2026, 10, 1)
    manual = _activity(start, tcx_id="manual")
    downloaded = _garmin(start, "111")
    kept = sync_state.deduplicate_activities([manual, downloaded]).kept
    rows = sync_outputs.build_activity_rows(kept)
    assert [row["activity_id"] for row in rows] == ["111"]
