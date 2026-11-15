"""The Codex account dialogue: identity, login and quota read from the tool's own app-server.

The tool answers over standard streams with JSON lines. The requests go out in one batch and stdin
stays open for a hold afterwards: a server that sees the end of its input at once stops after its
first reply (measured offline). Account and rate-limit reads are asked three and two times; the
replies must agree, and a disagreement discards the exchange. Anything off the measured shape is
unusable, never a guess.
"""

import json
import math

from agents_dispatcher.domain.account import AccountReading
from agents_dispatcher.domain.login import LoginState
from agents_dispatcher.domain.quota import EvidenceKind, QuotaEvidence, QuotaWindow

HOLD_SECONDS = 10
MAX_NOTIFICATIONS = 50
BOUND_BUCKET = "codex"
_ACCOUNT_IDS = (2, 4, 6)
_LIMIT_IDS = (3, 5)
_CLIENT = {"name": "agentsd", "version": "0"}
_REQUESTS = (
    {"id": 1, "method": "initialize", "params": {"clientInfo": _CLIENT}},
    {"method": "initialized"},
    {"id": 2, "method": "account/read", "params": {}},
    {"id": 3, "method": "account/rateLimits/read"},
    {"id": 4, "method": "account/read", "params": {}},
    {"id": 5, "method": "account/rateLimits/read"},
    {"id": 6, "method": "account/read", "params": {}},
)
_SCRIPT = f'{{ printf \'%s\\n\' "$2"; sleep {HOLD_SECONDS}; }} | "$1" app-server --listen stdio://'
_PERCENT_MAX = 100


def _unusable(detail: str) -> AccountReading:
    """Return the reading of an exchange that cannot be trusted."""
    return AccountReading(
        LoginState.UNKNOWN, None, QuotaEvidence(EvidenceKind.UNAVAILABLE, detail=detail)
    )


class CodexAccountDialogue:
    """The command that asks the app-server and the reading of its answers."""

    def __init__(self, executable: str) -> None:
        """Keep the path of the tool."""
        self._executable = executable

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the pipeline: the requests, the hold, then the server; the tool is an argument."""
        requests = "\n".join(json.dumps(request) for request in _REQUESTS)
        return ("/bin/sh", "-c", _SCRIPT, "sh", self._executable, requests)

    def interpret(self, exit_status: int | None, stdout: str) -> AccountReading:
        """Read the server's output; an exchange that is not whole is unusable."""
        if exit_status != 0:
            return _unusable(f"the account server exited with status {exit_status}")
        replies = _replies(stdout)
        if isinstance(replies, str):
            return _unusable(replies)
        return _reading(replies, _login(replies))


def _replies(stdout: str) -> dict[int, dict[str, object]] | str:
    """Return the replies by id, or the reason the output is unusable."""
    replies: dict[int, dict[str, object]] = {}
    notifications = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return "the account server printed a line that is not JSON"
        if not isinstance(value, dict):
            return "the account server printed something that is not an object"
        reply_id = value.get("id")
        if reply_id is None:
            notifications += 1
            if notifications > MAX_NOTIFICATIONS:
                return "the account server sent too many unsolicited messages"
        elif isinstance(reply_id, int) and not isinstance(reply_id, bool):
            replies[reply_id] = value
        else:
            return "the account server answered with an id that is not a number"
    return replies


def _result(replies: dict[int, dict[str, object]], reply_id: int) -> dict[str, object] | None:
    """Return the result object of one reply, or None when it is absent or an error."""
    result = replies.get(reply_id, {}).get("result")
    return result if isinstance(result, dict) else None


def _login(replies: dict[int, dict[str, object]]) -> LoginState:
    """Read the login state from the account replies; they must all agree."""
    states: set[LoginState] = set()
    for reply_id in _ACCOUNT_IDS:
        result = _result(replies, reply_id)
        if result is None or "account" not in result:
            return LoginState.UNKNOWN
        account = result["account"]
        if account is None:
            states.add(LoginState.LOGGED_OUT)
        elif isinstance(account, dict):
            states.add(LoginState.LOGGED_IN)
        else:
            return LoginState.UNKNOWN
    return states.pop() if len(states) == 1 else LoginState.UNKNOWN


def _reading(replies: dict[int, dict[str, object]], login: LoginState) -> AccountReading:
    """Combine the login with the rate-limit replies."""
    for reply_id in _LIMIT_IDS:
        if "error" in replies.get(reply_id, {}):
            detail = _error_text(replies[reply_id])
            return AccountReading(
                login, None, QuotaEvidence(EvidenceKind.UNAVAILABLE, detail=detail)
            )
    results = [_result(replies, reply_id) for reply_id in _LIMIT_IDS]
    answered = [result for result in results if result is not None]
    if len(answered) != len(_LIMIT_IDS):
        detail = "the rate limits were not answered"
        return AccountReading(login, None, QuotaEvidence(EvidenceKind.UNAVAILABLE, detail=detail))
    identities = {_identity(result) for result in answered}
    if len(identities) > 1:
        detail = "the reads name different accounts"
        return AccountReading(login, None, QuotaEvidence(EvidenceKind.CONFLICT, detail=detail))
    return AccountReading(login, identities.pop(), _quota(answered[-1]))


def _error_text(reply: dict[str, object]) -> str:
    """Return the message of an error reply."""
    error = reply.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else "the rate limits could not be read"


def _identity(result: dict[str, object]) -> str | None:
    """Return the account id of a rate-limit result, or None when it names none."""
    account_id = result.get("accountId")
    return account_id if isinstance(account_id, str) and account_id else None


def _quota(result: dict[str, object]) -> QuotaEvidence:
    """Read the windows of the bound bucket; a missing or unsound bucket is unavailable."""
    buckets = result.get("rateLimitsByLimitId")
    bucket = buckets.get(BOUND_BUCKET) if isinstance(buckets, dict) else None
    if not isinstance(bucket, dict):
        detail = f"no rate limits for the {BOUND_BUCKET} bucket"
        return QuotaEvidence(EvidenceKind.UNAVAILABLE, detail=detail)
    windows: list[QuotaWindow] = []
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if window is None:
            continue
        parsed = _window(window)
        if parsed is None:
            return QuotaEvidence(EvidenceKind.UNAVAILABLE, detail=f"the {name} window is unsound")
        windows.append(parsed)
    if not windows:
        return QuotaEvidence(EvidenceKind.UNAVAILABLE, detail="the bucket has no windows")
    return QuotaEvidence(EvidenceKind.SNAPSHOT, tuple(windows), age_s=0.0)


def _window(value: object) -> QuotaWindow | None:
    """Read one window; None when a number is missing, not finite or out of range."""
    if not isinstance(value, dict):
        return None
    minutes = value.get("windowDurationMins")
    used = value.get("usedPercent")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        return None
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    if not math.isfinite(used) or not 0 <= used <= _PERCENT_MAX:
        return None
    return QuotaWindow(minutes, float(used))
