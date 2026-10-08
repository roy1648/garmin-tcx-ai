"""Incremental sync state, date window and activity de-duplication.

This module holds the pure logic behind NAS incremental sync:

* :class:`SyncState` remembers ``last_successful_sync`` in a small JSON
  file. The file is only ever replaced by an explicit
  :func:`save_sync_state` call, so a failed run leaves it untouched.
* :func:`compute_sync_window` turns that timestamp into the date range
  to fetch: the last success minus an overlap (to catch activities that
  were uploaded late), never reaching further back than the maximum
  look-back.
* :func:`deduplicate_activities` removes activities that appear more
  than once, for example from overlapping downloads or from a manual
  export of an activity that was also downloaded.

Nothing here talks to the network or to Garmin Connect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from garmin_tcx_ai.ai_text import _aware, _zone
from garmin_tcx_ai.models import ParsedActivity
from garmin_tcx_ai.sync_outputs import atomic_write_text, resolve_activity_id

SYNC_STATE_SCHEMA_VERSION = 1
SYNC_STATE_FILE_NAME = "sync_state.json"
DEFAULT_OVERLAP_DAYS = 2
MAX_LOOKBACK_DAYS = 30


class SyncStateError(RuntimeError):
    """Raised when the sync state file cannot be trusted.

    The caller must stop instead of guessing, so a damaged or newer
    state file is never overwritten.
    """


@dataclass(frozen=True)
class SyncState:
    """Persistent state of the incremental sync.

    Attributes:
        last_successful_sync: When the last fully successful run
            started (timezone-aware), or ``None`` if none has succeeded.
    """

    last_successful_sync: datetime | None = None

    def with_success(self, started_at: datetime) -> SyncState:
        """Return a state recording a fully successful run.

        Pass the time the run *started*, not when it finished: an
        activity uploaded while the run was in progress is then still
        inside the next window.

        Args:
            started_at: Timezone-aware start time of the run.

        Returns:
            A new state; this one is not modified.

        Raises:
            ValueError: If *started_at* is naive.
        """
        if started_at.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return SyncState(last_successful_sync=started_at)


@dataclass(frozen=True)
class SyncWindow:
    """Inclusive local date range to fetch.

    Attributes:
        start_date: First day to fetch.
        end_date: Last day to fetch (today in the configured zone).
        first_run: ``True`` when no earlier success was known.
        truncated: ``True`` when the look-back limit shortened the
            range, so activities older than ``start_date`` may be
            missing.
    """

    start_date: date
    end_date: date
    first_run: bool
    truncated: bool


@dataclass(frozen=True)
class DedupeResult:
    """Outcome of :func:`deduplicate_activities`.

    Attributes:
        kept: One activity per real-world run, in input order.
        dropped: The duplicates that were removed.
    """

    kept: list[ParsedActivity]
    dropped: list[ParsedActivity]


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


def compute_sync_window(
    last_successful_sync: datetime | None,
    now: datetime,
    timezone_name: str = "Asia/Taipei",
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    max_lookback_days: int = MAX_LOOKBACK_DAYS,
) -> SyncWindow:
    """Compute the date range for the next sync.

    ``start = max(last_success_day - overlap_days, today - max_lookback)``
    where days are local dates in *timezone_name*. Without a previous
    success the range starts at the look-back limit.

    Args:
        last_successful_sync: Start time of the last successful run, or
            ``None``.
        now: Current time (timezone-aware).
        timezone_name: IANA zone used to decide the local dates.
        overlap_days: Days to re-fetch before the last success.
        max_lookback_days: Furthest back the window may reach.

    Returns:
        The window to fetch.

    Raises:
        ValueError: If a datetime is naive, ``overlap_days`` is
            negative, or ``max_lookback_days`` is less than 1.
    """
    if overlap_days < 0:
        raise ValueError("overlap_days must not be negative")
    if max_lookback_days < 1:
        raise ValueError("max_lookback_days must be at least 1")
    for value in (now, last_successful_sync):
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")

    zone = _zone(timezone_name)
    today = now.astimezone(zone).date()
    limit = today - timedelta(days=max_lookback_days)
    if last_successful_sync is None:
        return SyncWindow(limit, today, first_run=True, truncated=False)

    last_day = last_successful_sync.astimezone(zone).date()
    wanted = min(last_day - timedelta(days=overlap_days), today)
    start = max(wanted, limit)
    return SyncWindow(
        start_date=start,
        end_date=today,
        first_run=False,
        truncated=wanted < limit,
    )


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def load_sync_state(path: Path) -> SyncState:
    """Read the sync state file.

    A missing file means "never synced" and returns an empty state. An
    unreadable, malformed or newer-than-supported file raises, because
    guessing could hide a problem or overwrite data written by a newer
    version.

    Args:
        path: Location of the state file.

    Returns:
        The stored state.

    Raises:
        SyncStateError: If the file exists but cannot be trusted.
    """
    path = Path(path)
    if not path.exists():
        return SyncState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncStateError(
            f"Cannot read sync state file {path.name}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise SyncStateError(f"Sync state file {path.name} is not an object")

    version = raw.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise SyncStateError(
            f"Sync state file {path.name} has no valid schema_version"
        )
    if version > SYNC_STATE_SCHEMA_VERSION:
        raise SyncStateError(
            f"Sync state file {path.name} uses schema_version {version}, "
            f"newer than supported {SYNC_STATE_SCHEMA_VERSION}"
        )
    if version < 1:
        raise SyncStateError(
            f"Sync state file {path.name} has invalid schema_version "
            f"{version}"
        )
    return SyncState(
        last_successful_sync=_parse_timestamp(
            raw.get("last_successful_sync"), path.name
        )
    )


def save_sync_state(path: Path, state: SyncState) -> Path:
    """Write the sync state file atomically.

    Call this only after the whole sync succeeded.

    Args:
        path: Location of the state file.
        state: State to store.

    Returns:
        The written path.
    """
    last = state.last_successful_sync
    payload = {
        "schema_version": SYNC_STATE_SCHEMA_VERSION,
        "last_successful_sync": last.isoformat(timespec="seconds")
        if last
        else None,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(Path(path), text)


def _parse_timestamp(value: object, file_name: str) -> datetime | None:
    """Parse a stored timestamp, rejecting naive or malformed values."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SyncStateError(
            f"Sync state file {file_name} has an invalid timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SyncStateError(
            f"Sync state file {file_name} has an invalid timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise SyncStateError(
            f"Sync state file {file_name} has a timestamp without timezone"
        )
    return parsed


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


