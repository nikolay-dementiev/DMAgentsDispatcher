"""Runs a worker inside a Seatbelt profile, watches it, stops it and sweeps after it.

Process shape (PLAN-4 section 2): the supervisor starts the launcher shim in its own session.
The shim leads the worker's process group and ends it when the control pipe closes, so a
supervisor that dies cannot leave a worker behind. Time and memory are watched here, over the
whole tree of processes that belong to the run (O1 layer 3). After the group is gone, the
cleanup rounds find what is left by group, ancestry, marker and folder, stop it and report it.
"""

import contextlib
import json
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from agents_dispatcher.adapters.system import launcher_shim
from agents_dispatcher.adapters.system.disk_watch import DiskReading, DiskWatch
from agents_dispatcher.adapters.system.protection import never_signalled
from agents_dispatcher.adapters.system.seatbelt_profile import (
    ProfileInputs,
    ProviderProfile,
    render_profile,
)
from agents_dispatcher.application.containment import ContainmentCleanup, RunObserver
from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.application.ports import (
    Clock,
    DiskWalk,
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
    ProcessCensus,
    ProcessIdentity,
    Signaller,
    Sleeper,
    WorkerCommand,
)
from agents_dispatcher.application.settings import ObservationSettings, WalkSettings
from agents_dispatcher.domain.attribution import RunFacts
from agents_dispatcher.domain.process import Absence, ProcessBasics, ProcessKey
from agents_dispatcher.domain.routing import TaskMode
from agents_dispatcher.domain.run import LimitKind, Limits

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MARKER_ENV = "AGENTSD_RUN_ID"
PRIVATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SHIM_EXIT_MARGIN_S = 2.0
CAPTURED_LOGS = ("stdout.txt", "stderr.txt")
EXIT_PREFIX = "EXIT "
FAILED_PREFIX = "LAUNCH_FAILED "


class ProcessTable(Protocol):
    """The reads of the process table the confinement needs."""

    def group_members(self, pgid: int) -> list[int]:
        """Return the pids of a process group."""
        ...

    def basics(self, pid: int) -> ProcessBasics | Absence:
        """Return parent, group, start time and descriptors, or why they are unreadable."""
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
    provider: ProviderProfile
    observation: ObservationSettings
    file_bytes: int
    descriptors: int
    aggregate_descriptors: int
    output_bytes: int
    sandbox_bytes: int
    sandbox_inodes: int
    walk: WalkSettings


@dataclass(frozen=True)
class _Meters:
    """What the watch reads on every scan."""

    observer: RunObserver
    disk: DiskWatch
    logs: Path


@dataclass(frozen=True)
class _WatchEnd:
    """How the watch ended: the worker's status, a measured breach, a lost view or a stop."""

    exit_status: int | None = None
    limit_hit: LimitKind | None = None
    stopped: bool = False
    observation_lost: str | None = None


def _output_size(logs: Path) -> int:
    """Return the bytes the worker has written to its captured streams so far, both together."""
    total = 0
    for name in CAPTURED_LOGS:
        try:
            total += (logs / name).stat().st_size
        except FileNotFoundError:
            continue  # not created yet: nothing written
    return total


