from datetime import date, timedelta

import pytest

from internal_graphrag import backfill_jobs


def test_backfill_rejects_invalid_range_before_starting(tmp_path):
    today = backfill_jobs.datetime.now(backfill_jobs.LISBON).date()
    with pytest.raises(ValueError, match="valid date range"):
        backfill_jobs.start_backfill(today, today - timedelta(days=1), ["I"], data_dir=tmp_path)
    with pytest.raises(ValueError, match="Series"):
        backfill_jobs.start_backfill(date(2025, 1, 1), date(2025, 1, 2), [], data_dir=tmp_path)


def test_backfill_starts_existing_archive_worker_with_validated_arguments(tmp_path, monkeypatch):
    launched = {}

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["options"] = kwargs
        return object()

    monkeypatch.setattr(backfill_jobs.subprocess, "Popen", fake_popen)
    job = backfill_jobs.start_backfill(
        date(2025, 1, 1), date(2025, 1, 2), ["I"], 10, data_dir=tmp_path
    )
    assert job["state"] == "starting"
    assert launched["command"][-6:] == [
        "--to", "2025-01-02", "--series", "I", "--max-publications", "10",
    ]
    assert launched["options"]["pass_fds"]
