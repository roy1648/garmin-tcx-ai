"""``training_summary.md`` for the NAS ``current/`` folder.

The file is the first thing a chat AI reads after ``data_status.json``.
It starts with the freshness facts, then the rolling 7 and 28 day
totals, then the full ``summary.txt`` content for the last 28 days. All
numbers come from :mod:`garmin_tcx_ai.ai_text`, so this file cannot
disagree with ``summary.txt``. The text is factual only: no coaching,
no interpretation and no GPS coordinates.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from garmin_tcx_ai.ai_text import (
    _aggregate,
    _cell,
    _cell_km,
    _fmt_duration,
    _fmt_pace,
    _local,
    _longest,
    _zone,
    render_summary_text,
)
from garmin_tcx_ai.models import ParsedActivity
from garmin_tcx_ai.sync_outputs import atomic_write_text

TRAINING_SUMMARY_NAME = "training_summary.md"
SUMMARY_DAYS = 28
ROLLING_WINDOWS_DAYS = (7, 28)


def select_recent(
    activities: list[ParsedActivity],
    today: date,
    timezone_name: str = "Asia/Taipei",
    days: int = SUMMARY_DAYS,
) -> list[ParsedActivity]:
    """Return the activities of the last *days* local days.

    The range is inclusive of *today*. Activities without a start time
    cannot be placed on a day and are left out.

    Args:
        activities: Normalized activities in any order.
        today: Last day of the range.
        timezone_name: IANA zone used to decide local days.
        days: Length of the range in days.

    Returns:
        The matching activities.
    """
    zone = _zone(timezone_name)
    first = today - timedelta(days=days - 1)
    selected = []
    for item in activities:
        local = _local(item.activity.start_time, zone)
        if local is not None and first <= local.date() <= today:
            selected.append(item)
    return selected


def render_training_summary(
    activities: list[ParsedActivity],
    data_status: dict[str, Any],
    now: datetime,
    timezone_name: str = "Asia/Taipei",
    days: int = SUMMARY_DAYS,
    run_files: list[str] | None = None,
) -> str:
    """Render ``training_summary.md``.

    Args:
        activities: Every known activity; the last *days* days are used.
        data_status: The dict that is written to ``data_status.json``,
            so both files show the same freshness facts.
        now: The current time (timezone-aware).
        timezone_name: IANA zone used for local days and times.
        days: Length of the detailed range in days.
        run_files: Names of the ``runs/*.txt`` files, listed as a guide
            so no separate index file is needed.

    Returns:
        Markdown text with ``\\n`` line endings.

    Raises:
        ValueError: If *now* is naive.
    """
    if now.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    zone = _zone(timezone_name)
    today = now.astimezone(zone).date()
    first = today - timedelta(days=days - 1)
    recent = select_recent(activities, today, timezone_name, days)

    lines = [
        "# 跑步訓練摘要",
        "",
        "## 資料狀態",
        "",
        f"- 狀態: {data_status.get('status')}",
        f"- 最後成功同步: {_text(data_status.get('last_sync_at'))}",
        f"- 最後活動日期: {_text(data_status.get('last_activity_date'))}",
        f"- 本檔涵蓋: {first.isoformat()} ~ {today.isoformat()}"
        f" (最近 {days} 天, 本地日期, {timezone_name})",
        f"- 本檔跑步次數: {len(recent)}",
        "",
        "使用前請先確認狀態為 ok，且最後成功同步的時間夠新。"
        "狀態不是 ok 時，資料只更新到最後成功同步的時間，"
        "不能把之後沒有出現的活動當成沒有訓練。"
        "本檔只含跑步活動。",
        "",
        "## 最近 7 天與 28 天",
        "",
    ]
    pool = _dated_pool(activities, today, zone)
    lines.extend(_rolling_table(pool, today))
    lines.extend(
        [
            "",
            "## 詳細摘要",
            "",
            "以下為每週、每日與每次跑步的明細。",
            "",
            "```text",
        ]
    )
    detail = render_summary_text(
        recent, zone, timezone_name, coverage=(first, today)
    )
    lines.append(detail.rstrip("\n"))
    lines.extend(["```", ""])
    if run_files:
        lines.extend(
            ["## 單次跑步檔案", "", "每圈與軌跡取樣在 `runs/` 內：", ""]
        )
        lines.extend(f"- `runs/{name}`" for name in run_files)
        lines.append("")
    return "\n".join(lines)


def write_training_summary(
    activities: list[ParsedActivity],
    data_status: dict[str, Any],
    now: datetime,
    output_dir: Path,
    timezone_name: str = "Asia/Taipei",
    days: int = SUMMARY_DAYS,
    run_files: list[str] | None = None,
) -> Path:
    """Write ``training_summary.md`` into *output_dir* atomically.

    Args:
        activities: Every known activity; the last *days* days are used.
        data_status: The dict that is written to ``data_status.json``.
        now: The current time (timezone-aware).
        output_dir: Target folder, created when missing.
        timezone_name: IANA zone used for local days and times.
        days: Length of the detailed range in days.
        run_files: Names of the ``runs/*.txt`` files to list.

    Returns:
        The path of the written file.
    """
    text = render_training_summary(
        activities, data_status, now, timezone_name, days, run_files
    )
    return atomic_write_text(Path(output_dir) / TRAINING_SUMMARY_NAME, text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dated_pool(
    activities: list[ParsedActivity],
    today: date,
    zone: Any,
) -> list[tuple[date, ParsedActivity]]:
    """Pair each dated activity with its local day."""
    pool = []
    for item in activities:
        local = _local(item.activity.start_time, zone)
        if local is not None and local.date() <= today:
            pool.append((local.date(), item))
    return pool


def _rolling_table(
    pool: list[tuple[date, ParsedActivity]],
    today: date,
) -> list[str]:
    """Render the rolling-window totals as a Markdown table."""
    lines = [
        "| 區間 | 日期 | 次數 | 距離(km) | 時間 | 平均配速(/km) "
        "| 平均心率 | 最長單次(km) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for days in ROLLING_WINDOWS_DAYS:
        first = today - timedelta(days=days - 1)
        members = [item for day, item in pool if first <= day <= today]
        stats = _aggregate(members)
        longest = _longest(members)
        lines.append(
            "| "
            + " | ".join(
                [
                    f"最近 {days} 天",
                    f"{first.isoformat()} ~ {today.isoformat()}",
                    str(len(members)),
                    _cell_km(stats["meters"]),
                    _fmt_duration(stats["seconds"]) if members else "-",
                    _fmt_pace(stats["pace"]),
                    _cell(stats["avg_hr"]),
                    _cell_km(longest.activity.distance_meters)
                    if longest
                    else "-",
                ]
            )
            + " |"
        )
    return lines


def _text(value: object) -> str:
    """Render a status value, ``無`` for missing."""
    return "無" if value is None else str(value)


__all__ = [
    "ROLLING_WINDOWS_DAYS",
    "SUMMARY_DAYS",
    "TRAINING_SUMMARY_NAME",
    "render_training_summary",
    "select_recent",
    "write_training_summary",
]
