"""Composition root of `agentsd mcp serve`: settings are read once, then the server runs.

The settings are read when the server starts and never again: a changed settings file takes
effect the next time the server is started by its client.
"""

import json
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

from agents_dispatcher.adapters.system.decision_log import FileMcpLog
from agents_dispatcher.adapters.system.mcp_lanes import ThreadLanes
from agents_dispatcher.adapters.system.mcp_stdio import FdInput, FdOutput
from agents_dispatcher.adapters.system.package_version import PackageMetadataVersionSource
from agents_dispatcher.adapters.system.settings_loader import account_home_dir
from agents_dispatcher.adapters.system.stdio_claim import ShutdownSignals, claim_stdio
from agents_dispatcher.application.services import Services
from agents_dispatcher.application.settings import McpSettings, Settings
from agents_dispatcher.application.tool_endpoint import ServicesToolEndpoint
from agents_dispatcher.composition.wiring import load_account_settings
from agents_dispatcher.interfaces.mcp.framing import FrameLimits
from agents_dispatcher.interfaces.mcp.ports import McpEventLog
from agents_dispatcher.interfaces.mcp.server import ServerLimits
from agents_dispatcher.interfaces.mcp.stream_transport import StreamTransport, Timings

EXIT_FAILURE = 1
EXIT_INVALID_INPUT = 2
# Room for the protocol answers (errors, pings) that are queued beside the largest tool answers.
OUTBOX_HEADROOM_BYTES = 1_048_576
POLL_S = 0.05


def serve_account(build_services: Callable[[], Services], stderr: TextIO = sys.stderr) -> int:
    """Serve the real account over standard input and output; return the exit code.

    `build_services` wires the use cases; it runs after the standard streams are claimed, so
    nothing it prints can reach the client.
    """
    settings = load_account_settings(account_home_dir())
    if settings.mcp.transport != "own":
        stderr.write(
            "agentsd: error: mcp.transport is set to "
            f"{settings.mcp.transport!r}, which needs the optional 'mcp-sdk' extra; "
            'this build serves only the own transport (mcp.transport = "own")\n'
        )
        return EXIT_INVALID_INPUT
    version = PackageMetadataVersionSource().installed_version()
    if version is None:
        stderr.write(
            "agentsd: error: the package metadata is not installed, so the server cannot "
            "report its version; reinstall the package\n"
        )
        return EXIT_FAILURE
    stdio = claim_stdio()
    shutdown = threading.Event()
    with ShutdownSignals(shutdown):
        code = serve(
            settings,
            build_services(),
            version=version,
            input_fd=stdio.in_fd,
            output_fd=stdio.out_fd,
            log=FileMcpLog(settings.logging_directory, lambda text: _show(stderr, text)),
            shutdown=shutdown,
        )
    if code != 0:
        stderr.write(
            f"agentsd: error: the MCP server ended abnormally; see {settings.logging_directory}"
            "/sdk.log for the reason\n"
        )
    return code


def _show(stderr: TextIO, text: str) -> None:
    """Show a warning of the server itself on standard error."""
    stderr.write(f"agentsd: {text}\n")


def serve(  # noqa: PLR0913 - the facts of one server: settings, use cases, descriptors, log, signal
    settings: Settings,
    services: Services,
    *,
    version: str,
    input_fd: int,
    output_fd: int,
    log: McpEventLog,
    shutdown: threading.Event,
) -> int:
    """Serve over two descriptors until the input ends or `shutdown` is set."""
    mcp = settings.mcp
    lanes = ThreadLanes(
        control_workers=mcp.control_workers,
        control_queue=mcp.control_queue,
        normal_workers=mcp.normal_workers,
        normal_queue=mcp.normal_queue,
        on_job_error=lambda error: log.record("JOB_FAILED", f"error={type(error).__name__}"),
    )
    transport = StreamTransport(
        input=FdInput(input_fd),
        output=FdOutput(output_fd),
        lanes=lanes,
        limits=_limits(mcp),
        timings=_timings(mcp),
        log=log,
        server_version=version,
        outbox_bytes=_outbox_bytes(mcp),
        priority_burst=mcp.priority_burst,
        clock=time.monotonic,
    )
    endpoint = ServicesToolEndpoint(
        services=services, wait_max_s=mcp.wait_max_s, parse_json=json.loads
    )
    return transport.serve(endpoint, shutdown)


def _limits(mcp: McpSettings) -> ServerLimits:
    """Return the bounds the server enforces."""
    return ServerLimits(
        frame=FrameLimits(
            in_bytes=mcp.frame_in_bytes,
            depth=mcp.frame_depth,
            nodes=mcp.frame_nodes,
            numeric_token_bytes=mcp.numeric_token_bytes,
        ),
        string_bytes=mcp.string_bytes,
        ids_maximum=mcp.ids_maximum,
        aggregate_bytes=mcp.aggregate_bytes,
        out_bytes=mcp.frame_out_bytes,
    )


def _timings(mcp: McpSettings) -> Timings:
    """Return the clocks of the transport loop."""
    return Timings(
        initialize_timeout_s=mcp.initialize_timeout_s,
        partial_frame_timeout_s=mcp.partial_frame_timeout_s,
        write_timeout_s=mcp.write_timeout_s,
        shutdown_drain_s=mcp.shutdown_drain_s,
        shutdown_total_s=mcp.shutdown_total_s,
        poll_s=POLL_S,
    )


def _outbox_bytes(mcp: McpSettings) -> int:
    """Return the bound of queued output: every admitted call may answer with a full frame."""
    admitted = mcp.control_workers + mcp.control_queue + mcp.normal_workers + mcp.normal_queue
    return mcp.frame_out_bytes * admitted + OUTBOX_HEADROOM_BYTES
