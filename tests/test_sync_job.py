"""Unit tests for the NAS sync job, its settings and the CLI command."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from garmin_tcx_ai import cli, sync_job
from garmin_tcx_ai.garmin_auth import AuthRequiredError
from garmin_tcx_ai.sync_config import SyncConfig, load_sync_config
from garmin_tcx_ai.sync_lock import SyncAlreadyRunning

TAIPEI = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 7, 6, 10, tzinfo=TAIPEI)
FIXTURE = Path(__file__).parent / "fixtures" / "minimal_running.tcx"


def _tcx(day: str) -> bytes:
    return FIXTURE.read_text(encoding="utf-8").replace(
        "2000-01-01", day
    ).encode("utf-8")


class FakeClient:
    """Stands in for the Garmin client; no network."""

    def __init__(self, ids: dict[int, str], fail: bool = False) -> None:
        self.ids = ids
        self.fail = fail
        self.windows: list[tuple[str, str]] = []

    def get_activities_by_date(self, start, end, kind, sortorder=None):
        self.windows.append((start, end))
        return [
            {"activityId": i, "startTimeLocal": f"{day} 06:00:00"}
            for i, day in self.ids.items()
        ]

    def download_activity(self, activity_id, fmt):
        if self.fail:
            raise OSError("boom")
        return _tcx(self.ids[int(activity_id)])


@pytest.fixture(autouse=True)
def _no_real_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real lock needs fcntl (POSIX); it has its own tests.
    monkeypatch.setattr(sync_job, "SyncLock", lambda _dir: nullcontext())


@pytest.fixture
def config(tmp_path: Path) -> SyncConfig:
    return SyncConfig(
        output_dir=tmp_path / "RunningData",
        state_dir=tmp_path / "state",
        token_dir=tmp_path / "state" / "secrets",
    )


def _status(config: SyncConfig) -> dict:
    path = config.output_dir / "current" / "data_status.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _run(config: SyncConfig, client: FakeClient, now=NOW):
    return sync_job.run_sync(config, now, lambda _dir: client)


def test_success_writes_current_and_state(config: SyncConfig) -> None:
    result = _run(config, FakeClient({1: "2026-10-06"}))
    assert (result.outcome, result.new_count, result.activities) == (
        "ok", 1, 1,
    )
    current = config.output_dir / "current"
    status = _status(config)
    assert status["status"] == "ok"
    assert status["files"] == [
        "activities.csv",
        "training_summary.md",
        "runs/2026-10-06_0800_0.20km.txt",
        "data_status.json",
    ]
    assert all((current / name).is_file() for name in status["files"])
    assert (config.state_dir / "sync_state.json").is_file()


def test_second_run_downloads_nothing_new(config: SyncConfig) -> None:
    client = FakeClient({1: "2026-10-06"})
    _run(config, client)
    result = _run(config, client)
    assert (result.new_count, result.activities) == (0, 1)
    assert client.windows[1][0] == "2026-10-05"  # 2 days overlap


def test_download_failure_keeps_state_and_reports_error(
    config: SyncConfig,
) -> None:
    result = _run(config, FakeClient({1: "2026-10-06"}, fail=True))
    assert result.outcome == "error"
    assert not (config.state_dir / "sync_state.json").exists()
    status = _status(config)
    assert status["status"] == "error"
    assert status["last_sync_at"] is None


def test_failure_after_success_keeps_old_sync_time(
    config: SyncConfig,
) -> None:
    _run(config, FakeClient({1: "2026-10-06"}))
    later = NOW + timedelta(days=1)
    _run(config, FakeClient({2: "2026-10-07"}, fail=True), later)
    status = _status(config)
    assert status["status"] == "error"
    assert status["last_sync_at"].startswith("2026-10-07T06:10")
    assert status["files"]


def test_auth_failure_is_reported_as_auth_required(
    config: SyncConfig,
) -> None:
    def no_login(_dir: Path):
        raise AuthRequiredError("No saved Garmin login.")

    result = sync_job.run_sync(config, NOW, no_login)
    assert result.outcome == "auth_required"
    assert _status(config)["status"] == "auth_required"
    assert not (config.state_dir / "sync_state.json").exists()


def test_running_sync_is_reported_and_writes_nothing(
    config: SyncConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(_dir):
        raise SyncAlreadyRunning("Another sync is already running.")

    monkeypatch.setattr(sync_job, "SyncLock", busy)
    result = _run(config, FakeClient({}))
    assert result.outcome == "locked"
    assert not (config.output_dir / "current").exists()


def test_state_folder_inside_output_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "RunningData"
    bad = SyncConfig(out, out / "state", tmp_path / "tokens")
    result = _run(bad, FakeClient({}))
    assert result.outcome == "error"
    assert not (out / "current").exists()


def test_damaged_state_file_is_not_overwritten(config: SyncConfig) -> None:
    config.state_dir.mkdir(parents=True)
    state = config.state_dir / "sync_state.json"
    state.write_text("{not json", encoding="utf-8")
    result = _run(config, FakeClient({1: "2026-10-06"}))
    assert result.outcome == "error"
    assert state.read_text(encoding="utf-8") == "{not json"


def test_config_needs_output_and_state_dirs() -> None:
    with pytest.raises(ValueError, match="SYNC_OUTPUT_DIR"):
        load_sync_config({"SYNC_STATE_DIR": "/s"})


def test_config_defaults_token_dir_inside_state_dir() -> None:
    loaded = load_sync_config(
        {"SYNC_OUTPUT_DIR": "/o", "SYNC_STATE_DIR": "/s"}
    )
    assert loaded.token_dir == Path("/s/secrets")
    assert loaded.timezone_name == "Asia/Taipei"


def test_cli_sync_without_settings_exits_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNC_OUTPUT_DIR", raising=False)
    assert cli.main(["sync"]) == 2


@pytest.mark.parametrize(
    ("outcome", "code"),
    [("ok", 0), ("error", 1), ("auth_required", 3), ("locked", 4)],
)
def test_cli_sync_maps_outcome_to_exit_code(
    monkeypatch: pytest.MonkeyPatch, outcome: str, code: int
) -> None:
    monkeypatch.setenv("SYNC_OUTPUT_DIR", "/o")
    monkeypatch.setenv("SYNC_STATE_DIR", "/s")
    monkeypatch.setattr(
        cli, "run_sync", lambda _cfg: sync_job.SyncResult(outcome)
    )
    assert cli.main(["sync"]) == code
