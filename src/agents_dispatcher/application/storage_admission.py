"""The admission floor: no run is accepted while the run area's filesystem is nearly full."""

from agents_dispatcher.application.errors import StorageFullError
from agents_dispatcher.application.ports import FreeSpace

FLOOR_SETTING = "storage.free_floor_bytes"


class StorageAdmission:
    """Compares the free space of the run area's filesystem with the configured floor."""

    def __init__(self, space: FreeSpace, run_area: str, floor_bytes: int) -> None:
        """Keep the reader, the folder that stands for the filesystem and the floor."""
        self._space = space
        self._run_area = run_area
        self._floor_bytes = floor_bytes

    def require(self) -> None:
        """Raise `StorageFullError` naming the setting and the filesystem when space is short."""
        free = self._space.free_bytes(self._run_area)
        if free < self._floor_bytes:
            message = (
                f"the filesystem of {self._run_area} has {free} bytes free, below the floor of "
                f"{self._floor_bytes} bytes set by {FLOOR_SETTING}; free some space or lower "
                f"the setting"
            )
            raise StorageFullError(message)
