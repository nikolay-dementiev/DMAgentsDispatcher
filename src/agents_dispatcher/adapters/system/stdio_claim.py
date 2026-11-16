"""The process edge of the MCP server: take over standard input and output, and hear signals.

Standard output carries only protocol frames. Everything else that writes to descriptor 1 (a
stray print, a library, a child process) must not reach the client, so the real descriptor is
duplicated for the server's own use and descriptor 1 is pointed at standard error. Standard input
is duplicated the same way and descriptor 0 is pointed at the null device, so no child can read
frames meant for the server.
"""

import os
import signal
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import FrameType, TracebackType

from agents_dispatcher.adapters.system.signal_interrupts import Handler, restore_handler

_STDIN = 0
_STDOUT = 1
_STDERR = 2
ENDING_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


@dataclass(frozen=True)
class ClaimedStdio:
    """The descriptors the server uses; they are not inherited by child processes."""

    in_fd: int
    out_fd: int


def claim_stdio() -> ClaimedStdio:
    """Take the client's pipes for the server and detach descriptors 0 and 1 from them.

    Call it once, before anything else can write to standard output. Descriptors made by `dup`
    are not inheritable, so a child process never holds the client's pipes.
    """
    claimed = ClaimedStdio(in_fd=os.dup(_STDIN), out_fd=os.dup(_STDOUT))
    null = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null, _STDIN)
    finally:
        os.close(null)
    os.dup2(_STDERR, _STDOUT)
    return claimed


class ShutdownSignals(AbstractContextManager[None]):
    """Turns SIGTERM, SIGINT and SIGHUP into a set event for the length of a `with` block.

    Handlers can only be installed from the main thread, which is where the console script runs.
    """

    def __init__(self, shutdown: threading.Event) -> None:
        """Keep the event; nothing is installed yet."""
        self._shutdown = shutdown
        self._previous: dict[signal.Signals, Handler] = {}

    def _on_signal(self, number: int, frame: FrameType | None) -> None:
        """Signal handler: ask the server to end."""
        del number, frame
        self._shutdown.set()

    def __enter__(self) -> None:
        """Install the handlers, remembering the ones they replace."""
        for number in ENDING_SIGNALS:
            self._previous[number] = signal.signal(number, self._on_signal)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Put the previous handlers back."""
        for number, handler in self._previous.items():
            restore_handler(number, handler)
