"""The real console entry point, `agentsd <command>`, run against a temporary account.

Only the lookup of the account's home directory is replaced, so the settings, the run store and
the workers of the test live under a temporary folder; everything else is the shipped composition
root, started as `python -m tests.support.cli_real_child <command> ...`.

Two refusals keep a test from reaching the real account: the home must lie under the system
temporary folder, and the lookup must be replaced in every composition module that reads it.
"""

import importlib
import os
import pkgutil
import sys
import tempfile
from pathlib import Path

from agents_dispatcher import composition

HOME_ENV = "CLI_REAL_HOME"
LOOKUP = "account_home_dir"
EXIT_REFUSED = 70


def _refuse_home(home: str) -> str | None:
    """Return why `home` is not a temporary folder, or None when it is one."""
    temporary = Path(tempfile.gettempdir()).resolve()
    resolved = Path(home).resolve()
    if resolved == temporary or temporary not in resolved.parents:
        return f"{HOME_ENV} must be a folder under {temporary}, got {home!r}"
    if not resolved.is_dir():
        return f"{HOME_ENV} must be an existing folder, got {home!r}"
    return None


def readers() -> list[str]:
    """Return the composition modules that read the account's home directory."""
    found: list[str] = []
    for info in pkgutil.iter_modules(composition.__path__):
        module = importlib.import_module(f"{composition.__name__}.{info.name}")
        if hasattr(module, LOOKUP):
            found.append(module.__name__)
    return sorted(found)


def patch_account_home(home: str) -> list[str]:
    """Point the lookup of every composition module at `home`; return the modules changed."""
    names = readers()
    for name in names:
        setattr(sys.modules[name], LOOKUP, lambda: home)
    unchanged = [name for name in names if getattr(sys.modules[name], LOOKUP)() != home]
    if not names or unchanged:
        message = f"the account lookup was not replaced in: {unchanged or 'any module'}"
        raise RuntimeError(message)
    return names


def main() -> int:
    """Run the command of the arguments against the temporary account named by the environment."""
    home = os.environ.get(HOME_ENV, "")
    reason = _refuse_home(home) if home else f"{HOME_ENV} is not set"
    if reason is not None:
        sys.stderr.write(f"cli_real_child: {reason}\n")
        return EXIT_REFUSED
    patch_account_home(home)
    from agents_dispatcher.composition import cli  # noqa: PLC0415 - after the lookup is replaced

    sys.argv = ["agentsd", *sys.argv[1:]]
    return cli.main()


if __name__ == "__main__":
    sys.exit(main())
