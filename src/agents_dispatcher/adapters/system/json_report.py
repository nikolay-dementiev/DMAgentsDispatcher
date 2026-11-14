"""The one text form of a run report: what the store keeps and the commands print."""

import json

from agents_dispatcher.domain.run import RunReport


class JsonReportRenderer:
    """A `ReportRenderer` producing indented JSON."""

    def render(self, report: RunReport) -> str:
        """Return the report as indented JSON."""
        return report_text(report)


def report_text(report: RunReport) -> str:
    """Return the report as indented JSON, the same text for `run` and `report`."""
    return json.dumps(report.as_dict(), indent=2)
