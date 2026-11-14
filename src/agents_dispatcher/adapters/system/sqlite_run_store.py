"""The durable run record in one SQLite file (schema version 2, M2-provisional).

Durability follows the probe digest: rollback journal in DELETE mode, `synchronous=EXTRA`,
`fullfsync=ON`, every pragma read back, and every write inside `BEGIN IMMEDIATE` so a second
writer waits or fails instead of interleaving. Each operation uses its own short connection.
"""

import fcntl
import os
import shutil
import sqlite3
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import (
    AcceptedRun,
    FinishRecord,
    ProcessIdentity,
    RunSnapshot,
)
from agents_dispatcher.domain.run import LimitKind, Limits, RunId, RunState, TerminalReason
from agents_dispatcher.domain.views import (
    StopDisposition,
    StopInfo,
    escape_controls,
    stop_disposition,
)

SCHEMA_VERSION = 3
_FAULT_ADDED_IN = 3
BACKUP_SUFFIX = ".v1.bak"
M1_LOCK_NAME = "supervisor.lock"
_SYNCHRONOUS_EXTRA = 3
_CREATE_META = "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)"
_RUNS = """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        task TEXT NOT NULL DEFAULT '',
        base_commit TEXT,
        branch TEXT NOT NULL,
        state TEXT NOT NULL,
        terminal_reason TEXT,
        started_at REAL NOT NULL,
        finished_at REAL,
        pgid INTEGER,
        pg_start_time REAL,
        cli_version TEXT,
        limit_hit TEXT,
        result_branch TEXT,
        commits_on_branch INTEGER,
        worker_exit_status INTEGER,
        duration_s REAL,
        hard_s REAL,
        memory_bytes INTEGER,
        supervisor_pid INTEGER,
        supervisor_start_time REAL,
        stop_requested_at REAL,
        stop_reason TEXT,
        report_json TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        fault TEXT
    )"""
_V1_COLUMNS = (
    "run_id, repo, base_commit, branch, state, terminal_reason, started_at, finished_at,"
    " pgid, pg_start_time, cli_version, limit_hit, result_branch, commits_on_branch,"
    " worker_exit_status, duration_s"
)


def write_warning(message: str) -> None:
    """Print a warning line to standard error; `sys.stderr` is read at call time."""
    sys.stderr.write(f"agentsd: warning: {escape_controls(message)}\n")


@dataclass(frozen=True)
class StoredRun:
    """A run as read back from the store."""

    run_id: RunId
    repo: str
    task: str
    base_commit: str | None
    branch: str
    state: RunState
    terminal_reason: TerminalReason | None
    started_at: float
    finished_at: float | None
    pgid: int | None
    pg_start_time: float | None
    cli_version: str | None
    limit_hit: LimitKind | None
    result_branch: str | None
    commits_on_branch: int | None
    worker_exit_status: int | None
    duration_s: float | None
    limits: Limits | None
    supervisor_pid: int | None
    supervisor_start_time: float | None
    stop: StopInfo | None
    report_json: str | None
    revision: int
    fault: str | None


_COLUMNS = (
    "run_id, repo, task, base_commit, branch, state, terminal_reason, started_at,"
    " finished_at, pgid, pg_start_time, cli_version, limit_hit, result_branch,"
    " commits_on_branch, worker_exit_status, duration_s, hard_s, memory_bytes,"
    " supervisor_pid, supervisor_start_time, stop_requested_at, stop_reason, report_json,"
    " revision, fault"
)
_SELECT = f"SELECT {_COLUMNS} FROM runs"  # noqa: S608 - constant column list


def _stored_run(row: tuple[Any, ...]) -> StoredRun:
    """Build a `StoredRun` from a row of `_SELECT`."""
    return StoredRun(
        run_id=RunId(row[0]),
        repo=row[1],
        task=row[2],
        base_commit=row[3],
        branch=row[4],
        state=RunState[row[5]],
        terminal_reason=TerminalReason[row[6]] if row[6] else None,
        started_at=row[7],
        finished_at=row[8],
        pgid=row[9],
        pg_start_time=row[10],
        cli_version=row[11],
        limit_hit=LimitKind[row[12]] if row[12] else None,
        result_branch=row[13],
        commits_on_branch=row[14],
        worker_exit_status=row[15],
        duration_s=row[16],
        limits=Limits(row[17], row[18]) if row[17] is not None and row[18] is not None else None,
        supervisor_pid=row[19],
        supervisor_start_time=row[20],
        stop=StopInfo(row[21], row[22]) if row[21] is not None else None,
        report_json=row[23],
        revision=row[24],
        fault=row[25],
    )


