"""Unit tests for the sync lock."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from garmin_tcx_ai import sync_lock

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="needs fcntl file locking"
)


@posix_only
def test_second_sync_is_refused_while_the_first_runs(
    tmp_path: Path,
) -> None:
    with sync_lock.SyncLock(tmp_path):
        with pytest.raises(sync_lock.SyncAlreadyRunning):
            with sync_lock.SyncLock(tmp_path):
                pass


@posix_only
def test_lock_is_released_on_exit_and_on_error(tmp_path: Path) -> None:
    with sync_lock.SyncLock(tmp_path):
        pass
    with pytest.raises(RuntimeError, match="boom"):
        with sync_lock.SyncLock(tmp_path):
            raise RuntimeError("boom")
    with sync_lock.SyncLock(tmp_path):
        pass


@posix_only
def test_a_lock_held_by_another_process_is_respected(
    tmp_path: Path,
) -> None:
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from garmin_tcx_ai.sync_lock import SyncLock\n"
        "with SyncLock(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(30)\n"
    )
    src = str(Path(sync_lock.__file__).resolve().parents[1])
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": src},
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(sync_lock.SyncAlreadyRunning):
            with sync_lock.SyncLock(tmp_path):
                pass
    finally:
        child.kill()
        child.wait()
    # A killed process never leaves a stale lock behind.
    with sync_lock.SyncLock(tmp_path):
        pass


@posix_only
def test_lock_file_lives_in_the_state_folder(tmp_path: Path) -> None:
    with sync_lock.SyncLock(tmp_path / "state"):
        assert (tmp_path / "state" / "sync.lock").exists()


def test_platform_without_fcntl_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_lock, "fcntl", None)
    with pytest.raises(sync_lock.SyncLockUnsupported):
        with sync_lock.SyncLock(tmp_path):
            pass
