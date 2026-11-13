"""Runs a worker inside a Seatbelt profile, watches it, stops it and sweeps after it.

Process shape (PLAN-4 section 2): the supervisor starts the launcher shim in its own session.
The shim leads the worker's process group and ends it when the control pipe closes, so a
supervisor that dies cannot leave a worker behind. Time and memory are watched here. After the
group is gone, processes that still carry the run marker in their environment are found,
stopped and reported.
"""

import contextlib
import json
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agents_dispatcher.adapters.system import launcher_shim
from agents_dispatcher.adapters.system.seatbelt_profile import ProfileInputs, render_profile
from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.application.ports import (
    Clock,
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
    ProcessIdentity,
    WorkerCommand,
)
from agents_dispatcher.domain.run import LimitKind, Limits

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MARKER_ENV = "AGENTSD_RUN_ID"
PRIVATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SHIM_EXIT_MARGIN_S = 2.0
SWEEP_POLL_S = 0.05
EXIT_PREFIX = "EXIT "
FAILED_PREFIX = "LAUNCH_FAILED "


class ProcessTable(Protocol):
    """The reads of the process table the confinement needs."""

    def all_pids(self) -> list[int]:
        """Return every visible pid."""
        ...

    def group_members(self, pgid: int) -> list[int]:
        """Return the pids of a process group."""
        ...

    def start_time(self, pid: int) -> float | None:
        """Return the wall-clock start time, or None when gone."""
        ...

    def footprint(self, pid: int) -> int | None:
        """Return the physical footprint in bytes, or None when gone."""
        ...

    def environment(self, pid: int) -> dict[str, str] | None:
        """Return the environment at exec, or None when unreadable."""
        ...


@dataclass(frozen=True)
class ConfinementConfig:
    """Settings the confinement consumes; every value comes from the settings file."""

    denied_reads: tuple[str, ...]
    term_grace_s: float
    kill_wait_s: float
    scan_s: float
    interpreter: str


