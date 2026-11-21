"""Tests of the admission floor: free space of the run area's filesystem against a setting."""

import unittest

from agents_dispatcher.application.errors import StorageFullError
from agents_dispatcher.application.storage_admission import StorageAdmission

AREA = "/state/runs"
FLOOR = 2_000_000


class FixedSpace:
    """A filesystem with a set amount of free space; it records which path was asked."""

    def __init__(self, free: int) -> None:
        """Remember the free bytes."""
        self.free = free
        self.asked: list[str] = []

    def free_bytes(self, path: str) -> int:
        """Return the free bytes and record the path."""
        self.asked.append(path)
        return self.free


class StorageAdmissionTests(unittest.TestCase):
    """When a run may be accepted."""

    def gate(self, free: int) -> tuple[StorageAdmission, FixedSpace]:
        """Build the gate over a filesystem with `free` bytes."""
        space = FixedSpace(free)
        return StorageAdmission(space, AREA, FLOOR), space

    def test_more_free_space_than_the_floor_admits(self) -> None:
        """Nothing is raised."""
        gate, space = self.gate(FLOOR + 1)
        gate.require()
        self.assertEqual(space.asked, [AREA], "the run area's filesystem was measured")

    def test_exactly_the_floor_admits(self) -> None:
        """The floor is a minimum, not an exclusive bound."""
        self.gate(FLOOR)[0].require()

    def test_less_than_the_floor_refuses_and_names_the_setting_and_the_filesystem(self) -> None:
        """The message tells the caller what to change and where the space is short."""
        gate, _space = self.gate(FLOOR - 1)
        with self.assertRaises(StorageFullError, msg="refused") as caught:
            gate.require()
        text = str(caught.exception)
        self.assertIn("storage.free_floor_bytes", text, "the setting is named")
        self.assertIn(AREA, text, "the filesystem is named by its path")
        self.assertIn(str(FLOOR - 1), text, "the free space is stated")
        self.assertIn(str(FLOOR), text, "and the floor")


if __name__ == "__main__":
    unittest.main()
