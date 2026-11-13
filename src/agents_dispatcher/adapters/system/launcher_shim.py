"""The launcher shim: leader of a worker's process group and its dead-parent guard.

This file is deliberately self-contained. It is started as `python -I -S -B <this file> <fd>`,
with `-S` so that no `site` or `.pth` code of the user's environment runs, and therefore it
imports nothing from this package and only standard-library modules that need no `site` setup.
The supervisor starts it with `start_new_session=True`, which makes the shim the leader of a new
process group; the supervisor itself is never in that group.

Protocol:
  - standard input: one JSON object, the launch specification (`argv`, `env`, `cwd`,
    `stdin_path`, `stdout_path`, `stderr_path`, `term_grace_s`);
  - the control pipe (the descriptor given as the only argument): the supervisor holds the write
    end. End-of-file on it means "stop the group", whether the supervisor closed the pipe on
    purpose or died;
  - standard output: `EXIT <status>` when the worker ends (a negative status is a signal), or
    `LAUNCH_FAILED <reason>` when it could not be started.
"""

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

POLL_S = 0.1
READ_SIZE = 4096
PS = "/bin/ps"
PS_FIELDS = 2


def main(argv: list[str]) -> int:
    """Run the worker, report its end, and stop its group when the control pipe closes."""
    control_fd = int(argv[1])
    spec = json.load(sys.stdin)
    # A handler (not SIG_IGN): ignored signals would stay ignored in the worker after exec.
    signal.signal(signal.SIGTERM, _ignore)
    signal.signal(signal.SIGINT, _ignore)
    try:
        worker = _launch(spec)
    except OSError as error:
        _report(f"LAUNCH_FAILED {error}")
        return 1
    reported = False
    while True:
        readable, _, _ = select.select([control_fd], [], [], POLL_S)
        if readable and os.read(control_fd, READ_SIZE) == b"":
            return _stop_group(worker, float(spec["term_grace_s"]))
        if not reported and worker.poll() is not None:
            _report(f"EXIT {worker.returncode}")
            reported = True


def _ignore(_signal_number: int, _frame: object) -> None:
    """Survive a signal sent to the whole group; the shim ends the group itself."""


def _launch(spec: dict[str, object]) -> subprocess.Popen[bytes]:
    """Start the worker in this process group, with its own standard streams and no extra fds."""
    argv = [str(item) for item in spec["argv"]]  # type: ignore[attr-defined]
    env = {str(key): str(value) for key, value in spec["env"].items()}  # type: ignore[attr-defined]
    with (
        Path(str(spec["stdin_path"])).open("rb") as stdin,
        Path(str(spec["stdout_path"])).open("wb") as stdout,
        Path(str(spec["stderr_path"])).open("wb") as stderr,
    ):
        return subprocess.Popen(  # noqa: S603 - argv array from the supervisor, never a shell
            argv,
            env=env,
            cwd=str(spec["cwd"]),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
        )


def _report(line: str) -> None:
    """Tell the supervisor something; a supervisor that is gone is not an error here."""
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except OSError as error:
        sys.stderr.write(f"shim: supervisor unreachable: {error}\n")


def _stop_group(worker: subprocess.Popen[bytes], term_grace_s: float) -> int:
    """Send TERM to the group, wait for it to empty for the grace period, then KILL what is left."""
    group = os.getpgrp()
    os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + term_grace_s
    while time.monotonic() < deadline:
        worker.poll()
        if not _others_in_group(group):
            return 0
        time.sleep(POLL_S)
    os.killpg(group, signal.SIGKILL)
    return 0


def _others_in_group(group: int) -> bool:
    """Return whether any process other than this shim is still in the group."""
    listing = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [PS, "-ax", "-o", "pid=,pgid="], stdout=subprocess.PIPE, text=True
    )
    output, _ = listing.communicate()
    mine = {os.getpid(), listing.pid}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == PS_FIELDS and int(fields[1]) == group and int(fields[0]) not in mine:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main(sys.argv))
