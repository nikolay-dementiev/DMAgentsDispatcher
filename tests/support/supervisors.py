"""Waits for the detached supervisors of a temporary account to exit before it is removed.

A run is TERMINAL before its supervisor has finished: the sandbox is retired and the lock released
afterwards. A test that deletes the account the moment `wait` returns can race those last steps,
and a supervisor that then fails writes its fault into a store that is being removed.
"""

import contextlib
import os
import sqlite3
import time
from pathlib import Path

POLL_S = 0.02


def wait_for_supervisors(store_file: Path, *, timeout_s: float) -> list[int]:
    """Wait until every supervisor recorded in the store is gone; return the pids still alive."""
    if not store_file.exists():
        return []
    connection = sqlite3.connect(f"file:{store_file}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT supervisor_pid FROM runs WHERE supervisor_pid IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    # A fixture may record pid 1 or 0 as a stand-in; neither is a supervisor that will exit.
    pending = sorted({int(row[0]) for row in rows if int(row[0]) > 1})
    deadline = time.monotonic() + timeout_s
    while pending and time.monotonic() < deadline:
        pending = [pid for pid in pending if _alive(pid)]
        if pending:
            time.sleep(POLL_S)
    return pending


def _alive(pid: int) -> bool:
    """Return whether a process with this pid exists (a zombie of ours counts as gone)."""
    with contextlib.suppress(ChildProcessError):
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
