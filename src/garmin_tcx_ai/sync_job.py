"""One NAS sync: download new TCX files and rewrite ``current/``.

:func:`run_sync` returns a result and never prints or exits, so the
CLI, the daily schedule and the Telegram command can share it. A failed
run leaves ``last_successful_sync`` and the data files untouched and
only records the failure in ``data_status.json``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from garmin_tcx_ai.ai_text import _zone
from garmin_tcx_ai.credentials import redact_sensitive_text
from garmin_tcx_ai.garmin_auth import (
    AuthRequiredError,
    GarminAuthError,
    _translate,
    create_client_from_tokens,
    ensure_not_inside,
)
from garmin_tcx_ai.importers.garminconnect_importer import (
    download_activity_files,
)
from garmin_tcx_ai.models import ParsedActivity
from garmin_tcx_ai.normalizer import normalize_activity
from garmin_tcx_ai.parser import (
    TCXParseError,
    UnsupportedActivityError,
    parse_tcx,
)
from garmin_tcx_ai.sync_config import SyncConfig
from garmin_tcx_ai.sync_lock import (
    SyncAlreadyRunning,
    SyncLock,
    SyncLockUnsupported,
)
from garmin_tcx_ai.sync_outputs import (
    ACTIVITIES_CSV_NAME,
    CURRENT_DIR_NAME,
    DATA_STATUS_NAME,
    build_data_status,
    build_failure_status,
    write_activities_csv,
    write_data_status,
)
from garmin_tcx_ai.sync_runs import write_run_texts
from garmin_tcx_ai.sync_state import (
    SYNC_STATE_FILE_NAME,
    SyncStateError,
    compute_sync_window,
    deduplicate_activities,
    load_sync_state,
    save_sync_state,
)
from garmin_tcx_ai.sync_summary import (
    TRAINING_SUMMARY_NAME,
    write_training_summary,
)

RAW_DIR_NAME = "raw"
ACTIVITY_TYPE = "running"

SyncOutcome = Literal["ok", "error", "auth_required", "locked"]

#: ``sync`` exit codes, see docs/11_nas_sync_scope_change.md section 8.
EXIT_CODES: dict[str, int] = {
    "ok": 0,
    "error": 1,
    "auth_required": 3,
    "locked": 4,
}


@dataclass(frozen=True)
class SyncResult:
    """Outcome of :func:`run_sync`.

    Attributes:
        outcome: ``ok``, ``error``, ``auth_required`` or ``locked``.
        new_count: TCX files downloaded in this run.
        activities: Runs in ``current/`` after the run (0 on failure).
        message: Short reason on failure; never holds credentials.
    """

    outcome: SyncOutcome
    new_count: int = 0
    activities: int = 0
    message: str | None = None


def run_sync(
    config: SyncConfig,
    now: datetime | None = None,
    client_factory: Callable[[Path], Any] = create_client_from_tokens,
) -> SyncResult:
    """Run one sync.

    Args:
        config: Folders and time zone.
        now: Start time (timezone-aware); defaults to the current time.
        client_factory: Builds the logged-in client from the token
            folder; replaced in tests.

    Returns:
        The outcome. Failures are returned, not raised.
    """
    started = now or datetime.now().astimezone()
    current = Path(config.output_dir) / CURRENT_DIR_NAME
    try:
        ensure_not_inside(config.token_dir, config.output_dir)
        ensure_not_inside(config.state_dir, config.output_dir)
    except GarminAuthError as exc:
        return SyncResult("error", message=str(exc))

    try:
        with SyncLock(config.state_dir):
            return _sync_locked(config, started, current, client_factory)
    except SyncAlreadyRunning as exc:
        return SyncResult("locked", message=str(exc))
    except SyncLockUnsupported as exc:
        return SyncResult("error", message=str(exc))


def _sync_locked(
    config: SyncConfig,
    started: datetime,
    current: Path,
    client_factory: Callable[[Path], Any],
) -> SyncResult:
    """Do the sync while holding the lock."""
    state_path = Path(config.state_dir) / SYNC_STATE_FILE_NAME
    raw_dir = Path(config.state_dir) / RAW_DIR_NAME
    try:
        state = load_sync_state(state_path)
        window = compute_sync_window(
            state.last_successful_sync, started, config.timezone_name
        )
        client = client_factory(config.token_dir)
        listed = client.get_activities_by_date(
            window.start_date.isoformat(),
            window.end_date.isoformat(),
            ACTIVITY_TYPE,
            sortorder="asc",
        )
        outcome = download_activity_files(
            client, listed, raw_dir, ACTIVITY_TYPE
        )
        if outcome.failed_count:
            raise RuntimeError(
                f"{outcome.failed_count} activity download(s) failed."
            )
        activities = _load_activities(raw_dir)
        _write_current(current, activities, started, config)
        save_sync_state(state_path, state.with_success(started))
    except SyncStateError as exc:
        return _failed(current, config, started, "error", exc)
    except Exception as exc:
        error = (
            exc if isinstance(exc, GarminAuthError) else _translate(exc, ())
        )
        status = (
            "auth_required"
            if isinstance(error, AuthRequiredError)
            else "error"
        )
        return _failed(current, config, started, status, error)
    return SyncResult(
        "ok",
        new_count=outcome.downloaded_count,
        activities=len(activities),
    )


def _load_activities(raw_dir: Path) -> list[ParsedActivity]:
    """Parse every raw TCX file and drop duplicate runs.

    A file that cannot be parsed or is not a run is skipped; the other
    files still count.
    """
    found = []
    for path in sorted(raw_dir.glob("*.tcx")):
        try:
            found.append(
                normalize_activity(parse_tcx(path), gps_policy="remove")
            )
        except (TCXParseError, UnsupportedActivityError):
            continue
    return deduplicate_activities(found).kept


def _write_current(
    current: Path,
    activities: list[ParsedActivity],
    started: datetime,
    config: SyncConfig,
) -> None:
    """Rewrite ``current/``; ``data_status.json`` goes last."""
    zone_name = config.timezone_name
    today = started.astimezone(_zone(zone_name)).date()
    run_names = [
        path.name
        for path in write_run_texts(activities, today, current, zone_name)
    ]
    files = [
        ACTIVITIES_CSV_NAME,
        TRAINING_SUMMARY_NAME,
        *(f"runs/{name}" for name in run_names),
        DATA_STATUS_NAME,
    ]
    status = build_data_status(
        activities,
        last_sync_at=started,
        timezone_name=zone_name,
        files=files,
    )
    write_activities_csv(activities, current, zone_name)
    write_training_summary(
        activities, status, started, current, zone_name, run_files=run_names
    )
    write_data_status(status, current)


def _failed(
    current: Path,
    config: SyncConfig,
    started: datetime,
    status: Literal["error", "auth_required"],
    error: BaseException,
) -> SyncResult:
    """Record a failure in ``data_status.json`` and build the result."""
    message = redact_sensitive_text(str(error))[:300]
    try:
        previous = json.loads(
            (current / DATA_STATUS_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        previous = None
    write_data_status(
        build_failure_status(
            previous,
            status=status,
            last_attempt_at=started,
            last_error=message,
            timezone_name=config.timezone_name,
        ),
        current,
    )
    return SyncResult(status, message=message)
