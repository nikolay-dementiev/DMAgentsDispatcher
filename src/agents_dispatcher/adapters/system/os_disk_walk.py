"""The real disk walk: allocated bytes and inodes of a folder tree, each inode counted once."""

import os
import stat
from collections.abc import Sequence

from agents_dispatcher.application.ports import DiskUsage

BLOCK_BYTES = 512  # `st_blocks` counts 512-byte units on every platform Python supports here


class OsDiskWalk:
    """Walks with `lstat` and never follows a link, so a link out of the area adds only itself."""

    def measure(self, roots: Sequence[str]) -> DiskUsage:
        """Count what lies under `roots`; a folder that cannot be read raises `OSError`.

        A file that vanishes during the walk is skipped: the worker deleted it.
        """
        seen: set[tuple[int, int]] = set()
        allocated = 0
        for root in roots:
            pending = [root]
            while pending:
                path = pending.pop()
                try:
                    info = os.lstat(path)
                except FileNotFoundError:
                    continue
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                allocated += info.st_blocks * BLOCK_BYTES
                if stat.S_ISDIR(info.st_mode):
                    pending.extend(_children(path))
        return DiskUsage(allocated_bytes=allocated, inodes=len(seen))


def _children(folder: str) -> list[str]:
    """List a folder's entries; one that vanished or was replaced meanwhile has none."""
    try:
        with os.scandir(folder) as entries:
            return [entry.path for entry in entries]
    except (FileNotFoundError, NotADirectoryError):
        return []
