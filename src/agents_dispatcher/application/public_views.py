"""The payloads a front end shows for each operation: one source for the CLI and the MCP tools."""

from collections.abc import Callable

from agents_dispatcher.application.run_queries import ReportResult, WaitResult
from agents_dispatcher.domain.routing import Recommendation, Rejected
from agents_dispatcher.domain.views import RunSummary, StatusView, StopReceipt

READY = "READY"
PENDING = "PENDING"


def started_payload(summary: RunSummary) -> dict[str, object]:
    """Return what `start` and `route` show for an accepted run."""
    return {"run_id": summary.run_id.value, "state": summary.state.name}


def recommendation_payload(recommendation: Recommendation) -> dict[str, object]:
    """Return what a submission shows when every provider refused and the chain hands off."""
    return recommendation.as_dict()


def rejection_payload(rejected: Rejected) -> dict[str, object]:
    """Return what a submission shows when no provider could take the task."""
    return rejected.as_dict()


def status_payload(view: StatusView) -> dict[str, object]:
    """Return what `status` shows."""
    return view.as_dict()


def stop_payload(receipt: StopReceipt) -> dict[str, object]:
    """Return what `stop` shows."""
    return receipt.as_dict()


def wait_payload(result: WaitResult) -> dict[str, object]:
    """Return what `wait` shows: READY when the run ended, else PENDING, with the status."""
    return {"result": READY if result.ready else PENDING, "status": result.status.as_dict()}


def pending_payload(status: StatusView | None) -> dict[str, object]:
    """Return what `report` shows for a run that has not ended."""
    return {"result": PENDING, "status": status.as_dict() if status else None}


def report_payload(result: ReportResult, parse_json: Callable[[str], object]) -> dict[str, object]:
    """Return the stored report object of a finished run, or the pending payload.

    The inner layers read no JSON text themselves; the caller passes its parser.
    """
    if result.report_json is None:
        return pending_payload(result.pending)
    stored = parse_json(result.report_json)
    if not isinstance(stored, dict):
        message = "the stored report is not a JSON object"
        raise TypeError(message)
    return stored
