"""Settings of the NAS sync, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from garmin_tcx_ai.garmin_auth import TOKEN_DIR_ENV

OUTPUT_DIR_ENV = "SYNC_OUTPUT_DIR"
STATE_DIR_ENV = "SYNC_STATE_DIR"
TIMEZONE_ENV = "SYNC_TIMEZONE"
DEFAULT_TIMEZONE = "Asia/Taipei"
SECRETS_DIR_NAME = "secrets"


@dataclass(frozen=True)
class SyncConfig:
    """Folders and time zone of one NAS sync.

    Attributes:
        output_dir: Synced folder (``RunningData/``); files go to its
            ``current/`` sub-folder.
        state_dir: Folder for sync state, raw TCX and the lock. Not
            synced.
        token_dir: Folder with the Garmin login tokens. Not synced.
        timezone_name: IANA zone used for local days.
    """

    output_dir: Path
    state_dir: Path
    token_dir: Path
    timezone_name: str = DEFAULT_TIMEZONE


def load_sync_config(
    env: Mapping[str, str] | None = None,
) -> SyncConfig:
    """Build a :class:`SyncConfig` from environment variables.

    Args:
        env: Variables to read; defaults to ``os.environ``.

    Returns:
        The settings. The token folder defaults to ``secrets/`` inside
        the state folder.

    Raises:
        ValueError: If the output or state folder is not set.
    """
    values = os.environ if env is None else env
    missing = [
        name
        for name in (OUTPUT_DIR_ENV, STATE_DIR_ENV)
        if not values.get(name)
    ]
    if missing:
        raise ValueError(f"Set {', '.join(missing)}.")
    state_dir = Path(values[STATE_DIR_ENV])
    return SyncConfig(
        output_dir=Path(values[OUTPUT_DIR_ENV]),
        state_dir=state_dir,
        token_dir=Path(
            values.get(TOKEN_DIR_ENV) or state_dir / SECRETS_DIR_NAME
        ),
        timezone_name=values.get(TIMEZONE_ENV) or DEFAULT_TIMEZONE,
    )
