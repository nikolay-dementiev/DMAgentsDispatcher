"""The durable run record in one SQLite file (schema version 1, M1-provisional).

Durability follows the probe digest: rollback journal in DELETE mode, `synchronous=EXTRA`,
`fullfsync=ON`, every pragma read back, and every write inside `BEGIN IMMEDIATE` so a second
writer waits or fails instead of interleaving. Each operation uses its own short connection.
"""

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import FinishRecord, ProcessIdentity, RunRecord
from agents_dispatcher.domain.run import LimitKind, RunId, RunState, TerminalReason

SCHEMA_VERSION = 1
_SYNCHRONOUS_EXTRA = 3
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        base_commit TEXT NOT NULL,
        branch TEXT NOT NULL,
        state TEXT NOT NULL,
        terminal_reason TEXT,
        started_at REAL NOT NULL,
        finished_at REAL,
        pgid INTEGER,
        pg_start_time REAL,
        cli_version TEXT NOT NULL,
        limit_hit TEXT,
        result_branch TEXT,
        commits_on_branch INTEGER,
        worker_exit_status INTEGER,
        duration_s REAL
    )""",
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
            for statement in _SCHEMA:
                connection.execute(statement)
            self._check_schema_version(connection)

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
            row = connection.execute(
                "SELECT run_id, repo, base_commit, branch, state, terminal_reason, started_at,"
                " finished_at, pgid, pg_start_time, cli_version, limit_hit, result_branch,"
                " commits_on_branch, worker_exit_status, duration_s FROM runs WHERE run_id = ?",
                (run_id.value,),
            ).fetchone()
        if row is None:
            message = f"run store: run {run_id.value} is unknown"
            raise RunStoreError(message)
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

    @staticmethod
    def _check_schema_version(connection: sqlite3.Connection) -> None:
        """Insert the version into a new store, or refuse a store with another version."""
        row = connection.execute("SELECT version FROM schema_meta").fetchone()
        if row is None:
            connection.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row[0] != SCHEMA_VERSION:
            found = row[0]
            message = f"run store: schema version {found} unsupported, expected {SCHEMA_VERSION}"
            raise RunStoreError(message)


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
