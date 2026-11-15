"""The confined account inspector: it runs a tool's login status command inside the confinement.

No observation ever runs unconfined (ARCH L4245). The command gets a scratch sandbox of its own,
a minimal environment and no brief; its proxy endpoint points at a port nothing serves, because a
login status command needs no network. Anything that does not run cleanly is `UNKNOWN`, never a
guessed state.
"""

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agents_dispatcher.adapters.system.private_folder import ensure_private_folder
from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.application.ports import (
    Confinement,
    EgressEndpoint,
    EgressProxy,
    IdGenerator,
    PreparedWorkspace,
    WorkerCommand,
)
from agents_dispatcher.domain.login import LoginObservation, LoginState
from agents_dispatcher.domain.routing import TaskMode
from agents_dispatcher.domain.run import Limits, RunId

PRIVATE_MODE = 0o700
UNSERVED_PORT = 9  # the discard port: nothing listens, so even the one allowed address is dead
BASE_SHA = "0" * 40


@dataclass(frozen=True)
class Captured:
    """What a confined command printed; `note` records a cleanup trouble."""

    exit_status: int | None
    stdout: str
    note: str = ""


@dataclass(frozen=True)
class NotCaptured:
    """Why a confined command produced nothing to trust."""

    note: str


class LoginProbe(Protocol):
    """One tool's login command and the reading of what it prints."""

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the command to run: the tool and its status arguments."""
        ...

    def interpret(self, exit_status: int | None, stdout: str) -> LoginState:
        """Read the outcome of the command."""
        ...


class ConfinedLoginInspector:
    """Runs login probes through a `Confinement`."""

    def __init__(  # noqa: PLR0913 - the composition root wires exactly these collaborators
        self,
        *,
        confinement: Confinement,
        ids: IdGenerator,
        scratch_root: str,
        limits: Limits,
        home: str,
        user: str,
        path: str,
    ) -> None:
        """Keep the confinement, the scratch folder, the limits and the tool's account facts."""
        self._confinement = confinement
        self._ids = ids
        self._scratch_root = Path(scratch_root)
        self._limits = limits
        self._home = home
        self._user = user
        self._path = path

    def observe(self, probe: LoginProbe) -> LoginObservation:
        """Run the probe confined and return the state it shows, or `UNKNOWN` with a note."""
        captured = self.capture(probe.argv)
        if isinstance(captured, NotCaptured):
            return LoginObservation(LoginState.UNKNOWN, captured.note)
        return LoginObservation(
            probe.interpret(captured.exit_status, captured.stdout), captured.note
        )

    def capture(
        self, argv: tuple[str, ...], proxy: EgressProxy | None = None
    ) -> "Captured | NotCaptured":
        """Run `argv` confined and return what it printed, or why nothing can be trusted.

        Without a proxy the command's only allowed address is a port nothing serves; with one,
        it reaches the hosts that proxy allows. The proxy is stopped on every path.
        """
        run_id = self._ids.new_run_id()
        try:
            ensure_private_folder(self._scratch_root, PRIVATE_MODE, LaunchError)
            sandbox = Path(tempfile.mkdtemp(prefix="inspect-", dir=self._scratch_root))
        except (LaunchError, OSError) as error:
            return NotCaptured(f"no scratch sandbox: {error}")
        try:
            result = self._run(argv, run_id, sandbox, proxy)
        finally:
            leftover = _remove(sandbox)
        if not leftover:
            return result
        if isinstance(result, NotCaptured):
            return NotCaptured("; ".join(item for item in (result.note, leftover) if item))
        return Captured(
            result.exit_status,
            result.stdout,
            "; ".join(item for item in (result.note, leftover) if item),
        )

    def _run(
        self, argv: tuple[str, ...], run_id: RunId, sandbox: Path, proxy: EgressProxy | None
    ) -> "Captured | NotCaptured":
        """Run `argv` in `sandbox` and read its outcome."""
        worktree = sandbox / "worktree"
        tmp = sandbox / "tmp"
        try:
            for folder in (worktree, tmp):
                ensure_private_folder(folder, PRIVATE_MODE, LaunchError)
        except LaunchError as error:
            return NotCaptured(str(error))
        workspace = PreparedWorkspace(
            base_commit=BASE_SHA,
            run_id=run_id,
            sandbox_dir=str(sandbox),
            worktree_dir=str(worktree),
            branch_name=run_id.branch_name,
        )
        try:
            endpoint = proxy.start(run_id) if proxy else EgressEndpoint("127.0.0.1", UNSERVED_PORT)
        except LaunchError as error:
            return NotCaptured(f"the proxy could not start: {error}")
        try:
            return self._confined(argv, workspace, endpoint, tmp, proxied=proxy is not None)
        finally:
            if proxy:
                proxy.stop()

    def _confined(
        self,
        argv: tuple[str, ...],
        workspace: PreparedWorkspace,
        endpoint: EgressEndpoint,
        tmp: Path,
        *,
        proxied: bool,
    ) -> "Captured | NotCaptured":
        """Run the command through the confinement."""
        environment = {
            "HOME": self._home,
            "USER": self._user,
            "LOGNAME": self._user,
            "PATH": self._path,
            "TMPDIR": str(tmp),
        }
        if proxied:
            address = f"http://{endpoint.host}:{endpoint.port}"
            for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                environment[name] = address
        command = WorkerCommand(
            argv=argv, environment=environment, stdin="", cwd=workspace.worktree_dir
        )
        try:
            outcome = self._confinement.run(
                command,
                workspace,
                endpoint,
                self._limits,
                lambda _identity: None,
                lambda: False,
                TaskMode.READ,
            )
        except LaunchError as error:
            return NotCaptured(f"the status command could not start: {error}")
        if outcome.limit_hit is not None or outcome.survivors:
            return NotCaptured("the status command hit a limit or left processes behind")
        return Captured(outcome.exit_status, outcome.stdout)


def _remove(sandbox: Path) -> str:
    """Remove the scratch sandbox; return a note when that failed, else an empty text."""
    try:
        shutil.rmtree(sandbox)
    except OSError as error:
        return f"inspector sandbox {sandbox} was not removed: {error}"
    return ""