class SeatbeltConfinement:
    """A `Confinement` over `sandbox-exec`, the launcher shim and the process table."""

    def __init__(self, config: ConfinementConfig, *, table: ProcessTable, clock: Clock) -> None:
        """Keep the settings, the process table and the clock."""
        self._config = config
        self._table = table
        self._clock = clock

    def run(
        self,
        command: WorkerCommand,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        limits: Limits,
        on_started: Callable[[ProcessIdentity], None],
    ) -> ExecutionOutcome:
        """Run, watch, stop and sweep; raise `LaunchError` when the worker never started."""
        marker = workspace.run_id.value
        self._require_executable(command)
        logs = self._prepare_logs(workspace, command)
        shim, control = self._start_shim(command, workspace, egress, logs, marker)
        try:
            on_started(ProcessIdentity(shim.pid, self._table.start_time(shim.pid)))
            exit_status, limit_hit, stopped = self._watch(shim, limits, marker)
        finally:
            self._stop(shim, control)
        survivors, alive = self._sweep(marker)
        return ExecutionOutcome(
            exit_status=exit_status,
            stdout=(logs / "stdout.txt").read_text(encoding="utf-8", errors="replace"),
            survivors=survivors,
            survivors_alive=alive,
            limit_hit=limit_hit,
            stopped=stopped,
        )

    @staticmethod
    def _require_executable(command: WorkerCommand) -> None:
        """Refuse a worker that cannot be executed: sandbox-exec would only report a status."""
        executable = Path(command.argv[0]) if command.argv else None
        if executable is None or not executable.is_file() or not os.access(executable, os.X_OK):
            message = f"worker executable {command.argv[:1]} is missing or not executable"
            raise LaunchError(message)

    @staticmethod
    def _prepare_logs(workspace: PreparedWorkspace, command: WorkerCommand) -> Path:
        """Create the private log folder and write the brief that becomes standard input."""
        logs = Path(workspace.sandbox_dir).resolve() / "logs"
        logs.mkdir(mode=PRIVATE_MODE, exist_ok=True)
        brief = logs / "brief.txt"
        brief.touch(mode=PRIVATE_FILE_MODE)
        brief.write_text(command.stdin, encoding="utf-8")
        return logs

    def _start_shim(
        self,
        command: WorkerCommand,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        logs: Path,
        marker: str,
    ) -> tuple["subprocess.Popen[str]", int]:
        """Start the shim in its own session and hand it the specification."""
        sandbox = str(Path(workspace.sandbox_dir).resolve())
        denied = tuple(str(Path(path).resolve()) for path in self._config.denied_reads)
        profile = render_profile(ProfileInputs(sandbox, egress.port, denied))
        spec = {
            "argv": [SANDBOX_EXEC, "-p", profile, *command.argv],
            "env": {**command.environment, MARKER_ENV: marker},
            "cwd": command.cwd,
            "stdin_path": str(logs / "brief.txt"),
            "stdout_path": str(logs / "stdout.txt"),
            "stderr_path": str(logs / "stderr.txt"),
            "term_grace_s": self._config.term_grace_s,
        }
        read_end, write_end = os.pipe()
        try:
            shim = subprocess.Popen(  # noqa: S603 - argv array, interpreter from settings, no shell
                [self._config.interpreter, "-I", "-S", "-B", launcher_shim.__file__, str(read_end)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                pass_fds=(read_end,),
                start_new_session=True,
            )
        except OSError as error:
            os.close(write_end)
            message = f"cannot start the launcher shim: {error}"
            raise LaunchError(message) from error
        finally:
            os.close(read_end)
        try:
            if shim.stdin is None:
                message = "the launcher shim has no standard input"
                raise LaunchError(message)  # noqa: TRY301 - handled by the clause below
            shim.stdin.write(json.dumps(spec))
            shim.stdin.close()
        except (OSError, LaunchError) as error:
            self._stop(shim, write_end)
            message = f"cannot hand the specification to the launcher shim: {error}"
            raise LaunchError(message) from error
        return shim, write_end

    def _watch(
        self, shim: "subprocess.Popen[str]", limits: Limits, marker: str
    ) -> tuple[int | None, LimitKind | None, bool]:
        """Wait for the worker to end or a limit to trip; return status, limit and stop flag."""
        started = self._clock.now()
        stream = shim.stdout
        if stream is None:
            message = "the launcher shim has no standard output"
            raise LaunchError(message)
        try:
            while True:
                readable, _, _ = select.select([stream], [], [], self._config.scan_s)
                if readable:
                    line = stream.readline().strip()
                    if line.startswith(FAILED_PREFIX):
                        raise LaunchError(line.removeprefix(FAILED_PREFIX))
                    if line.startswith(EXIT_PREFIX):
                        return int(line.removeprefix(EXIT_PREFIX)), None, False
                    if not line:
                        message = "the launcher shim ended without reporting"
                        raise LaunchError(message)
                if self._clock.now() - started > limits.hard_s:
                    return None, LimitKind.TIME, False
                if self._footprint(shim.pid, marker) > limits.memory_bytes:
                    return None, LimitKind.MEMORY, False
        except KeyboardInterrupt:
            return None, None, True

    def _footprint(self, pgid: int, marker: str) -> int:
        """Sum the footprint of the group and of every process that carries the marker (F4)."""
        pids = set(self._table.group_members(pgid)) | set(self._marked(marker))
        return sum(self._table.footprint(pid) or 0 for pid in pids)

    def _marked(self, marker: str) -> list[int]:
        """Return the pids whose environment carries this run's marker."""
        own = os.getpid()
        found = []
        for pid in self._table.all_pids():
            if pid == own:
                continue
            environment = self._table.environment(pid)
            if environment is not None and environment.get(MARKER_ENV) == marker:
                found.append(pid)
        return found

    def _stop(self, shim: "subprocess.Popen[str]", control: int) -> None:
        """Close the control pipe so the shim ends the group; kill the group if it hangs."""
        with contextlib.suppress(OSError):  # already closed on an earlier path
            os.close(control)
        patience = self._config.term_grace_s + self._config.kill_wait_s + SHIM_EXIT_MARGIN_S
        try:
            shim.wait(timeout=patience)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):  # the group may have just ended
                os.killpg(shim.pid, signal.SIGKILL)
            shim.wait(timeout=self._config.kill_wait_s + SHIM_EXIT_MARGIN_S)
        if shim.stdout is not None:
            shim.stdout.close()

    def _sweep(self, marker: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Find, kill and report processes that left the group but kept the marker."""
        found = self._marked(marker)
        unsignalable: set[int] = set()
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # it ended meanwhile
                continue
            except PermissionError:
                # Not ours to signal: it stays alive and is reported as a survivor, and the
                # sweep carries on with the others instead of aborting the run.
                unsignalable.add(pid)
        deadline = time.monotonic() + self._config.kill_wait_s
        remaining = list(found)
        while remaining and time.monotonic() < deadline:
            time.sleep(SWEEP_POLL_S)
            remaining = [
                pid
                for pid in remaining
                if pid in unsignalable or self._table.start_time(pid) is not None
            ]
        return (
            tuple(f"pid {pid}" for pid in found),
            tuple(f"pid {pid}" for pid in remaining),
        )
