"""Composition root of the detached supervisor: takes one accepted run to its stored end."""

import argparse
import os
import signal
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

import agents_dispatcher
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.run_log import RunLog
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.signal_interrupts import restore_handler
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.errors import RunNotClaimableError
from agents_dispatcher.application.ports import ProcessIdentity, StopPoll
from agents_dispatcher.application.settings import Settings, SettingsError
from agents_dispatcher.composition.wiring import (
    CONTROL_DIR,
    STORE_FILE,
    WorkerEnvironment,
    build_run_task,
    load_account_settings,
)
from agents_dispatcher.domain.routing import RunRouting
from agents_dispatcher.domain.run import RunId, TaskSpec
from agents_dispatcher.domain.views import bounded_text

EXIT_REFUSED = 3
EXIT_INVALID = 2
EXIT_FAILED = 1
# Used when the settings, which hold `public.summary_bytes` and the busy timeout, did not load.
FAULT_FALLBACK_BYTES = 1024
FAULT_FALLBACK_BUSY_S = 5.0


HANGUP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class SignalStops:
    """A stop poll that also answers yes once the supervisor itself was told to end.

    A SIGTERM or SIGHUP is read as a request to stop the run: the worker group is stopped, the
    report is sealed and the run ends STOPPED, instead of the run being left RUNNING.
    """

    def __init__(self, store: StopPoll) -> None:
        """Wrap the store's stop poll; no signal has arrived yet."""
        self._store = store
        self._received: str | None = None

    def received(self, number: int) -> None:
        """Record the first signal that asks the supervisor to end."""
        if self._received is None:
            self._received = signal.Signals(number).name

    def stop_requested(self, run_id: RunId) -> bool:
        """Return whether a signal arrived or the store holds a stop request."""
        return self._received is not None or self._store.stop_requested(run_id)

    def stop_reason(self, run_id: RunId) -> str | None:
        """Return the signal that ended the run, else the reason of the stored request."""
        if self._received is not None:
            return f"supervisor received {self._received}"
        return self._store.stop_reason(run_id)

    @contextmanager
    def listening(self) -> Iterator[None]:
        """Route SIGTERM and SIGHUP here for the length of the block; restore them after."""
        previous = {
            number: signal.signal(number, lambda n, _frame: self.received(n))
            for number in HANGUP_SIGNALS
        }
        try:
            yield
        finally:
            for number, handler in previous.items():
                restore_handler(number, handler)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the arguments the launcher passes; none of them is secret."""
    parser = argparse.ArgumentParser(prog="agentsd-supervisor", add_help=False)
    for name in ("run-id", "store-path", "account-home", "account-user", "user-settings"):
        parser.add_argument(f"--{name}", required=True)
    return parser.parse_args(argv)


def run_supervisor(argv: Sequence[str], stdin: TextIO) -> int:
    """Read the brief, run the accepted run to its end, write the log; return an exit code."""
    try:
        arguments = _parse(argv)
        run_id = RunId(arguments.run_id)
    except (SystemExit, ValueError) as error:
        sys.stderr.write(f"agentsd-supervisor: invalid arguments: {error}\n")
        return EXIT_INVALID
    brief = stdin.read()  # first, so the launcher is never kept waiting on the pipe
    try:
        settings = load_account_settings(arguments.account_home, arguments.user_settings or None)
    except SettingsError as error:
        sys.stderr.write(f"agentsd-supervisor: {error}\n")
        _record_fault(arguments, run_id, str(error), FAULT_FALLBACK_BYTES, FAULT_FALLBACK_BUSY_S)
        return EXIT_FAILED
    if settings.store_path != arguments.store_path.rstrip("/"):
        # The row lives where the launcher says; looking for it elsewhere would find nothing.
        message = (
            f"store path mismatch: the launcher passed {arguments.store_path} "
            f"but the settings load {settings.store_path}"
        )
        sys.stderr.write(f"agentsd-supervisor: {message}\n")
        _record_fault(arguments, run_id, message, FAULT_FALLBACK_BYTES, FAULT_FALLBACK_BUSY_S)
        return EXIT_FAILED
    log = RunLog(settings.logging_directory, run_id.value)
    try:
        return _supervise(arguments, run_id, brief, settings, log)
    except BaseException as error:  # noqa: BLE001 - the last boundary: record it, then exit
        text = f"{type(error).__name__}: {error}"
        log.write(f"supervisor failed: {text}")
        _record_fault(
            arguments, run_id, text, settings.public_summary_bytes, settings.sqlite_busy_s
        )
        return EXIT_FAILED
    finally:
        log.close()


def _record_fault(
    arguments: argparse.Namespace, run_id: RunId, text: str, bound: int, busy_s: float
) -> None:
    """Leave the cause in the run row so the caller's report can name it; never hides a failure.

    The row is in the store the launcher named, because that is where `start` made it. Whatever
    goes wrong while writing is reported on stderr and, when the row is writable after all,
    attached to the fault text; it never replaces the failure that is being recorded.
    """
    try:
        _write_fault(arguments, run_id, text, bound, busy_s)
    except Exception as error:  # noqa: BLE001 - the original failure must still reach the caller
        sys.stderr.write(f"agentsd-supervisor: cannot record the fault: {error}\n")
        try:
            attached = f"{text} (recording this fault also failed: {error})"
            _write_fault(arguments, run_id, attached, bound, busy_s)
        except Exception as second:  # noqa: BLE001 - nothing more can be done; say so
            sys.stderr.write(f"agentsd-supervisor: the fault could not be attached: {second}\n")


def _write_fault(
    arguments: argparse.Namespace, run_id: RunId, text: str, bound: int, busy_s: float
) -> None:
    """Write one fault text into the row, in the store the launcher named."""
    store = SqliteRunStore(f"{arguments.store_path}/{STORE_FILE}", epoch_s=time.time, busy_s=busy_s)
    store.record_fault(run_id, bounded_text(text, bound))


def _log_route(log: RunLog, routing: RunRouting) -> None:
    """Write the decision the run was accepted with: what runs, then every evaluated link."""
    log.write(
        f"route: task_type={routing.task_type or '-'} mode={routing.mode.value} "
        f"provider={routing.provider} model={routing.model} effort={routing.effort}"
    )
    for item in routing.route:
        codes = ",".join(problem.code.value for problem in item.problems)
        log.write(
            f"route: {item.provider} {item.disposition.value}" + (f"({codes})" if codes else "")
        )


def _supervise(
    arguments: argparse.Namespace,
    run_id: RunId,
    brief: str,
    settings: Settings,
    log: RunLog,
) -> int:
    """Wire the use case, run it and log what the caller cannot see."""
    package_root = Path(agents_dispatcher.__file__).resolve().parent.parent
    log.write(f"supervisor pid {os.getpid()} python {sys.version.split()[0]}")
    log.write(f"package: {package_root}")
    table = LibprocProcessTable()
    store = SqliteRunStore(
        f"{settings.store_path}/{STORE_FILE}", epoch_s=time.time, busy_s=settings.sqlite_busy_s
    )
    owners = FileRunOwners(f"{settings.store_path}/{CONTROL_DIR}")
    environment = WorkerEnvironment(
        arguments.account_home, arguments.account_user, sys.executable, os.environ.get("PATH", "")
    )
    stops = SignalStops(store)
    parts = build_run_task(
        settings, environment, store=store, table=table, owners=owners, stops=stops
    )
    stored = store.get(run_id)
    _log_route(log, stored.routing)
    limits = stored.limits
    spec = TaskSpec(
        repo=stored.repo,
        task=brief,
        hard_s=limits.hard_s if limits else None,
        memory_bytes=limits.memory_bytes if limits else None,
    )
    identity = ProcessIdentity(os.getpid(), table.start_time(os.getpid()))
    try:
        with stops.listening():
            report = parts.task.supervise(spec, run_id, identity, stored.routing)
    except RunNotClaimableError as error:
        log.write(str(error))
        return EXIT_REFUSED
    finally:
        decisions = parts.proxy.decisions()
        log.write(f"proxy decisions: {len(decisions)}")
        for decision in decisions:
            verdict = "allow" if decision.allowed else "refuse"
            log.write(f"proxy {verdict} {decision.host}:{decision.port} {decision.reason}")
        for fault in parts.proxy.faults():
            log.write(f"proxy fault: {fault}")
    reason = report.terminal_reason.name if report.terminal_reason else "none"
    log.write(f"run ended: {reason}")
    return 0
