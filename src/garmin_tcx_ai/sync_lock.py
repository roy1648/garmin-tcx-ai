"""A lock that allows only one sync to run at a time.

The scheduler inside the container and a manual ``docker exec`` run
must never sync at the same moment. The lock is an OS-level file lock,
so it is released automatically if the process dies and a crash can
never leave a stale lock behind. It needs a POSIX system (the NAS
container runs Linux).
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

SYNC_LOCK_NAME = "sync.lock"


class SyncAlreadyRunning(RuntimeError):
    """Another sync holds the lock."""


class SyncLockUnsupported(RuntimeError):
    """This platform has no file locking."""


class SyncLock:
    """Context manager holding the sync lock.

    Args:
        state_dir: Folder for the lock file (not a synced folder).
    """

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / SYNC_LOCK_NAME
        self._handle = None

    def __enter__(self) -> SyncLock:
        """Take the lock without waiting.

        Raises:
            SyncAlreadyRunning: If another sync holds it.
            SyncLockUnsupported: On a platform without ``fcntl``.
        """
        if fcntl is None:
            raise SyncLockUnsupported(
                "Sync locking needs a POSIX system."
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SyncAlreadyRunning(
                "Another sync is already running."
            ) from None
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock."""
        handle, self._handle = self._handle, None
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


__all__ = [
    "SYNC_LOCK_NAME",
    "SyncAlreadyRunning",
    "SyncLock",
    "SyncLockUnsupported",
]
