"""The tool endpoint: arguments in, the same public objects the command line prints out."""

from collections.abc import Callable, Mapping

from agents_dispatcher.application.errors import NoProviderEligibleError
from agents_dispatcher.application.failures import describe
from agents_dispatcher.application.public_views import (
    recommendation_payload,
    rejection_payload,
    report_payload,
    started_payload,
    status_payload,
    stop_payload,
    wait_payload,
)
from agents_dispatcher.application.schema_check import check
from agents_dispatcher.application.services import Services
from agents_dispatcher.application.tool_outcome import ToolOutcome, error_outcome
from agents_dispatcher.application.tool_schemas import INVALID_ARGUMENT, tool_definitions
from agents_dispatcher.domain.routing import Recommendation
from agents_dispatcher.domain.run import RunId, TaskSpec

Arguments = Mapping[str, object]
Handler = Callable[[Arguments, Callable[[], bool]], ToolOutcome]


class ServicesToolEndpoint:
    """Runs the six tools over the use cases."""

    def __init__(
        self,
        *,
        services: Services,
        wait_max_s: float,
        parse_json: Callable[[str], object],
    ) -> None:
        """Keep the use cases, the wait bound and the parser of stored reports."""
        self._services = services
        self._wait_max_s = wait_max_s
        self._parse_json = parse_json
        self._tools = tool_definitions(wait_max_s)
        self._input_schemas = {str(tool["name"]): tool["inputSchema"] for tool in self._tools}
        self._handlers: dict[str, Handler] = {
            "route": self._route,
            "start": self._start,
            "status": self._status,
            "wait": self._wait,
            "stop": self._stop,
            "report": self._report,
        }

    def tools(self) -> list[dict[str, object]]:
        """Return the tool definitions."""
        return self._tools

    def call(self, name: str, arguments: object, cancelled: Callable[[], bool]) -> ToolOutcome:
        """Run one tool; every failure comes back as an error outcome."""
        handler = self._handlers.get(name)
        if handler is None:
            return error_outcome(INVALID_ARGUMENT, f"unknown tool {name!r}")
        invalid = self._invalid(name, arguments)
        if invalid is not None:
            return invalid
        try:
            return handler(arguments, cancelled)  # type: ignore[arg-type]
        except NoProviderEligibleError as error:
            failure = describe(error)
            rejection: dict[str, object] = {"rejection": rejection_payload(error.rejected)}
            return error_outcome(failure.code, failure.text, rejection)
        except ValueError as error:
            return error_outcome(INVALID_ARGUMENT, str(error))
        except Exception as error:  # noqa: BLE001 - every failure becomes an error outcome
            failure = describe(error)
            return error_outcome(failure.code, failure.text)

    def _invalid(self, name: str, arguments: object) -> ToolOutcome | None:
        """Return the error for arguments that break the tool's input schema, else None."""
        if not isinstance(arguments, dict):
            return error_outcome(INVALID_ARGUMENT, "the arguments must be a JSON object")
        violation = check(self._input_schemas[name], arguments)  # type: ignore[arg-type]
        if violation is None:
            return None
        text = violation.message
        if name == "wait" and violation.path == "timeout_s" and violation.keyword == "maximum":
            text = f"{text}; the bound is the setting mcp.wait_max_s"
        return error_outcome(INVALID_ARGUMENT, text)

    def _spec(self, arguments: Arguments, *, provider: str | None) -> TaskSpec:
        """Build the task specification of a submission."""
        return TaskSpec(
            repo=str(arguments["repo"]),
            task=str(arguments["task"]),
            hard_s=_float(arguments.get("hard_s")),
            memory_bytes=_int(arguments.get("memory_bytes")),
            task_type=_optional_text(arguments.get("task_type")),
            provider=provider,
        )

    def _submit(self, spec: TaskSpec) -> ToolOutcome:
        """Start a run, or return the recommendation."""
        result = self._services.start.start(spec)
        if isinstance(result, Recommendation):
            return ToolOutcome(recommendation_payload(result), is_error=False)
        return ToolOutcome(started_payload(result), is_error=False)

    def _route(self, arguments: Arguments, _cancelled: Callable[[], bool]) -> ToolOutcome:
        """Submit a task to the task type's chain."""
        return self._submit(self._spec(arguments, provider=None))

    def _start(self, arguments: Arguments, _cancelled: Callable[[], bool]) -> ToolOutcome:
        """Submit a task to one provider."""
        return self._submit(self._spec(arguments, provider=str(arguments["provider"])))

    def _status(self, arguments: Arguments, _cancelled: Callable[[], bool]) -> ToolOutcome:
        """Show a run."""
        view = self._services.status.status(RunId(str(arguments["run_id"])))
        return ToolOutcome(status_payload(view), is_error=False)

    def _wait(self, arguments: Arguments, cancelled: Callable[[], bool]) -> ToolOutcome:
        """Wait for a run; the caller's cancellation ends the wait early."""
        timeout = _float(arguments.get("timeout_s"))
        result = self._services.wait.wait(
            RunId(str(arguments["run_id"])),
            self._wait_max_s if timeout is None else timeout,
            cancelled,
        )
        return ToolOutcome(wait_payload(result), is_error=False)

    def _stop(self, arguments: Arguments, _cancelled: Callable[[], bool]) -> ToolOutcome:
        """Ask a run to stop."""
        receipt = self._services.stop.stop(
            RunId(str(arguments["run_id"])), str(arguments["reason"])
        )
        return ToolOutcome(stop_payload(receipt), is_error=False)

    def _report(self, arguments: Arguments, _cancelled: Callable[[], bool]) -> ToolOutcome:
        """Return the stored report."""
        result = self._services.report.report(RunId(str(arguments["run_id"])))
        return ToolOutcome(report_payload(result, self._parse_json), is_error=False)


def _float(value: object) -> float | None:
    """Return a number argument as a float, or None when absent."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def _int(value: object) -> int | None:
    """Return an integer argument, or None when absent."""
    return None if value is None else int(value)  # type: ignore[call-overload]


def _optional_text(value: object) -> str | None:
    """Return a text argument, or None when absent."""
    return None if value is None else str(value)