def deduplicate_activities(
    activities: list[ParsedActivity],
) -> DedupeResult:
    """Remove activities that describe the same run.

    Two activities are the same run when they share an identifier from
    :func:`~garmin_tcx_ai.sync_outputs.resolve_activity_id`, or share a
    start time to the second. The second rule catches a manual export
    (identified by its start time) of an activity that was also
    downloaded (identified by its Garmin ID). Activities with neither
    identifier nor start time are never merged.

    Of two duplicates, the one with a Garmin activity ID is kept, then
    the one with more trackpoints, then the earlier in the input.

    Args:
        activities: Normalized activities in any order.

    Returns:
        The kept activities (in input order) and the dropped ones.
    """
    kept: list[ParsedActivity | None] = []
    dropped: list[ParsedActivity] = []
    by_id: dict[str, int] = {}
    by_start: dict[datetime, int] = {}

    for item in activities:
        keys = _identity_keys(item)
        index = by_id.get(keys[0]) if keys[0] else None
        if index is None and keys[1] is not None:
            index = by_start.get(keys[1])

        if index is None:
            index = len(kept)
            kept.append(item)
        else:
            current = kept[index]
            if _prefer(item, current):
                kept[index] = item
                dropped.append(current)
            else:
                dropped.append(item)
                continue

        if keys[0]:
            by_id[keys[0]] = index
        if keys[1] is not None:
            by_start[keys[1]] = index

    return DedupeResult(
        kept=[item for item in kept if item is not None],
        dropped=dropped,
    )


def _identity_keys(
    item: ParsedActivity,
) -> tuple[str | None, datetime | None]:
    """Return the (identifier, start-to-the-second) pair of *item*."""
    start = item.activity.start_time
    start_key = (
        _aware(start).replace(microsecond=0) if start is not None else None
    )
    return resolve_activity_id(item), start_key


def _prefer(candidate: ParsedActivity, current: ParsedActivity) -> bool:
    """Return ``True`` when *candidate* should replace *current*."""
    candidate_rank = _rank(candidate)
    current_rank = _rank(current)
    return candidate_rank > current_rank


def _rank(item: ParsedActivity) -> tuple[bool, int]:
    """Rank an activity: Garmin ID first, then trackpoint count."""
    identifier = resolve_activity_id(item) or ""
    return identifier.isdigit(), len(item.trackpoints)


__all__ = [
    "DEFAULT_OVERLAP_DAYS",
    "MAX_LOOKBACK_DAYS",
    "SYNC_STATE_FILE_NAME",
    "SYNC_STATE_SCHEMA_VERSION",
    "DedupeResult",
    "SyncState",
    "SyncStateError",
    "SyncWindow",
    "compute_sync_window",
    "deduplicate_activities",
    "load_sync_state",
    "save_sync_state",
]
