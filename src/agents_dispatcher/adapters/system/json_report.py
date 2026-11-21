"""The one text form of a run report: what the store keeps and the commands print."""

import json
from collections.abc import Sequence

from agents_dispatcher.domain.run import RunReport, RunState


class JsonReportRenderer:
    """A `ReportRenderer` producing indented JSON."""

    def render(self, report: RunReport) -> str:
        """Return the report as indented JSON."""
        return report_text(report)


def report_text(report: RunReport) -> str:
    """Return the report as indented JSON, the same text for `run` and `report`."""
    return json.dumps(report.as_dict(), indent=2)


STATE_TERMINAL = RunState.TERMINAL.name
OPEN_NOTES = ("CLEANUP_UNCERTAIN", "result not sealed")


class JsonReportAmender:
    """A `ReportAmender` over the JSON text the store keeps."""

    def amend(
        self,
        report_json: str,
        *,
        branch: str | None,
        commits: int,
        notes: Sequence[str],
        resolved: bool,
    ) -> str:
        """Return the report as it reads once a quarantine ended; raise `ValueError` if damaged.

        A resolved run no longer holds the notes that were true while it was held back, and its
        problem is closed. A released run proves nothing, so its problem and notes stay.
        """
        shown = json.loads(report_json)
        if not isinstance(shown, dict):
            message = "the kept report is not a JSON object"
            raise TypeError(message)
        kept = [str(item) for item in shown.get("notes", [])]
        if resolved:
            kept = [item for item in kept if not item.startswith(OPEN_NOTES)]
            shown["problem"] = None
        shown["state"] = STATE_TERMINAL
        shown["branch"] = branch
        shown["commits_on_branch"] = commits
        shown["notes"] = [*kept, *notes]
        return json.dumps(shown, indent=2)
