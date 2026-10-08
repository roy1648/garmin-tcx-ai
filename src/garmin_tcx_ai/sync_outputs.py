"""Machine-readable ``current/`` outputs for NAS sync.

This module writes the two structured files that sit next to the
human-readable summary in the NAS ``current/`` folder:

* ``activities.csv`` -- one row per run so an AI (or a script) can
  locate a single activity quickly.
* ``data_status.json`` -- freshness information so a reader can tell
  whether the data is current before trusting it.

Calculations reuse :mod:`garmin_tcx_ai.ai_text` so ``summary.txt`` and
these files can never disagree. Neither file contains GPS coordinates.
Each file is written to a temporary file in the same folder and then
moved into place, so a folder-sync tool never uploads a half-written
file.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from garmin_tcx_ai.ai_text import (
    _avg,
    _local,
    _pace,
    _sort_key,
    _values,
    _zone,
)
from garmin_tcx_ai.models import ParsedActivity

CURRENT_DIR_NAME = "current"
ACTIVITIES_CSV_NAME = "activities.csv"
DATA_STATUS_NAME = "data_status.json"
DATA_STATUS_SCHEMA_VERSION = 1
DEFAULT_SOURCE = "garmin_tcx"

ACTIVITIES_CSV_COLUMNS = (
    "activity_id",
    "date",
    "start_time",
    "distance_km",
    "duration",
    "avg_hr",
    "max_hr",
    "pace",
    "power",
)

SyncStatus = Literal["ok", "error", "auth_required"]
SYNC_STATUSES: tuple[str, ...] = ("ok", "error", "auth_required")

# Garmin Connect downloads are named ``<start>_activity_<id>_<type>.tcx``.
_GARMIN_FILE_ID = re.compile(r"_activity_(\d+)_")


# ---------------------------------------------------------------------------
# Public API: activities.csv
# ---------------------------------------------------------------------------


def resolve_activity_id(activity: ParsedActivity) -> str | None:
    """Return the most stable identifier available for an activity.

    The Garmin activity ID embedded in a Garmin Connect download file
    name wins. Otherwise the TCX ``Id`` element (the start time for a
    manual export) is used. ``None`` means no identifier exists.

    Args:
        activity: A normalized activity.

    Returns:
        The identifier text, or ``None``.
    """
    match = _GARMIN_FILE_ID.search(Path(activity.source.file_name).name)
    if match:
        return match.group(1)
    return activity.activity.activity_id or None


def build_activity_rows(
    activities: list[ParsedActivity],
    timezone_name: str = "Asia/Taipei",
) -> list[dict[str, str]]:
    """Build ``activities.csv`` rows ordered by start time.

    Missing values become empty strings. Activities without a start
    time are placed last.

    Args:
        activities: Normalized activities in any order.
        timezone_name: IANA zone used for local dates and times.

    Returns:
        One dict per activity keyed by :data:`ACTIVITIES_CSV_COLUMNS`.
    """
    zone = _zone(timezone_name)
    rows: list[dict[str, str]] = []
    for item in sorted(activities, key=_sort_key):
        facts = item.activity
        local_start = _local(facts.start_time, zone)
        pace = _pace(facts.total_time_seconds, facts.distance_meters)
        power = _avg(_values(item.trackpoints, "power_watts"))
        rows.append(
            {
                "activity_id": resolve_activity_id(item) or "",
                "date": local_start.date().isoformat()
                if local_start
                else "",
                "start_time": local_start.strftime("%H:%M")
                if local_start
                else "",
                "distance_km": _format_km(facts.distance_meters),
                "duration": _format_clock(facts.total_time_seconds),
                "avg_hr": _text(facts.average_heart_rate_bpm),
                "max_hr": _text(facts.maximum_heart_rate_bpm),
                "pace": _format_clock(pace),
                "power": _text(power),
            }
        )
    return rows


def render_activities_csv(
    activities: list[ParsedActivity],
    timezone_name: str = "Asia/Taipei",
) -> str:
    """Render ``activities.csv`` content with a header row.

    Args:
        activities: Normalized activities in any order.
        timezone_name: IANA zone used for local dates and times.

    Returns:
        CSV text using ``\\n`` line endings.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=ACTIVITIES_CSV_COLUMNS,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(build_activity_rows(activities, timezone_name))
    return buffer.getvalue()


def write_activities_csv(
    activities: list[ParsedActivity],
    output_dir: Path,
    timezone_name: str = "Asia/Taipei",
) -> Path:
    """Write ``activities.csv`` into *output_dir*.

    Args:
        activities: Normalized activities in any order.
        output_dir: Target folder, created when missing.
        timezone_name: IANA zone used for local dates and times.

    Returns:
        The path of the written file.
    """
    text = render_activities_csv(activities, timezone_name)
    return atomic_write_text(Path(output_dir) / ACTIVITIES_CSV_NAME, text)


# ---------------------------------------------------------------------------
# Public API: data_status.json
# ---------------------------------------------------------------------------


