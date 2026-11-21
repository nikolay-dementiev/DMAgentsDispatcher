"""The real free-space reader, over `statvfs`."""

import os
from pathlib import Path


class OsFreeSpace:
    """Free space of the filesystem that holds a path, whether or not the path exists yet."""

    def free_bytes(self, path: str) -> int:
        """Return the bytes an ordinary user can allocate, read at the nearest existing parent."""
        folder = Path(path)
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        info = os.statvfs(folder)
        return info.f_bavail * info.f_frsize
