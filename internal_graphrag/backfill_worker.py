"""Run the archive crawler while keeping the web job status current."""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .archive import main as archive_main
from .backfill_jobs import _write_state


def main() -> int:
    lock_fd, job_id, *archive_args = sys.argv[1:]
    # The inherited descriptor holds the same lock used by CLI archive runs.
    os.fstat(int(lock_fd))
    data_dir = Path(os.getenv("GRAPHRAG_DATA_DIR", "/data"))
    state_path = data_dir / "web_backfill.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state["id"] != job_id:
        raise RuntimeError("Backfill job state changed before the worker started")
    state["state"] = "running"
    state["pid"] = os.getpid()
    _write_state(state_path, state)
    print(f"Starting archive backfill {state['start']} to {state['end']} ({', '.join(state['series'])})", flush=True)
    exit_code = 1
    try:
        sys.argv = ["archive", *archive_args]
        os.environ["GRAPHRAG_ARCHIVE_LOCK_HELD"] = "1"
        exit_code = archive_main()
    except Exception:  # noqa: BLE001 - persist any worker failure for the UI
        traceback.print_exc()
    finally:
        state["state"] = "completed" if exit_code == 0 else "failed"
        state["finished_at"] = datetime.now(ZoneInfo("Europe/Lisbon")).isoformat(timespec="seconds")
        state["exit_code"] = exit_code
        _write_state(state_path, state)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
