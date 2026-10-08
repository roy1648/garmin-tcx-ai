"""``runs/*.txt`` for the NAS ``current/`` folder.

One plain-text file per run of the last 28 local days, rendered by
:mod:`garmin_tcx_ai.ai_text` so the numbers match ``training_summary.md``.
Files of runs that left the range are deleted; the raw TCX files stay.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from garmin_tcx_ai.ai_text import (
    RUNS_DIR_NAME,
    _run_file_stem,
    _sort_key,
    _unique_name,
    _zone,
    render_activity_text,
)
from garmin_tcx_ai.models import ParsedActivity
from garmin_tcx_ai.sync_outputs import atomic_write_text
from garmin_tcx_ai.sync_summary import SUMMARY_DAYS, select_recent


def write_run_texts(
    activities: list[ParsedActivity],
    today: date,
    output_dir: Path,
    timezone_name: str = "Asia/Taipei",
    days: int = SUMMARY_DAYS,
) -> list[Path]:
    """Write ``runs/<date>_<HHMM>_<km>km.txt`` and drop stale files.

    Args:
        activities: Every known activity; the last *days* days are used.
        today: Last local day of the range.
        output_dir: ``current/`` folder; ``runs/`` is created inside it.
        timezone_name: IANA zone used for local days and times.
        days: Length of the range in days.

    Returns:
        The written paths, oldest run first.
    """
    zone = _zone(timezone_name)
    recent = sorted(
        select_recent(activities, today, timezone_name, days),
        key=_sort_key,
    )
    runs_dir = Path(output_dir) / RUNS_DIR_NAME
    used: set[str] = set()
    written = []
    for index, activity in enumerate(recent, start=1):
        name = _unique_name(_run_file_stem(activity, zone, index), used)
        text = render_activity_text(activity, zone, timezone_name)
        written.append(atomic_write_text(runs_dir / f"{name}.txt", text))
    for old in runs_dir.glob("*.txt"):
        if old not in written:
            old.unlink()
    return written
