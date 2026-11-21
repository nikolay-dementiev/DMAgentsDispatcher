"""Tests of the real free-space reader."""

import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.os_free_space import OsFreeSpace

MIB = 1024 * 1024


class OsFreeSpaceTests(unittest.TestCase):
    """What the reader returns for a folder that exists and for one that does not yet."""

    def test_a_folder_that_exists_has_positive_free_space(self) -> None:
        """Bytes an ordinary user can still allocate."""
        with tempfile.TemporaryDirectory() as name:
            self.assertGreater(OsFreeSpace().free_bytes(name), 0, "some space is free")

    def test_a_folder_that_does_not_exist_yet_is_measured_through_its_nearest_parent(self) -> None:
        """The run area is created by the first run; the floor must still apply to it."""
        with tempfile.TemporaryDirectory() as name:
            reader = OsFreeSpace()
            there = reader.free_bytes(name)
            missing = reader.free_bytes(str(Path(name) / "not" / "yet"))
            self.assertLess(abs(there - missing), 256 * MIB, "the same filesystem")


if __name__ == "__main__":
    unittest.main()
