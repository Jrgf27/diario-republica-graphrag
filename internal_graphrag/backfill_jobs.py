"""Start and inspect one local historical backfill at a time."""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

LISBON = ZoneInfo("Europe/Lisbon")


class BackfillBusy(RuntimeError):
    """Another archive worker holds the shared snapshot lock."""


def _paths(data_dir: Path) -> tuple[Path, Path, Path]:
    return (
        data_dir / ".archive.lock",
        data_dir / "web_backfill.json",
        data_dir / "web_backfill_logs",
    )


def _write_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _lock_held(lock_path: Path) -> bool:
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        finished, _ = os.waitpid(pid, os.WNOHANG)
        if finished:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def start_backfill(
    start: date,
    end: date,
    series: list[str],
    max_publications: int = 0,
    data_dir: Path | None = None,
) -> dict:
    """Launch the existing archive code in a background process."""
    if start > end or end > datetime.now(LISBON).date():
        raise ValueError("Choose a valid date range ending no later than today.")
    if not series or set(series) - {"I", "II"}:
        raise ValueError("Choose Series I, Series II, or both.")
    if max_publications < 0:
        raise ValueError("The publication limit cannot be negative.")

    directory = Path(data_dir or os.getenv("GRAPHRAG_DATA_DIR", "/data"))
    directory.mkdir(parents=True, exist_ok=True)
    lock_path, state_path, log_dir = _paths(directory)
    log_dir.mkdir(exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackfillBusy("An archive backfill is already running.") from exc

        job_id = datetime.now(LISBON).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        state = {
            "id": job_id,
            "state": "starting",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "series": series,
            "max_publications": max_publications,
            "started_at": datetime.now(LISBON).isoformat(timespec="seconds"),
            "container": socket.gethostname(),
        }
        _write_state(state_path, state)
        args = ["--from", state["start"], "--to", state["end"]]
        for value in series:
            args.extend(("--series", value))
        if max_publications:
            args.extend(("--max-publications", str(max_publications)))
        command = [sys.executable, "-u", "-m", "internal_graphrag.backfill_worker", str(lock.fileno()), job_id, *args]
        log_path = log_dir / f"{job_id}.log"
        try:
            with log_path.open("w", encoding="utf-8") as log:
                subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    pass_fds=(lock.fileno(),),
                    start_new_session=True,
                )
        except OSError as exc:
            state["state"] = "failed"
            state["error"] = str(exc)
            _write_state(state_path, state)
            raise
        # The worker writes its own PID and state, avoiding a parent/child write race.
        return state


def backfill_status(data_dir: Path | None = None) -> dict | None:
    directory = Path(data_dir or os.getenv("GRAPHRAG_DATA_DIR", "/data"))
    lock_path, state_path, log_dir = _paths(directory)
    if not directory.exists():
        return None
    state = _read_state(state_path)
    held = _lock_held(lock_path)
    if state is None:
        return {"state": "external"} if held else None
    if state["state"] in {"starting", "running"}:
        if state["state"] == "starting" and held and state.get("container") == socket.gethostname():
            return state
        own_process = state.get("container") == socket.gethostname() and _pid_running(state.get("pid"))
        if not held or not own_process:
            state["state"] = "interrupted"
        else:
            state["state"] = "running"
    if held and state["state"] not in {"running", "starting"}:
        state["other_run_active"] = True
    job_id = state.get("id", "")
    if re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", job_id):
        log_path = log_dir / f"{job_id}.log"
        if log_path.exists():
            with log_path.open(encoding="utf-8", errors="replace") as log:
                from collections import deque

                state["log_tail"] = "".join(deque(log, maxlen=18))
    return state
