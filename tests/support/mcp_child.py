"""A real MCP server process over fake use cases, started as `python -m tests.support.mcp_child`.

The use cases misbehave on purpose: the status use case prints to standard output and writes to
descriptor 1 directly, the wait use case blocks until it is told to stop, and the start use case
waits for a second concurrent caller. The server must still speak only frames on standard output.
"""

import os
import sys
import threading
import time
from collections.abc import Callable

from agents_dispatcher.adapters.system.decision_log import FileMcpLog
from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.adapters.system.stdio_claim import ShutdownSignals, claim_stdio
from agents_dispatcher.application.run_queries import WaitResult
from agents_dispatcher.application.services import Services
from agents_dispatcher.composition.mcp import serve
from agents_dispatcher.domain.run import RunId, TaskSpec
from agents_dispatcher.domain.views import RunSummary, StatusView, StopDisposition, StopReceipt
from tests.support.cli_book import FakeReport, make_status, make_summary

CONCURRENT_START = threading.Barrier(2, timeout=10)


class NoisyStatus:
    """Writes to standard output in every way a careless use case could."""

    def status(self, run_id: RunId) -> StatusView:
        """Print, write to descriptor 1, then answer."""
        del run_id
        print("noise from a use case")  # noqa: T201 - the misbehaviour under test
        os.write(1, b"raw noise from descriptor 1\n")
        return make_status()


class BlockingWait:
    """Waits until the caller no longer wants the answer."""

    def wait(
        self, run_id: RunId, timeout_s: float | None, should_stop: Callable[[], bool] | None = None
    ) -> WaitResult:
        """Poll the cancellation probe like the real wait does."""
        del run_id, timeout_s
        while should_stop is not None and not should_stop():
            time.sleep(0.02)
        return WaitResult(ready=False, status=make_status())


class MeetingStart:
    """Answers only when two submissions are in it at the same time."""

    def start(self, spec: TaskSpec) -> RunSummary:
        """Meet the other caller at the barrier."""
        del spec
        CONCURRENT_START.wait()
        return make_summary()


class QuickStop:
    """Answers at once."""

    def stop(self, run_id: RunId, reason: str) -> StopReceipt:
        """Return an effective receipt."""
        del reason
        return StopReceipt(run_id=run_id, disposition=StopDisposition.EFFECTIVE)


def _warn(text: str) -> None:
    """Show a warning of the server on standard error."""
    sys.stderr.write(text + "\n")


def main() -> int:
    """Serve over the claimed standard streams; the test supplies settings and folders."""
    user_text = os.environ.get("MCP_TEST_SETTINGS") or None
    log_directory = os.environ["MCP_TEST_LOG_DIR"]
    settings = load_settings(
        defaults_text=shipped_defaults_text(), user_text=user_text, account_home="/home/example"
    )
    stdio = claim_stdio()
    shutdown = threading.Event()
    services = Services(
        start=MeetingStart(),
        status=NoisyStatus(),
        stop=QuickStop(),
        wait=BlockingWait(),
        report=FakeReport(),
    )
    with ShutdownSignals(shutdown):
        return serve(
            settings,
            services,
            version="0.0.0-test",
            input_fd=stdio.in_fd,
            output_fd=stdio.out_fd,
            log=FileMcpLog(log_directory, _warn),
            shutdown=shutdown,
        )


if __name__ == "__main__":
    sys.exit(main())