def _default_connect(path: str) -> sqlite3.Connection:
    """Open a connection in autocommit mode so transactions are explicit."""
    return sqlite3.connect(path, isolation_level=None)


class SqliteRunStore:
    """A `RunStore` backed by a private SQLite file."""

    def __init__(
        self,
        path: str,
        *,
        epoch_s: Callable[[], float],
        busy_s: float,
        connect: Callable[[str], sqlite3.Connection] = _default_connect,
        warn: Callable[[str], None] = write_warning,
    ) -> None:
        """Create the private file and the schema, or verify an existing store.

        `busy_s` is the `storage.sqlite_busy_s` setting; there is no default here.
        """
        self._path = path
        self._epoch_s = epoch_s
        self._busy_ms = round(busy_s * 1000)
        self._connect = connect
        _create_private_file(path)
        _remove_unheld_m1_lock(path, warn)
        with self._write() as connection:
            self._prepare_schema(connection)

    def set_state(self, run_id: RunId, state: RunState) -> None:
        """Store a state change; TERMINAL is reached only through `finish`."""
        if state is RunState.TERMINAL:
            message = "run store: TERMINAL is stored by finish, not by set_state"
            raise RunStoreError(message)
        self._update(run_id, "state = ?", (state.name,))

    def set_process(self, run_id: RunId, identity: ProcessIdentity) -> None:
        """Store the worker group identity."""
        self._update(run_id, "pgid = ?, pg_start_time = ?", (identity.pgid, identity.start_time_s))

    def accept(self, run: AcceptedRun) -> None:
        """Store a new run in state ACCEPTED with what `start` knows."""
        with self._write() as connection:
            try:
                connection.execute(
                    "INSERT INTO runs (run_id, repo, task, branch, state, started_at, hard_s,"
                    " memory_bytes, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        run.run_id.value,
                        run.repo,
                        run.task,
                        run.run_id.branch_name,
                        RunState.ACCEPTED.name,
                        self._epoch_s(),
                        run.limits.hard_s,
                        run.limits.memory_bytes,
                    ),
                )
            except sqlite3.IntegrityError as error:
                message = f"run store: run {run.run_id.value} is already stored"
                raise RunStoreError(message) from error

    def claim(self, run_id: RunId, supervisor: ProcessIdentity) -> bool:
        """Move ACCEPTED to STARTING for this supervisor; False when the run was not ACCEPTED."""
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE runs SET state = ?, supervisor_pid = ?, supervisor_start_time = ?,"
                " revision = revision + 1 WHERE run_id = ? AND state = ?",
                (
                    RunState.STARTING.name,
                    supervisor.pgid,
                    supervisor.start_time_s,
                    run_id.value,
                    RunState.ACCEPTED.name,
                ),
            )
            return cursor.rowcount == 1

    def record_workspace(self, run_id: RunId, *, base_commit: str, cli_version: str) -> None:
        """Store the facts the supervisor learns once the worktree exists."""
        self._update(run_id, "base_commit = ?, cli_version = ?", (base_commit, cli_version))

    def record_fault(self, run_id: RunId, text: str) -> None:
        """Store why the supervisor gave up; the sweep puts it in the report of the closed run."""
        self._update(run_id, "fault = ?", (text,))

    def request_stop(self, run_id: RunId, reason: str) -> StopDisposition | None:
        """Record a stop request atomically with the state check; None when the run is unknown."""
        with self._write() as connection:
            row = connection.execute(
                "SELECT state, stop_requested_at FROM runs WHERE run_id = ?", (run_id.value,)
            ).fetchone()
            if row is None:
                return None
            disposition = stop_disposition(RunState[row[0]], already_requested=row[1] is not None)
            if disposition is StopDisposition.EFFECTIVE:
                connection.execute(
                    "UPDATE runs SET stop_requested_at = ?, stop_reason = ?,"
                    " revision = revision + 1 WHERE run_id = ?",
                    (self._epoch_s(), reason, run_id.value),
                )
            return disposition

    def finish(self, run_id: RunId, record: FinishRecord) -> None:
        """Store the terminal outcome, the report and the state TERMINAL in one write."""
        self._update(run_id, *self._finish_assignments(record))

    def finish_unclaimed(self, run_id: RunId, record: FinishRecord) -> bool:
        """Close a run nobody took over; False when it is no longer ACCEPTED."""
        assignments, values = self._finish_assignments(record)
        with self._write() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {assignments}, revision = revision + 1"  # noqa: S608
                " WHERE run_id = ? AND state = ?",
                (*values, run_id.value, RunState.ACCEPTED.name),
            )
            return cursor.rowcount == 1

    def _finish_assignments(self, record: FinishRecord) -> tuple[str, tuple[object, ...]]:
        """Return the column assignments and values that make a run TERMINAL."""
        return (
            (
                "state = ?, terminal_reason = ?, result_branch = ?, commits_on_branch = ?,"
                " limit_hit = ?, worker_exit_status = ?, duration_s = ?, finished_at = ?,"
                " report_json = ?"
            ),
            (
                RunState.TERMINAL.name,
                record.terminal_reason.name,
                record.branch,
                record.commits_on_branch,
                record.limit_hit.name if record.limit_hit else None,
                record.worker_exit_status,
                record.duration_s,
                self._epoch_s(),
                record.report_json,
            ),
        )

    def stop_requested(self, run_id: RunId) -> bool:
        """Return whether a stop request is recorded for the run."""
        with self._read() as connection:
            row = connection.execute(
                "SELECT stop_requested_at FROM runs WHERE run_id = ?", (run_id.value,)
            ).fetchone()
        return row is not None and row[0] is not None

    def stop_reason(self, run_id: RunId) -> str | None:
        """Return the reason recorded with the run's stop request, or None."""
        with self._read() as connection:
            row = connection.execute(
                "SELECT stop_reason FROM runs WHERE run_id = ?", (run_id.value,)
            ).fetchone()
        return None if row is None else row[0]

    def find(self, run_id: RunId) -> RunSnapshot | None:
        """Return the run as the read side sees it, or None when it is unknown."""
        with self._read() as connection:
            row = connection.execute(_SELECT + " WHERE run_id = ?", (run_id.value,)).fetchone()
        if row is None:
            return None
        stored = _stored_run(row)
        terminal = stored.state is RunState.TERMINAL
        return RunSnapshot(
            run_id=stored.run_id,
            task=stored.task,
            state=stored.state,
            terminal_reason=stored.terminal_reason,
            branch=stored.result_branch if terminal else stored.branch,
            revision=stored.revision,
            stop=stored.stop,
            report_json=stored.report_json,
            fault=stored.fault,
        )

    def get(self, run_id: RunId) -> StoredRun:
        """Return the stored run, or raise when it is unknown."""
        with self._read() as connection:
            row = connection.execute(_SELECT + " WHERE run_id = ?", (run_id.value,)).fetchone()
        if row is None:
            message = f"run store: run {run_id.value} is unknown"
            raise RunStoreError(message)
        return _stored_run(row)

    def unfinished(self) -> list[StoredRun]:
        """Return every run that has not reached TERMINAL, oldest first."""
        with self._read() as connection:
            rows = connection.execute(
                _SELECT + " WHERE state != ? ORDER BY started_at", (RunState.TERMINAL.name,)
            ).fetchall()
        return [_stored_run(row) for row in rows]

    def _update(self, run_id: RunId, assignments: str, values: tuple[object, ...]) -> None:
        """Update one unfinished run; an unknown or finished run is an error."""
        with self._write() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {assignments}, revision = revision + 1"  # noqa: S608
                " WHERE run_id = ? AND state != ?",
                (*values, run_id.value, RunState.TERMINAL.name),
            )
            if cursor.rowcount == 1:
                return
            known = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id.value,)
            ).fetchone()
        reason = "is already finished" if known else "is unknown"
        message = f"run store: run {run_id.value} {reason}"
        raise RunStoreError(message)

    def _open(self) -> sqlite3.Connection:
        """Open a connection and verify the durability pragmas by reading them back."""
        try:
            connection = self._connect(self._path)
        except sqlite3.Error as error:
            message = f"run store: cannot open {self._path}: {error}"
            raise RunStoreError(message) from error
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA fullfsync=ON")
            connection.execute(f"PRAGMA busy_timeout={self._busy_ms}")
            self._verify_pragmas(connection)
            self._verify_busy_timeout(connection)
        except sqlite3.Error as error:
            connection.close()
            message = f"run store: cannot configure the database {self._path}: {error}"
            raise RunStoreError(message) from error
        except RunStoreError:
            connection.close()
            raise
        return connection

    def _verify_busy_timeout(self, connection: sqlite3.Connection) -> None:
        """Fail closed when the configured busy timeout did not take effect."""
        timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        if timeout != self._busy_ms:
            message = f"run store: busy_timeout is {timeout} ms, expected {self._busy_ms} ms"
            raise RunStoreError(message)

    @staticmethod
    def _verify_pragmas(connection: sqlite3.Connection) -> None:
        """Fail closed when a durability pragma did not take effect."""
        journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal != "delete":
            message = f"run store: journal_mode is {journal!r}, expected 'delete'"
            raise RunStoreError(message)
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        if synchronous != _SYNCHRONOUS_EXTRA:
            message = f"run store: synchronous is {synchronous}, expected EXTRA (3)"
            raise RunStoreError(message)
        if connection.execute("PRAGMA fullfsync").fetchone()[0] != 1:
            message = "run store: fullfsync did not turn on"
            raise RunStoreError(message)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """Run the body on a fresh connection; a database error becomes a store error."""
        with closing(self._open()) as connection:
            try:
                yield connection
            except sqlite3.Error as error:
                message = f"run store: cannot read {self._path}: {error}"
                raise RunStoreError(message) from error

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run the body in one `BEGIN IMMEDIATE` transaction on a fresh connection."""
        with closing(self._open()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                message = f"run store: cannot start a write transaction on {self._path}: {error}"
                raise RunStoreError(message) from error
            try:
                yield connection
                connection.execute("COMMIT")
            except sqlite3.Error as error:
                _rollback(connection)
                message = f"run store: write to {self._path} failed: {error}"
                raise RunStoreError(message) from error
            except BaseException:
                _rollback(connection)
                raise

    def _prepare_schema(self, connection: sqlite3.Connection) -> None:
        """Create a new store, migrate an older store, or refuse any other version."""
        connection.execute(_CREATE_META)
        row = connection.execute("SELECT version FROM schema_meta").fetchone()
        if row is None:
            connection.execute(_RUNS)
            connection.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
            return
        found = row[0]
        if found == 1:
            self._migrate_v1(connection)
        elif found == _FAULT_ADDED_IN - 1:
            connection.execute("ALTER TABLE runs ADD COLUMN fault TEXT")
            connection.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
        elif found > SCHEMA_VERSION:
            message = (
                f"run store: schema version {found} is newer than this program supports"
                f" ({SCHEMA_VERSION}); upgrade agents-dispatcher"
            )
            raise RunStoreError(message)
        elif found != SCHEMA_VERSION:
            message = f"run store: schema version {found} unsupported, expected {SCHEMA_VERSION}"
            raise RunStoreError(message)

    def _migrate_v1(self, connection: sqlite3.Connection) -> None:
        """Rebuild the table as version 2 after keeping a private copy of the version 1 file.

        The caller holds the write transaction, so the copy is the file as it was before any change.
        """
        _copy_private(self._path, self._path + BACKUP_SUFFIX)
        connection.execute("ALTER TABLE runs RENAME TO runs_v1")
        connection.execute(_RUNS)
        connection.execute(
            f"INSERT INTO runs ({_V1_COLUMNS}) SELECT {_V1_COLUMNS} FROM runs_v1"  # noqa: S608
        )
        connection.execute("DROP TABLE runs_v1")
        connection.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))


def _rollback(connection: sqlite3.Connection) -> None:
    """Roll back; a failed rollback is itself an error."""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as error:
        message = f"run store: rollback failed: {error}"
        raise RunStoreError(message) from error


def _create_private_file(path: str) -> None:
    """Create the state directory (owner only) and an empty store file (owner read/write)."""
    directory = Path(path).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _unlink_locked(descriptor: int, lock: Path, warn: Callable[[str], None]) -> None:
    """Remove `lock` if it is still the regular file that `descriptor` holds locked."""
    try:
        locked = os.fstat(descriptor)
        if not stat.S_ISREG(locked.st_mode):
            warn(f"run store: {lock} is not a regular file and is left in place")
            return
        current = os.lstat(lock)
        if (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino):
            return  # the name now belongs to another file; it is not the one we locked
        lock.unlink()
    except FileNotFoundError:
        return  # another opener removed it between our lock and our unlink
    except OSError as error:
        warn(f"run store: the old lock file {lock} could not be removed: {error}")


def _remove_unheld_m1_lock(store_path: str, warn: Callable[[str], None]) -> None:
    """Remove the global `supervisor.lock` that M1 left next to the store, unless it is held.

    M2 locks per run, so nothing reads this file any more. A lock that is still held belongs to a
    live M1 supervisor and stays: the failed non-blocking lock is the answer, not an error. The
    file is only a leftover, so every other problem with it is a warning and the store still
    opens; a symlink is never followed.
    """
    lock = Path(store_path).parent / M1_LOCK_NAME
    try:
        descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    except OSError as error:
        warn(f"run store: the old lock file {lock} is left in place (cannot open it: {error})")
        return
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        except OSError as error:
            warn(f"run store: the old lock file {lock} is left in place (cannot lock it: {error})")
            return
        _unlink_locked(descriptor, lock, warn)
    finally:
        os.close(descriptor)


def _copy_private(source: str, target: str) -> None:
    """Copy `source` to `target` with owner-only permissions, keeping an existing copy."""
    if Path(target).exists():
        return
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as out, Path(source).open("rb") as original:
        shutil.copyfileobj(original, out)
        out.flush()
        os.fsync(out.fileno())
