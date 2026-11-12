"""A temporary home and workspace that never touch the real ones."""

import os
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Self


class TemporaryHome:
    """A throw-away home directory and workspace, removed when the context ends."""

    def __init__(self) -> None:
        """Create the directories; they exist until the context exits."""
        self._root = tempfile.TemporaryDirectory(prefix="agentsd-test-")
        root = Path(self._root.name)
        self.home = root / "home"
        self.workspace = root / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()

    @property
    def environment(self) -> dict[str, str]:
        """The environment for child processes; the real process environment is never patched."""
        return {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def __enter__(self) -> Self:
        """Enter the context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Remove both directories."""
        self._root.cleanup()
