"""Unit tests for ``training_summary.md``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from garmin_tcx_ai import ai_text, sync_outputs, sync_summary
from garmin_tcx_ai.models import (
    Activity,
    ParsedActivity,
    PrivacyInfo,
    SourceInfo,
    Trackpoint,
)

TAIPEI = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 7, 6, 10, 22, tzinfo=TAIPEI)


def _run(
    day: date,
    km: float = 10.0,
    minutes: float = 50.0,
    hr: int = 150,
    name: str | None = None,
) -> ParsedActivity:
    start = datetime(day.year, day.month, day.day, 6, 0, tzinfo=TAIPEI)
    return ParsedActivity(
        source=SourceInfo(
            format="tcx",
            file_name=name or f"{day.isoformat()}_activity_1_running.tcx",
            file_path="",
        ),
        privacy=PrivacyInfo(gps_policy="remove"),
        activity=Activity(
            sport="Running",
            activity_id=day.isoformat(),
            start_time=start,
            total_time_seconds=minutes * 60,
            distance_meters=km * 1000,
            average_heart_rate_bpm=hr,
            maximum_heart_rate_bpm=hr + 20,
        ),
        trackpoints=[
            Trackpoint(trackpoint_index=0, latitude=25.1, longitude=121.5)
        ],
    )


def _status(activities: list[ParsedActivity]) -> dict:
    return sync_outputs.build_data_status(activities, last_sync_at=NOW)


def _render(activities: list[ParsedActivity], **kwargs) -> str:
    return sync_summary.render_training_summary(
        activities, _status(activities), NOW, **kwargs
    )


def test_select_recent_uses_inclusive_local_day_range() -> None:
    today = date(2026, 10, 7)
    inside_first = _run(today - timedelta(days=27))
    outside = _run(today - timedelta(days=28))
    inside_today = _run(today)
    future = _run(today + timedelta(days=1))
    undated = _run(today)
    undated.activity.start_time = None
    picked = sync_summary.select_recent(
        [outside, inside_first, inside_today, future, undated], today
    )
    assert picked == [inside_first, inside_today]


def test_select_recent_uses_local_days_not_utc() -> None:
    # 2026-10-06 22:00 UTC is already 2026-10-07 06:00 in Taipei.
    item = _run(date(2026, 10, 7))
    item.activity.start_time = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
    assert sync_summary.select_recent([item], date(2026, 10, 7)) == [item]
    assert sync_summary.select_recent(
        [item], date(2026, 10, 6), "UTC"
    ) == [item]


def test_header_shows_the_same_freshness_facts_as_data_status() -> None:
    runs = [_run(date(2026, 10, 6))]
    status = _status(runs)
    text = sync_summary.render_training_summary(runs, status, NOW)
    assert "- 狀態: ok" in text
    assert f"- 最後成功同步: {status['last_sync_at']}" in text
    assert "- 最後活動日期: 2026-10-06" in text
    assert "- 本檔涵蓋: 2026-09-10 ~ 2026-10-07" in text
    assert "- 本檔跑步次數: 1" in text


def test_rolling_windows_count_the_right_days() -> None:
    runs = [
        _run(date(2026, 10, 7), km=10),
        _run(date(2026, 10, 1), km=5),  # day 7 back, inside 7 days
        _run(date(2026, 9, 30), km=20),  # day 8 back, only in 28 days
        _run(date(2026, 9, 10), km=8),  # day 28 back, inside 28 days
        _run(date(2026, 9, 9), km=100),  # day 29 back, outside both
    ]
    text = _render(runs)
    rows = {
        line.split("|")[1].strip(): [c.strip() for c in line.split("|")[2:-1]]
        for line in text.splitlines()
        if line.startswith("| 最近")
    }
    seven = rows["最近 7 天"]
    twenty_eight = rows["最近 28 天"]
    assert seven[0] == "2026-10-01 ~ 2026-10-07"
    assert seven[1:3] == ["2", "15.00"]
    assert twenty_eight[0] == "2026-09-10 ~ 2026-10-07"
    assert twenty_eight[1:3] == ["4", "43.00"]
    assert twenty_eight[-1] == "20.00"  # longest single run


def test_rolling_windows_without_runs_show_dashes() -> None:
    text = _render([])
    assert "| 最近 7 天 | 2026-10-01 ~ 2026-10-07 | 0 | - | - | - | - | - |" in (
        text
    )


def test_detail_is_the_summary_txt_content_for_the_range() -> None:
    runs = [_run(date(2026, 10, 6)), _run(date(2026, 8, 1))]
    text = _render(runs)
    recent = [runs[0]]
    expected = ai_text.render_summary_text(
        recent,
        ai_text._zone("Asia/Taipei"),
        "Asia/Taipei",
        coverage=(date(2026, 9, 10), date(2026, 10, 7)),
    )
    assert expected.rstrip("\n") in text
    assert "2026-08-01" not in text
    assert "```text" in text


def test_summary_has_no_gps_or_coaching_words() -> None:
    text = _render([_run(date(2026, 10, 6))])
    assert "25.1" not in text
    assert "121.5" not in text
    for word in ("建議", "疲勞", "恢復", "加強"):
        assert word not in text


def test_failed_status_is_stated_plainly() -> None:
    previous = _status([_run(date(2026, 10, 1))])
    failed = sync_outputs.build_failure_status(
        previous,
        status="auth_required",
        last_attempt_at=NOW,
        last_error="login required",
    )
    text = sync_summary.render_training_summary([], failed, NOW)
    assert "- 狀態: auth_required" in text
    assert f"- 最後成功同步: {previous['last_sync_at']}" in text


def test_never_synced_status_shows_none() -> None:
    failed = sync_outputs.build_failure_status(
        None, status="error", last_attempt_at=NOW, last_error="x"
    )
    text = sync_summary.render_training_summary([], failed, NOW)
    assert "- 最後成功同步: 無" in text
    assert "- 最後活動日期: 無" in text


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sync_summary.render_training_summary(
            [], _status([]), datetime(2026, 10, 7)
        )


def test_write_training_summary_is_atomic_utf8(tmp_path: Path) -> None:
    runs = [_run(date(2026, 10, 6))]
    path = sync_summary.write_training_summary(
        runs, _status(runs), NOW, tmp_path / "current"
    )
    assert path.name == "training_summary.md"
    assert "跑步訓練摘要" in path.read_text(encoding="utf-8")
    assert b"\r" not in path.read_bytes()
    assert [p.name for p in path.parent.iterdir()] == [
        "training_summary.md"
    ]