class SeatbeltConfinement:
    """A `Confinement` over `sandbox-exec`, the launcher shim and the process table."""

    def __init__(  # noqa: PLR0913 - the composition root wires exactly these collaborators
        self,
        config: ConfinementConfig,
        *,
        table: ProcessTable,
        clock: Clock,
        census: ProcessCensus,
        signaller: Signaller,
        sleeper: Sleeper,
        disk_walk: DiskWalk,
        protected: Callable[[str], frozenset[ProcessKey]],
    ) -> None:
        """Keep the settings and the collaborators.

        `protected` returns, for a run marker, the identities of the other live runs' processes
        (supervisor and shim leader): they are never attributed, whatever they hold open.
        """
        self._config = config
        self._table = table
        self._clock = clock
        self._census = census
        self._signaller = signaller
        self._sleeper = sleeper
        self._disk_walk = disk_walk
        self._protected = protected

    def run(  # noqa: PLR0913, PLR0917 - the port signature
        self,
        command: WorkerCommand,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        limits: Limits,
        on_started: Callable[[ProcessIdentity], None],
        should_stop: Callable[[], bool],
        mode: TaskMode,
    ) -> ExecutionOutcome:
        """Run, watch, stop and sweep; raise `LaunchError` when the worker never started."""
        marker = workspace.run_id.value
        self._require_executable(command)
        logs = self._prepare_logs(workspace, command)
        before_start = time.time()
        shim, control = self._start_shim(command, workspace, egress, logs, marker, mode)
        facts = self._facts(shim.pid, before_start, marker, workspace)
        observer = RunObserver(self._census, self._clock, self._config.observation, facts)
        disk = DiskWatch(self._disk_walk, self._clock, self._config.walk, facts.roots)
        disk.start()
        try:
            on_started(self._shim_identity(shim.pid, logs))
            end = self._watch(shim, limits, _Meters(observer, disk, logs), should_stop)
        finally:
            disk.stop()
            self._stop(shim, control)
        cleanup = ContainmentCleanup(
            self._census, self._signaller, self._sleeper, self._config.observation
        )
        result = cleanup.run(replace(facts, remembered=observer.remembered))
        survivors = tuple(
            f"pid {pid}"
            for pid in sorted(
                {key.pid for key in result.killed} | {item.key.pid for item in result.survivors}
            )
        )
        return ExecutionOutcome(
            exit_status=end.exit_status,
            stdout=(logs / "stdout.txt").read_text(encoding="utf-8", errors="replace"),
            survivors=survivors,
            unresolved=result.unresolved(),
            limit_hit=end.limit_hit,
            stopped=end.stopped,
            observation_lost=end.observation_lost,
            unreadable=tuple(f"pid {key.pid}" for key in result.unreadable),
        )

    def _shim_identity(self, shim_pid: int, logs: Path) -> ProcessIdentity:
        """Name the shim's group and start time; an unreadable start time is None and noted."""
        reading = self._table.basics(shim_pid)
        if isinstance(reading, ProcessBasics):
            return ProcessIdentity(shim_pid, reading.start_time)
        note = (
            f"the start time of the worker shim {shim_pid} could not be read ({reading.name}); "
            "its row will not identify a live group\n"
        )
        with (logs / "supervisor.txt").open("a", encoding="utf-8") as handle:
            handle.write(note)
        return ProcessIdentity(shim_pid, None)

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

    def _start_shim(  # noqa: PLR0913, PLR0917 - every input is a distinct fact of the run
        self,
        command: WorkerCommand,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        logs: Path,
        marker: str,
        mode: TaskMode,
    ) -> tuple["subprocess.Popen[str]", int]:
        """Start the shim in its own session and hand it the specification."""
        sandbox = str(Path(workspace.sandbox_dir).resolve())
        denied = tuple(str(Path(path).resolve()) for path in self._config.denied_reads)
        profile = render_profile(
            ProfileInputs(
                sandbox,
                egress.port,
                denied,
                mode,
                str(Path(workspace.worktree_dir).resolve()),
                self._config.provider,
            )
        )
        spec = {
            "argv": [SANDBOX_EXEC, "-p", profile, *command.argv],
            "env": {**command.environment, MARKER_ENV: marker},
            "cwd": command.cwd,
            "stdin_path": str(logs / "brief.txt"),
            "stdout_path": str(logs / "stdout.txt"),
            "stderr_path": str(logs / "stderr.txt"),
            "term_grace_s": self._config.term_grace_s,
            "file_bytes": self._config.file_bytes,
            "descriptors": self._config.descriptors,
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
        self,
        shim: "subprocess.Popen[str]",
        limits: Limits,
        meters: "_Meters",
        should_stop: Callable[[], bool],
    ) -> "_WatchEnd":
        """Wait for the worker to end, a limit to trip or the run to become unobservable."""
        started = self._clock.now()
        stream = shim.stdout
        if stream is None:
            message = "the launcher shim has no standard output"
            raise LaunchError(message)
        while True:
            readable, _, _ = select.select([stream], [], [], self._config.scan_s)
            if readable:
                line = stream.readline().strip()
                if line.startswith(FAILED_PREFIX):
                    raise LaunchError(line.removeprefix(FAILED_PREFIX))
                if line.startswith(EXIT_PREFIX):
                    return _WatchEnd(exit_status=int(line.removeprefix(EXIT_PREFIX)))
                if not line:
                    message = "the launcher shim ended without reporting"
                    raise LaunchError(message)
            breach = self._breach(started, limits, meters)
            if breach is not None:
                return breach
            if should_stop():
                return _WatchEnd(stopped=True)

    def _breach(self, started: float, limits: Limits, meters: "_Meters") -> "_WatchEnd | None":
        """Return how the run must end now because of a limit or a lost view, or None."""
        disk = meters.disk.reading()
        limit = self._limit_hit(started, limits, meters, disk)
        if limit is not None:
            return _WatchEnd(limit_hit=limit)
        lost = meters.observer.lost() or disk.lost
        return None if lost is None else _WatchEnd(observation_lost=lost)

    def _limit_hit(
        self, started: float, limits: Limits, meters: "_Meters", disk: DiskReading
    ) -> LimitKind | None:
        """Return the first measured limit the run has passed, or None."""
        if self._clock.now() - started > limits.hard_s:
            return LimitKind.TIME
        if meters.observer.memory() > limits.memory_bytes:
            return LimitKind.MEMORY
        if meters.observer.descriptors() > self._config.aggregate_descriptors:
            return LimitKind.DESCRIPTORS
        if _output_size(meters.logs) > self._config.output_bytes:
            return LimitKind.OUTPUT
        return self._disk_limit(disk)

    def _disk_limit(self, disk: DiskReading) -> LimitKind | None:
        """Return the disk limit the latest walk shows as passed, or None."""
        if disk.usage is None:
            return None
        if disk.usage.allocated_bytes > self._config.sandbox_bytes:
            return LimitKind.SANDBOX_BYTES
        if disk.usage.inodes > self._config.sandbox_inodes:
            return LimitKind.SANDBOX_INODES
        return None

    def _facts(
        self, shim_pid: int, before_start: float, marker: str, workspace: PreparedWorkspace
    ) -> RunFacts:
        """Collect what is known about the run before the first look at the table."""
        basics = self._table.basics(shim_pid)
        # A shim that is already unreadable still started after `before_start`.
        start = basics.start_time if isinstance(basics, ProcessBasics) else before_start
        roots = tuple(
            str(Path(path).resolve()) for path in (workspace.sandbox_dir, workspace.worktree_dir)
        )
        return RunFacts(
            marker=marker,
            shim=ProcessKey(shim_pid, start),
            group=shim_pid,
            roots=roots,
            remembered=frozenset(),
            protected_pids=never_signalled(self._table),
            protected_keys=self._protected(marker),
            path_until=None,
        )

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
