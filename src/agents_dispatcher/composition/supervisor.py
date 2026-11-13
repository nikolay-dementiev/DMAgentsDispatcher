"""Composition root of the detached supervisor: takes one accepted run to its stored end."""

import argparse
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

import agents_dispatcher
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.run_log import RunLog
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.errors import RunNotClaimableError, RunStoreError
from agents_dispatcher.application.ports import ProcessIdentity
from agents_dispatcher.application.settings import Settings, SettingsError
from agents_dispatcher.composition.wiring import (
    CONTROL_DIR,
    STORE_FILE,
    WorkerEnvironment,
    build_run_task,
    load_account_settings,
)
from agents_dispatcher.domain.run import RunId, TaskSpec
from agents_dispatcher.domain.views import bounded_text

EXIT_REFUSED = 3
EXIT_INVALID = 2
EXIT_FAILED = 1
# Used when the settings, which hold `public.summary_bytes` and the busy timeout, did not load.
FAULT_FALLBACK_BYTES = 1024
FAULT_FALLBACK_BUSY_S = 5.0


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
    """Leave the cause in the run row so the caller's report can name it; never hides a failure."""
    try:
        store = SqliteRunStore(
            f"{arguments.store_path}/{STORE_FILE}", epoch_s=time.time, busy_s=busy_s
        )
        store.record_fault(run_id, bounded_text(text, bound))
    except (RunStoreError, OSError) as error:
        sys.stderr.write(f"agentsd-supervisor: cannot record the fault: {error}\n")


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
    parts = build_run_task(settings, environment, store=store, table=table, owners=owners)
    stored = store.get(run_id)
    limits = stored.limits
    spec = TaskSpec(
        repo=stored.repo,
        task=brief,
        hard_s=limits.hard_s if limits else None,
        memory_bytes=limits.memory_bytes if limits else None,
    )
    identity = ProcessIdentity(os.getpid(), table.start_time(os.getpid()))
    try:
        report = parts.task.supervise(spec, run_id, identity)
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
