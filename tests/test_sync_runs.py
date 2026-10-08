"""Unit tests for ``runs/*.txt`` in the NAS ``current/`` folder."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from garmin_tcx_ai import sync_outputs, sync_runs, sync_summary
from tests.test_sync_summary import NOW, _run

TODAY = date(2026, 10, 7)


def test_writes_one_file_per_run_in_range(tmp_path: Path) -> None:
    runs = [_run(TODAY), _run(TODAY - timedelta(days=27))]
    runs.append(_run(TODAY - timedelta(days=28)))  # out of range
    paths = sync_runs.write_run_texts(runs, TODAY, tmp_path)
    assert [p.name for p in paths] == [
        "2026-09-10_0600_10.00km.txt",
        "2026-10-07_0600_10.00km.txt",
    ]
    assert all(p.read_text(encoding="utf-8") for p in paths)


def test_stale_run_files_are_removed_but_others_kept(
    tmp_path: Path,
) -> None:
    old = _run(TODAY - timedelta(days=27))
    sync_runs.write_run_texts([old, _run(TODAY)], TODAY, tmp_path)
    next_day = TODAY + timedelta(days=1)
    sync_runs.write_run_texts([old, _run(TODAY)], next_day, tmp_path)
    names = sorted(p.name for p in (tmp_path / "runs").iterdir())
    assert names == ["2026-10-07_0600_10.00km.txt"]


def test_same_start_and_distance_get_unique_names(tmp_path: Path) -> None:
    twin = [_run(TODAY, name="a.tcx"), _run(TODAY, name="b.tcx")]
    paths = sync_runs.write_run_texts(twin, TODAY, tmp_path)
    assert len({p.name for p in paths}) == 2


def test_run_text_has_no_gps(tmp_path: Path) -> None:
    (path,) = sync_runs.write_run_texts([_run(TODAY)], TODAY, tmp_path)
    assert "25.1" not in path.read_text(encoding="utf-8")


def test_summary_lists_run_files() -> None:
    runs = [_run(TODAY)]
    status = sync_outputs.build_data_status(runs, last_sync_at=NOW)
    text = sync_summary.render_training_summary(
        runs, status, NOW, run_files=["a.txt"]
    )
    assert "`runs/a.txt`" in text
    assert "runs/" not in sync_summary.render_training_summary(
        runs, status, NOW
    )


def test_status_lists_files_and_failure_keeps_them() -> None:
    ok = sync_outputs.build_data_status(
        [], last_sync_at=NOW, files=["activities.csv"]
    )
    assert ok["files"] == ["activities.csv"]
    failed = sync_outputs.build_failure_status(
        ok, status="error", last_attempt_at=NOW, last_error="x"
    )
    assert failed["files"] == ["activities.csv"]
