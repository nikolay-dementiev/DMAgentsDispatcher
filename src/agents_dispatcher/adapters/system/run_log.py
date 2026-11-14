"""The supervisor log of one run: a private, append-only text file (PLAN-5 section 3.2)."""

import os
import time
from pathlib import Path

FOLDER_MODE = 0o700
FILE_MODE = 0o600


class RunLog:
    """Appends timestamped lines to `<directory>/<run id>.log`, created for the owner only."""

    def __init__(self, directory: str, run_id: str) -> None:
        """Create the folder and the file; every line is flushed to disk at once."""
        folder = Path(directory)
        folder.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
        folder.chmod(FOLDER_MODE)
        self._descriptor = os.open(
            folder / f"{run_id}.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND, FILE_MODE
        )

    def write(self, line: str) -> None:
        """Append one line; a failure to write raises, it is never swallowed."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        os.write(self._descriptor, f"{stamp} {line}\n".encode())
        os.fsync(self._descriptor)

    def close(self) -> None:
        """Close the file."""
        os.close(self._descriptor)
