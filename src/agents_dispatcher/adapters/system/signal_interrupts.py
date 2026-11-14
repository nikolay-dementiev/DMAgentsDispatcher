"""A scope that turns SIGINT and SIGTERM into a counting flag the wait loops poll.

The process must not die on these signals while it waits: `wait` only stops waiting and `run`
asks the run to stop and keeps waiting. The scope installs the handlers on entry and puts the
previous ones back on exit, also after an error. Handlers can only be installed from the main
thread, which is where the console script runs.
"""

import signal
from collections.abc import Callable
from contextlib import AbstractContextManager
from types import FrameType, TracebackType

HANDLED = (signal.SIGINT, signal.SIGTERM)
Handler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None


def restore_handler(number: signal.Signals, previous: Handler) -> None:
    """Put back the handler `signal.signal` returned when a scope took the signal over.

    `signal.signal` returns None for a handler that was installed from outside Python, and
    None cannot be passed back to it; the default action is the only honest stand-in.
    """
    signal.signal(number, signal.SIG_DFL if previous is None else previous)


class _Flag:
    """The interrupt flag of one scope."""

    def __init__(self) -> None:
        """Start lowered."""
        self._count = 0

    def raise_flag(self, number: int, frame: FrameType | None) -> None:
        """Signal handler: remember that an interrupt arrived."""
        del number, frame
        self._count += 1

    def raised(self) -> bool:
        """Return True once a handled signal arrived in this scope."""
        return self._count > 0

    def count(self) -> int:
        """Return how many handled signals arrived in this scope."""
        return self._count


class _Scope(AbstractContextManager[_Flag]):
    """Installs the handlers for the length of a `with` block."""

    def __init__(self) -> None:
        """Create the flag; nothing is installed yet."""
        self._flag = _Flag()
        self._previous: dict[signal.Signals, Handler] = {}

    def __enter__(self) -> _Flag:
        """Install the handlers, remembering the ones they replace."""
        for number in HANDLED:
            self._previous[number] = signal.signal(number, self._flag.raise_flag)
        return self._flag

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Put the previous handlers back."""
        for number, handler in self._previous.items():
            restore_handler(number, handler)
        self._previous.clear()


class SignalInterrupts:
    """An `InterruptScope`: call it to get a fresh scope with its own flag."""

    def __call__(self) -> AbstractContextManager[_Flag]:
        """Return a new scope."""
        return _Scope()
