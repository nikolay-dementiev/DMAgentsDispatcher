"""The login commands of the supported tools and the reading of what they print.

Both commands work with the network denied, so they need no proxy and contact no vendor. Each probe
keeps only the login state: a status document may carry an e-mail address or an organisation, and
none of that is read, stored or logged (ARCH L4252).
"""

import json

from agents_dispatcher.domain.login import LoginState

_OK = 0
_NOT_LOGGED_IN = 1
_CODEX_IN = "Logged in"
_CODEX_OUT = "Not logged in"


class CodexLoginProbe:
    """`codex login status`: exit 0 and "Logged in ...", or exit 1 and "Not logged in"."""

    def __init__(self, executable: str) -> None:
        """Keep the resolved tool."""
        self.argv = (executable, "login", "status")

    @staticmethod
    def interpret(exit_status: int | None, stdout: str) -> LoginState:
        """Read the one-line status; anything that does not fit is `UNKNOWN`."""
        line = stdout.strip()
        if exit_status == _OK and line.startswith(_CODEX_IN):
            return LoginState.LOGGED_IN
        if exit_status == _NOT_LOGGED_IN and line.startswith(_CODEX_OUT):
            return LoginState.LOGGED_OUT
        return LoginState.UNKNOWN


class ClaudeLoginProbe:
    """`claude auth status`: a JSON document whose `loggedIn` boolean is the only thing read."""

    def __init__(self, executable: str) -> None:
        """Keep the resolved tool."""
        self.argv = (executable, "auth", "status")

    @staticmethod
    def interpret(exit_status: int | None, stdout: str) -> LoginState:
        """Read `loggedIn`; a missing or non-boolean value, or a contradicting exit is `UNKNOWN`."""
        try:
            document = json.loads(stdout)
        except json.JSONDecodeError:
            return LoginState.UNKNOWN
        flag = document.get("loggedIn") if isinstance(document, dict) else None
        if flag is True and exit_status == _OK:
            return LoginState.LOGGED_IN
        if flag is False and exit_status == _NOT_LOGGED_IN:
            return LoginState.LOGGED_OUT
        return LoginState.UNKNOWN