def build_data_status(
    activities: list[ParsedActivity],
    *,
    last_sync_at: datetime,
    timezone_name: str = "Asia/Taipei",
    source: str = DEFAULT_SOURCE,
    files: list[str] | None = None,
) -> dict[str, Any]:
    """Build the status of a sync that finished successfully.

    Args:
        activities: Every activity that ended up in ``activities.csv``.
        last_sync_at: When the sync finished (timezone-aware).
        timezone_name: IANA zone used to render times and dates.
        source: Label of the data source.
        files: Paths of the files written in this run, relative to
            ``current/``. ``data_status.json`` is written last, so it
            marks the run as complete.

    Returns:
        A JSON-serializable dict with ``status`` set to ``"ok"``.

    Raises:
        ValueError: If *last_sync_at* is naive.
    """
    zone = _zone(timezone_name)
    synced = _aware_local(last_sync_at, zone)
    local_days = [
        local.date()
        for local in (
            _local(item.activity.start_time, zone) for item in activities
        )
        if local is not None
    ]
    return {
        "schema_version": DATA_STATUS_SCHEMA_VERSION,
        "status": "ok",
        "source": source,
        "last_sync_at": synced.isoformat(timespec="seconds"),
        "last_attempt_at": synced.isoformat(timespec="seconds"),
        "last_activity_date": _iso_day(max(local_days, default=None)),
        "activities": len(activities),
        "files": files or [],
        "last_error": None,
    }


def build_failure_status(
    previous: dict[str, Any] | None,
    *,
    status: SyncStatus,
    last_attempt_at: datetime,
    last_error: str,
    timezone_name: str = "Asia/Taipei",
    source: str = DEFAULT_SOURCE,
) -> dict[str, Any]:
    """Build the status after a failed sync attempt.

    The data files were not replaced, so ``last_sync_at``,
    ``last_activity_date`` and ``activities`` keep describing the data
    that is actually on disk. A failure is never reported as a newer
    successful sync.

    Args:
        previous: The status read before this attempt, or ``None``
            when no sync has ever succeeded.
        status: ``"error"`` or ``"auth_required"``.
        last_attempt_at: When the failed attempt happened.
        last_error: A short message. Callers must not pass credentials.
        timezone_name: IANA zone used to render times.
        source: Label of the data source.

    Returns:
        A JSON-serializable dict.

    Raises:
        ValueError: If *status* is ``"ok"`` or unknown, or
            *last_attempt_at* is naive.
    """
    if status not in SYNC_STATUSES or status == "ok":
        raise ValueError(f"Invalid failure status: {status!r}")
    zone = _zone(timezone_name)
    attempt = _aware_local(last_attempt_at, zone)
    base = previous or {}
    return {
        "schema_version": DATA_STATUS_SCHEMA_VERSION,
        "status": status,
        "source": base.get("source", source),
        "last_sync_at": base.get("last_sync_at"),
        "last_attempt_at": attempt.isoformat(timespec="seconds"),
        "last_activity_date": base.get("last_activity_date"),
        "activities": base.get("activities", 0),
        "files": base.get("files", []),
        "last_error": last_error,
    }


def write_data_status(
    status: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write ``data_status.json`` into *output_dir*.

    Args:
        status: A dict from :func:`build_data_status` or
            :func:`build_failure_status`.
        output_dir: Target folder, created when missing.

    Returns:
        The path of the written file.
    """
    text = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(Path(output_dir) / DATA_STATUS_NAME, text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def atomic_write_text(target: Path, text: str) -> Path:
    """Write UTF-8 text to *target* via a same-folder temporary file.

    Args:
        target: Destination file; its folder is created when missing.
        text: Content to write, using ``\\n`` line endings.

    Returns:
        The destination path.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def _aware_local(value: datetime, zone: Any) -> datetime:
    """Return *value* in *zone*, rejecting naive datetimes."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(zone)


def _iso_day(value: date | None) -> str | None:
    """Return an ISO date string or ``None``."""
    return value.isoformat() if value else None


def _text(value: object) -> str:
    """Render a CSV cell, empty for missing values."""
    return "" if value is None else str(value)


def _format_km(meters: float | None) -> str:
    """Format metres as kilometres with two decimals, or empty."""
    if meters is None:
        return ""
    return f"{meters / 1000.0:.2f}"


def _format_clock(seconds: float | None) -> str:
    """Format seconds as ``h:mm:ss`` (or ``m:ss`` below an hour)."""
    if seconds is None or seconds < 0:
        return ""
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


__all__ = [
    "ACTIVITIES_CSV_COLUMNS",
    "ACTIVITIES_CSV_NAME",
    "CURRENT_DIR_NAME",
    "DATA_STATUS_NAME",
    "DATA_STATUS_SCHEMA_VERSION",
    "SYNC_STATUSES",
    "atomic_write_text",
    "build_activity_rows",
    "build_data_status",
    "build_failure_status",
    "render_activities_csv",
    "resolve_activity_id",
    "write_activities_csv",
    "write_data_status",
]
