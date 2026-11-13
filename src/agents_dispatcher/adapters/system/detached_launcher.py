"""Starts the supervisor of a run as a detached process and hands it the brief on stdin.

The supervisor leads its own session, so closing the caller's terminal or killing the caller's
process group does not touch it. It receives exactly four environment variables (the account
values and a fixed `PATH`), all configuration as arguments, and the brief on standard input:
argv and the environment can be read by other accounts, a pipe cannot. The caller never waits
for it; a thread only reaps it if it ends while the caller still lives.
"""

import contextlib
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass

from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.domain.run import RunId

WRITE_CHUNK = 65536


@dataclass(frozen=True)
class DetachedLauncherConfig:
    """Everything the launcher needs; none of it is secret."""

    interpreter: str
    bootstrap: str
    leading_args: tuple[str, ...]
    account_home: str
    account_user: str
    path: str
    store_path: str
    user_settings: str
    write_timeout_s: float


class DetachedSupervisorLauncher:
    """A `SupervisorLauncher` that starts `python -I <bootstrap> ...` in a new session."""

    def __init__(self, config: DetachedLauncherConfig) -> None:
        """Keep the configuration; nothing is started until `launch`."""
        self._config = config

    def launch(self, run_id: RunId, brief: str) -> None:
        """Start the supervisor and hand it the brief; raise `LaunchError` when that fails."""
        config = self._config
        argv = [
            config.interpreter,
            "-I",
            "-B",
            config.bootstrap,
            *config.leading_args,
            "--run-id",
            run_id.value,
            "--store-path",
            config.store_path,
            "--account-home",
            config.account_home,
            "--account-user",
            config.account_user,
            "--user-settings",
            config.user_settings,
        ]
        environment = {
            "HOME": config.account_home,
            "USER": config.account_user,
            "LOGNAME": config.account_user,
            "PATH": config.path,
        }
        try:
            process = subprocess.Popen(  # noqa: S603 - argv array, interpreter from the caller
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                cwd="/",
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            message = f"cannot start the supervisor: {error}"
            raise LaunchError(message) from error
        threading.Thread(target=process.wait, daemon=True).start()
        try:
            self._hand_over(process, brief.encode("utf-8"))
        except OSError as error:
            message = f"cannot hand the brief to the supervisor: {error}"
            raise LaunchError(message) from error

    def _hand_over(self, process: "subprocess.Popen[bytes]", data: bytes) -> None:
        """Write the whole brief within the time bound, then close standard input."""
        stream = process.stdin
        if stream is None:
            message = "the supervisor has no standard input"
            raise OSError(message)
        try:
            self._write_all(stream.fileno(), data)
        except OSError:
            # Closing standard input would end the brief early, and the supervisor would run on a
            # part of the task. Kill it first; a supervisor that is already gone is fine.
            with contextlib.suppress(OSError):
                process.kill()
            raise
        finally:
            with contextlib.suppress(OSError):  # a supervisor that died closed the pipe already
                stream.close()

    def _write_all(self, descriptor: int, data: bytes) -> None:
        """Write all of the data to a pipe within the time bound; raise `OSError` otherwise."""
        os.set_blocking(descriptor, False)
        deadline = time.monotonic() + self._config.write_timeout_s
        view = memoryview(data)
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                message = "the supervisor did not read its brief in time"
                raise OSError(message)
            _, writable, _ = select.select([], [descriptor], [], min(left, 1.0))
            if writable:
                written = os.write(descriptor, view[:WRITE_CHUNK])
                view = view[written:]
