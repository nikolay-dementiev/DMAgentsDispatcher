"""The text form of a command's object: for a person, not a parsing contract.

The renderer is pure. It shows every member of the object in the object's own order under one
headline chosen from the members present, and it escapes every string so that text that came
from a run cannot move the cursor. Nothing is cut and nothing is summarised.
"""

from collections.abc import Mapping

from agents_dispatcher.domain.views import escape_controls

_BLOCK_OVER = 80
_STEP = "  "


def render_text(payload: Mapping[str, object]) -> str:
    """Return the object as lines: a headline, then `key: value` for every member."""
    lines: list[str] = []
    headline = _headline(payload)
    if headline:
        lines.append(escape_controls(headline))
    members = dict(payload)
    error = members.get("error")
    if isinstance(error, dict):
        del members["error"]
        members = {**error, **members}
    for key, value in members.items():
        lines.extend(_member(key, value, 0))
    return "\n".join(lines) + "\n"


def _headline(payload: Mapping[str, object]) -> str:
    """Choose the headline from the members present, as the tool schemas' alternatives do."""
    for rule in _RULES:
        text = rule(payload)
        if text is not None:
            return text
    return ""


def _error_rule(payload: Mapping[str, object]) -> str | None:
    error = payload.get("error")
    return f"error {error.get('code')}" if isinstance(error, dict) else None


def _status_rule(payload: Mapping[str, object]) -> str | None:
    run = payload.get("run")
    if "target" in payload and isinstance(run, dict):
        return f"run {run.get('run_id')} is {run.get('state')}"
    return None


def _started_rule(payload: Mapping[str, object]) -> str | None:
    if set(payload) == {"run_id", "state"}:
        return f"run {payload['run_id']} is {payload['state']}"
    return None


def _result_rule(payload: Mapping[str, object]) -> str | None:
    if "result" in payload and "status" in payload:
        return f"result {payload['result']}"
    return None


def _stop_rule(payload: Mapping[str, object]) -> str | None:
    if "disposition" in payload:
        return f"stop of run {payload.get('run_id')}: {payload['disposition']}"
    return None


def _report_rule(payload: Mapping[str, object]) -> str | None:
    if "terminal_reason" in payload:
        return f"run {payload.get('run_id')} ended {payload['terminal_reason']}"
    return None


def _quarantine_rule(payload: Mapping[str, object]) -> str | None:
    if "outcome" in payload and "survivors" in payload:
        return f"run {payload.get('run_id')} outcome {payload['outcome']}"
    return None


def _recommendation_rule(payload: Mapping[str, object]) -> str | None:
    if payload.get("decision") == "RECOMMENDATION":
        return f"recommendation: model {payload.get('model')}, effort {payload.get('effort')}"
    return None


_RULES = (
    _error_rule,
    _status_rule,
    _started_rule,
    _result_rule,
    _stop_rule,
    _report_rule,
    _quarantine_rule,
    _recommendation_rule,
)


def _member(key: str, value: object, depth: int) -> list[str]:
    """Return the lines of one member, indented by `depth` steps."""
    pad = _STEP * depth
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{key}: (none)"]
        lines = [f"{pad}{key}:"]
        for name, item in value.items():
            lines.extend(_member(name, item, depth + 1))
        return lines
    if isinstance(value, list):
        return _list(key, value, depth)
    return _scalar(key, value, depth)


def _list(key: str, value: list[object], depth: int) -> list[str]:
    """Return a list: scalars on one line, objects as `- ` blocks, empty as `(none)`."""
    pad = _STEP * depth
    if not value:
        return [f"{pad}{key}: (none)"]
    if all(not isinstance(item, dict | list) for item in value):
        return [f"{pad}{key}: " + ", ".join(escape_controls(_plain(item)) for item in value)]
    lines = [f"{pad}{key}:"]
    for item in value:
        lines.extend(_item(item, depth + 1))
    return lines


def _item(item: object, depth: int) -> list[str]:
    """Return one list item as a block whose first line starts with `- `."""
    pad = _STEP * depth
    if isinstance(item, dict):
        inner: list[str] = []
        for name, member in item.items():
            inner.extend(_member(name, member, depth + 1))
        if not inner:
            return [f"{pad}- (none)"]
        first = inner[0][len(pad) + len(_STEP) :]
        return [f"{pad}- {first}", *inner[1:]]
    if isinstance(item, list):
        return [f"{pad}- " + ", ".join(escape_controls(_plain(part)) for part in item)]
    return [f"{pad}- {escape_controls(_plain(item))}"]


def _scalar(key: str, value: object, depth: int) -> list[str]:
    """Return a scalar: one line, or a block when the text is long or has several lines."""
    pad = _STEP * depth
    text = _plain(value)
    if isinstance(value, str) and ("\n" in text or "\r" in text or len(text) > _BLOCK_OVER):
        lines = [f"{pad}{key}:"]
        lines.extend(f"{pad}{_STEP}{escape_controls(line)}" for line in text.splitlines() or [""])
        return lines
    return [f"{pad}{key}: {escape_controls(text)}"]


def _plain(value: object) -> str:
    """Return a scalar as text: `-` for nothing, `true` and `false` for booleans."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value == "":
        return "(empty)"
    return str(value)
