"""Signals to processes named by pid and start time (O1 layer 3).

The identity is read immediately before every signal, so a pid that was reused is never hit.
Pid 0 (our own group) and pid 1 (launchd) are refused before anything is read.
"""

import os
import signal
from collections.abc import Callable
from typing import Protocol

from agents_dispatcher.application.ports import SignalResult
from agents_dispatcher.domain.process import Absence, ProcessBasics, ProcessKey

SYSTEM_PID_MAX = 1
START_TIME_TOLERANCE_S = 0.001


class SignalTable(Protocol):
    """The one read the signaller needs."""

    def basics(self, pid: int) -> ProcessBasics | Absence:
        """Return the facts of the process, or why they are unreadable."""
        ...


class OsSignaller:
    """A `Signaller` over `os.kill` that checks the identity first."""

    def __init__(self, table: SignalTable, send: Callable[[int, int], None] = os.kill) -> None:
        """Keep the table reader and the function that delivers a signal."""
        self._table = table
        self._send_signal = send

    def stop(self, key: ProcessKey) -> SignalResult:
        """Pause the process if it is still the one named."""
        return self._send(key, signal.SIGSTOP)

    def kill(self, key: ProcessKey) -> SignalResult:
        """End the process if it is still the one named."""
        return self._send(key, signal.SIGKILL)

    def _send(self, key: ProcessKey, number: signal.Signals) -> SignalResult:
        """Revalidate the identity, then send; macOS says EPERM for a process that is exiting."""
        refused = self._refusal(key)
        if refused is not None:
            return refused
        try:
            self._send_signal(key.pid, number)
        except ProcessLookupError:
            return SignalResult.GONE
        except PermissionError:
            return SignalResult.DENIED
        return SignalResult.SENT

    def _refusal(self, key: ProcessKey) -> SignalResult | None:
        """Return why nothing may be sent to this identity, or None when it may."""
        if key.pid <= SYSTEM_PID_MAX:
            return SignalResult.DENIED
        basics = self._table.basics(key.pid)
        if isinstance(basics, Absence):
            return SignalResult.GONE if basics is Absence.GONE else SignalResult.DENIED
        if abs(basics.start_time - key.start_time) > START_TIME_TOLERANCE_S:
            return SignalResult.CHANGED
        return None
