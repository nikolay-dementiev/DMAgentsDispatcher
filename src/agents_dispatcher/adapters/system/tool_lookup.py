"""Finding the command-line tool of a provider: the configured file, or its name on a path."""

import os
import shutil
from pathlib import Path

from agents_dispatcher.application.settings import SettingsError


def resolve_tool(setting: str, path: str, *, name: str, key: str) -> str:
    """Return the tool to run: the configured file, or `name` found on `path`.

    `key` is the settings key that fixes a failure; the message names it.
    """
    if setting == "auto":
        found = shutil.which(name, path=path)
        if found is None:
            message = (
                f"settings: {key}: no {name} executable found on {path}; "
                f"install it or set this key to its absolute path"
            )
            raise SettingsError(message)
        return found
    candidate = Path(setting)
    try:
        usable = candidate.is_file() and os.access(candidate, os.X_OK)
    except OSError:
        usable = False  # Python before 3.14 raises for a name the file system refuses (too long)
    if not usable:
        message = f"settings: {key}: {setting} is not an executable file"
        raise SettingsError(message)
    return setting
