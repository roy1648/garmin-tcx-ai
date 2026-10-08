"""Unit tests for the NAS ``current/`` structured outputs."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from garmin_tcx_ai import sync_outputs
from garmin_tcx_ai.models import (
    Activity,
    ParsedActivity,
    PrivacyInfo,
    SourceInfo,
    Trackpoint,
)
from garmin_tcx_ai.normalizer import normalize_activity
from garmin_tcx_ai.parser import parse_tcx

FIXTURES = Path(__file__).parent / "fixtures"
TAIPEI = timezone(timedelta(hours=8))


def _activity(
    start: datetime | None,
    meters: float | None = 10000.0,
    seconds: float | None = 3000.0,
    file_name: str = "manual.tcx",
    tcx_id: str | None = "tcx-id",
    power: list[int] | None = None,
) -> ParsedActivity:
    points = [
        Trackpoint(trackpoint_index=i, power_watts=watts)
        for i, watts in enumerate(power or [])
    ]
    return ParsedActivity(
        source=SourceInfo(format="tcx", file_name=file_name, file_path=""),
        privacy=PrivacyInfo(gps_policy="keep"),
        activity=Activity(
            sport="Running",
            activity_id=tcx_id,
            start_time=start,
            total_time_seconds=seconds,
            distance_meters=meters,
            average_heart_rate_bpm=150,
            maximum_heart_rate_bpm=175,
        ),
        trackpoints=points,
    )


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# resolve_activity_id
# ---------------------------------------------------------------------------


def test_resolve_activity_id_prefers_garmin_id_from_file_name() -> None:
    item = _activity(
        None,
        file_name="2026-09-29_activity_12345678901_running.tcx",
    )
    assert sync_outputs.resolve_activity_id(item) == "12345678901"


def test_resolve_activity_id_falls_back_to_tcx_id() -> None:
    item = _activity(None, tcx_id="2026-09-29T06:00:00Z")
    assert sync_outputs.resolve_activity_id(item) == "2026-09-29T06:00:00Z"


def test_resolve_activity_id_none_when_nothing_available() -> None:
    item = _activity(None, tcx_id=None)
    assert sync_outputs.resolve_activity_id(item) is None


def test_resolve_activity_id_ignores_directories_in_path() -> None:
    item = _activity(None, file_name="/data/_activity_999_/plain.tcx")
    assert sync_outputs.resolve_activity_id(item) == "tcx-id"


# ---------------------------------------------------------------------------
# activities.csv
# ---------------------------------------------------------------------------


def test_csv_header_matches_documented_columns(tmp_path: Path) -> None:
    path = sync_outputs.write_activities_csv([], tmp_path)
    assert path.name == "activities.csv"
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(sync_outputs.ACTIVITIES_CSV_COLUMNS)


def test_csv_row_values_use_local_time_and_summary_formats(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 9, 29, 22, 30, tzinfo=UTC)  # 06:30 next day
    item = _activity(
        start,
        file_name="x_activity_42_running.tcx",
        power=[200, 210, 220],
    )
    path = sync_outputs.write_activities_csv([item], tmp_path)
    (row,) = _read_rows(path)
    assert row == {
        "activity_id": "42",
        "date": "2026-09-30",
        "start_time": "06:30",
        "distance_km": "10.00",
        "duration": "50:00",
        "avg_hr": "150",
        "max_hr": "175",
        "pace": "5:00",
        "power": "210",
    }


def test_csv_duration_over_one_hour_uses_hours() -> None:
    start = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)
    rows = sync_outputs.build_activity_rows(
        [_activity(start, meters=21097.5, seconds=7384.0)]
    )
    assert rows[0]["duration"] == "2:03:04"


def test_csv_missing_values_are_blank_cells() -> None:
    item = _activity(None, meters=None, seconds=None, tcx_id=None)
    item.activity.average_heart_rate_bpm = None
    item.activity.maximum_heart_rate_bpm = None
    (row,) = sync_outputs.build_activity_rows([item])
    assert set(row.values()) == {""}


def test_csv_rows_sorted_by_start_with_unknown_last() -> None:
    late = _activity(datetime(2026, 9, 30, tzinfo=UTC), tcx_id="late")
    early = _activity(datetime(2026, 9, 1, tzinfo=UTC), tcx_id="early")
    unknown = _activity(None, tcx_id="unknown")
    rows = sync_outputs.build_activity_rows([unknown, late, early])
    assert [row["activity_id"] for row in rows] == [
        "early",
        "late",
        "unknown",
    ]


def test_csv_respects_timezone_name() -> None:
    start = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)
    taipei = sync_outputs.build_activity_rows([_activity(start)])
    utc = sync_outputs.build_activity_rows([_activity(start)], "UTC")
    assert (taipei[0]["date"], taipei[0]["start_time"]) == (
        "2026-09-30",
        "04:00",
    )
    assert (utc[0]["date"], utc[0]["start_time"]) == (
        "2026-09-29",
        "20:00",
    )


def test_csv_rejects_invalid_timezone() -> None:
    with pytest.raises(ValueError):
        sync_outputs.build_activity_rows([], "Not/AZone")


def test_csv_from_real_fixture_has_no_gps(tmp_path: Path) -> None:
    parsed = normalize_activity(
        parse_tcx(FIXTURES / "minimal_running.tcx"), gps_policy="keep"
    )
    path = sync_outputs.write_activities_csv([parsed], tmp_path)
    text = path.read_text(encoding="utf-8")
    assert len(_read_rows(path)) == 1
    with_gps = [p for p in parsed.trackpoints if p.latitude is not None]
    assert with_gps
    for point in with_gps:
        assert str(point.latitude) not in text
        assert str(point.longitude) not in text


def test_csv_uses_lf_line_endings(tmp_path: Path) -> None:
    path = sync_outputs.write_activities_csv(
        [_activity(datetime(2026, 9, 1, tzinfo=UTC))], tmp_path
    )
    assert b"\r" not in path.read_bytes()


# ---------------------------------------------------------------------------
# data_status.json
# ---------------------------------------------------------------------------


def test_success_status_reports_latest_local_activity_date() -> None:
    items = [
        _activity(datetime(2026, 9, 28, 1, 0, tzinfo=UTC)),
        _activity(datetime(2026, 9, 29, 20, 0, tzinfo=UTC)),
    ]
    status = sync_outputs.build_data_status(
        items,
        last_sync_at=datetime(2026, 9, 30, 6, 10, 22, tzinfo=TAIPEI),
    )
    assert status == {
        "schema_version": 1,
        "status": "ok",
        "source": "garmin_tcx",
        "last_sync_at": "2026-09-30T06:10:22+08:00",
        "last_attempt_at": "2026-09-30T06:10:22+08:00",
        "last_activity_date": "2026-09-30",
        "activities": 2,
        "files": [],
        "last_error": None,
    }


def test_success_status_converts_sync_time_to_configured_zone() -> None:
    status = sync_outputs.build_data_status(
        [],
        last_sync_at=datetime(2026, 9, 29, 22, 10, 22, tzinfo=UTC),
    )
    assert status["last_sync_at"] == "2026-09-30T06:10:22+08:00"


def test_success_status_without_activities_has_null_date() -> None:
    status = sync_outputs.build_data_status(
        [],
        last_sync_at=datetime(2026, 9, 30, tzinfo=TAIPEI),
    )
    assert status["last_activity_date"] is None
    assert status["activities"] == 0


def test_status_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sync_outputs.build_data_status(
            [], last_sync_at=datetime(2026, 9, 30, 6, 0)
        )


def test_failure_status_keeps_previous_success_fields() -> None:
    previous = sync_outputs.build_data_status(
        [_activity(datetime(2026, 9, 28, 1, 0, tzinfo=UTC))],
        last_sync_at=datetime(2026, 9, 29, 6, 0, tzinfo=TAIPEI),
    )
    failed = sync_outputs.build_failure_status(
        previous,
        status="error",
        last_attempt_at=datetime(2026, 9, 30, 6, 0, tzinfo=TAIPEI),
        last_error="network timeout",
    )
    assert failed["status"] == "error"
    assert failed["last_sync_at"] == previous["last_sync_at"]
    assert failed["last_activity_date"] == previous["last_activity_date"]
    assert failed["activities"] == previous["activities"]
    assert failed["last_attempt_at"] == "2026-09-30T06:00:00+08:00"
    assert failed["last_error"] == "network timeout"


def test_failure_status_without_previous_has_no_sync_time() -> None:
    failed = sync_outputs.build_failure_status(
        None,
        status="auth_required",
        last_attempt_at=datetime(2026, 9, 30, 6, 0, tzinfo=TAIPEI),
        last_error="login required",
    )
    assert failed["status"] == "auth_required"
    assert failed["last_sync_at"] is None
    assert failed["last_activity_date"] is None
    assert failed["activities"] == 0


@pytest.mark.parametrize("bad", ["ok", "weird"])
def test_failure_status_rejects_non_failure_values(bad: str) -> None:
    with pytest.raises(ValueError):
        sync_outputs.build_failure_status(
            None,
            status=bad,  # type: ignore[arg-type]
            last_attempt_at=datetime(2026, 9, 30, tzinfo=TAIPEI),
            last_error="x",
        )


def test_write_data_status_roundtrips_as_utf8_json(tmp_path: Path) -> None:
    status = sync_outputs.build_data_status(
        [],
        last_sync_at=datetime(2026, 9, 30, tzinfo=TAIPEI),
        source="資料來源",
    )
    path = sync_outputs.write_data_status(status, tmp_path)
    assert path.name == "data_status.json"
    text = path.read_text(encoding="utf-8")
    assert "資料來源" in text
    assert text.endswith("\n")
    assert json.loads(text) == status


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


def test_writers_create_missing_folder_and_leave_no_temp_files(
    tmp_path: Path,
) -> None:
    target = tmp_path / "RunningData" / "current"
    sync_outputs.write_activities_csv([], target)
    sync_outputs.write_data_status(
        sync_outputs.build_data_status(
            [], last_sync_at=datetime(2026, 9, 30, tzinfo=TAIPEI)
        ),
        target,
    )
    assert sorted(p.name for p in target.iterdir()) == [
        "activities.csv",
        "data_status.json",
    ]


def test_failed_write_keeps_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = sync_outputs.write_activities_csv([], tmp_path)
    before = path.read_text(encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(sync_outputs.os, "replace", boom)
    item = _activity(datetime(2026, 9, 1, tzinfo=UTC))
    with pytest.raises(OSError):
        sync_outputs.write_activities_csv([item], tmp_path)

    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["activities.csv"]
