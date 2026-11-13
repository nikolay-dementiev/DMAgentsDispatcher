"""The durable run record in one SQLite file (schema version 2, M2-provisional).

Durability follows the probe digest: rollback journal in DELETE mode, `synchronous=EXTRA`,
`fullfsync=ON`, every pragma read back, and every write inside `BEGIN IMMEDIATE` so a second
writer waits or fails instead of interleaving. Each operation uses its own short connection.
"""

import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import FinishRecord, ProcessIdentity, RunRecord
from agents_dispatcher.domain.run import LimitKind, RunId, RunState, TerminalReason

SCHEMA_VERSION = 2
BACKUP_SUFFIX = ".v1.bak"
_SYNCHRONOUS_EXTRA = 3
_CREATE_META = "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)"
_RUNS_V2 = """CREATE TABLE runs (
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
        revision INTEGER NOT NULL DEFAULT 0
    )"""
_V1_COLUMNS = (
    "run_id, repo, base_commit, branch, state, terminal_reason, started_at, finished_at,"
    " pgid, pg_start_time, cli_version, limit_hit, result_branch, commits_on_branch,"
    " worker_exit_status, duration_s"
)


@dataclass(frozen=True)
class StoredRun:
    """A run as read back from the store."""

    run_id: RunId
    repo: str
    base_commit: str
    branch: str
    state: RunState
    terminal_reason: TerminalReason | None
    started_at: float
    finished_at: float | None
    pgid: int | None
    pg_start_time: float | None
    cli_version: str
    limit_hit: LimitKind | None
    result_branch: str | None
    commits_on_branch: int | None
    worker_exit_status: int | None
    duration_s: float | None


_SELECT = (
    "SELECT run_id, repo, base_commit, branch, state, terminal_reason, started_at,"
    " finished_at, pgid, pg_start_time, cli_version, limit_hit, result_branch,"
    " commits_on_branch, worker_exit_status, duration_s FROM runs"
)


def _stored_run(row: tuple[Any, ...]) -> StoredRun:
    """Build a `StoredRun` from a row of `_SELECT`."""
    return StoredRun(
        run_id=RunId(row[0]),
        repo=row[1],
        base_commit=row[2],
        branch=row[3],
        state=RunState[row[4]],
        terminal_reason=TerminalReason[row[5]] if row[5] else None,
        started_at=row[6],
        finished_at=row[7],
        pgid=row[8],
        pg_start_time=row[9],
        cli_version=row[10],
        limit_hit=LimitKind[row[11]] if row[11] else None,
        result_branch=row[12],
        commits_on_branch=row[13],
        worker_exit_status=row[14],
        duration_s=row[15],
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
    ) -> None:
        """Create the private file and the schema, or verify an existing store.

        `busy_s` is the `storage.sqlite_busy_s` setting; there is no default here.
        """
        self._path = path
        self._epoch_s = epoch_s
        self._busy_ms = round(busy_s * 1000)
        self._connect = connect
        _create_private_file(path)
        with self._write() as connection:
            self._prepare_schema(connection)

    def begin(self, record: RunRecord) -> None:
        """Store a new run in state ACCEPTED."""
        with self._write() as connection:
            try:
                connection.execute(
                    "INSERT INTO runs (run_id, repo, base_commit, branch, state, started_at,"
                    " cli_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.run_id.value,
                        record.repo,
                        record.base_commit,
                        record.branch,
                        RunState.ACCEPTED.name,
                        self._epoch_s(),
                        record.cli_version,
                    ),
                )
            except sqlite3.IntegrityError as error:
                message = f"run store: run {record.run_id.value} is already stored"
                raise RunStoreError(message) from error

    def set_state(self, run_id: RunId, state: RunState) -> None:
        """Store a state change; TERMINAL is reached only through `finish`."""
        if state is RunState.TERMINAL:
            message = "run store: TERMINAL is stored by finish, not by set_state"
            raise RunStoreError(message)
        self._update(run_id, "state = ?", (state.name,))

    def set_process(self, run_id: RunId, identity: ProcessIdentity) -> None:
        """Store the worker group identity."""
        self._update(run_id, "pgid = ?, pg_start_time = ?", (identity.pgid, identity.start_time_s))

    def finish(self, run_id: RunId, record: FinishRecord) -> None:
        """Store the terminal outcome and the state TERMINAL."""
        self._update(
            run_id,
            "state = ?, terminal_reason = ?, result_branch = ?, commits_on_branch = ?,"
            " limit_hit = ?, worker_exit_status = ?, duration_s = ?, finished_at = ?",
            (
                RunState.TERMINAL.name,
                record.terminal_reason.name,
                record.branch,
                record.commits_on_branch,
                record.limit_hit.name if record.limit_hit else None,
                record.worker_exit_status,
                record.duration_s,
                self._epoch_s(),
            ),
        )

    def get(self, run_id: RunId) -> StoredRun:
        """Return the stored run, or raise when it is unknown."""
        with closing(self._open()) as connection:
            row = connection.execute(_SELECT + " WHERE run_id = ?", (run_id.value,)).fetchone()
        if row is None:
            message = f"run store: run {run_id.value} is unknown"
            raise RunStoreError(message)
        return _stored_run(row)

    def unfinished(self) -> list[StoredRun]:
        """Return every run that has not reached TERMINAL, oldest first."""
        with closing(self._open()) as connection:
            rows = connection.execute(
                _SELECT + " WHERE state != ? ORDER BY started_at", (RunState.TERMINAL.name,)
            ).fetchall()
        return [_stored_run(row) for row in rows]

    def _update(self, run_id: RunId, assignments: str, values: tuple[object, ...]) -> None:
        """Update one unfinished run; an unknown or finished run is an error."""
        with self._write() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ? AND state != ?",  # noqa: S608
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
        connection = self._connect(self._path)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA fullfsync=ON")
            connection.execute(f"PRAGMA busy_timeout={self._busy_ms}")
            self._verify_pragmas(connection)
            self._verify_busy_timeout(connection)
        except sqlite3.Error as error:
            connection.close()
            message = f"run store: cannot configure the database: {error}"
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
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run the body in one `BEGIN IMMEDIATE` transaction on a fresh connection."""
        with closing(self._open()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                message = f"run store: cannot start a write transaction: {error}"
                raise RunStoreError(message) from error
            try:
                yield connection
                connection.execute("COMMIT")
            except sqlite3.Error as error:
                _rollback(connection)
                message = f"run store: write failed: {error}"
                raise RunStoreError(message) from error
            except BaseException:
                _rollback(connection)
                raise

    def _prepare_schema(self, connection: sqlite3.Connection) -> None:
        """Create a new store, migrate a version 1 store, or refuse any other version."""
        connection.execute(_CREATE_META)
        row = connection.execute("SELECT version FROM schema_meta").fetchone()
        if row is None:
            connection.execute(_RUNS_V2)
            connection.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
            return
        found = row[0]
        if found == 1:
            self._migrate_v1(connection)
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
        connection.execute(_RUNS_V2)
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


def _copy_private(source: str, target: str) -> None:
    """Copy `source` to `target` with owner-only permissions, keeping an existing copy."""
    if Path(target).exists():
        return
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as out, Path(source).open("rb") as original:
        shutil.copyfileobj(original, out)
        out.flush()
        os.fsync(out.fileno())
