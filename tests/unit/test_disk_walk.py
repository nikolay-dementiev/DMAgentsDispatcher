"""Tests of the real disk walk: allocated bytes and inodes of a run area, counted once each."""

import os
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.os_disk_walk import OsDiskWalk

BLOCK = 512


class OsDiskWalkTests(unittest.TestCase):
    """What the walk counts, and what it must not follow."""

    def setUp(self) -> None:
        """Create a private area and a folder outside it."""
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name).resolve()
        self.area = root / "area"
        self.area.mkdir()
        self.outside = root / "outside"
        self.outside.mkdir()
        self.walk = OsDiskWalk()

    def test_an_empty_folder_is_one_inode(self) -> None:
        """The root itself is counted."""
        self.assertEqual(self.walk.measure([str(self.area)]).inodes, 1, "the folder itself")

    def test_files_and_folders_are_counted_with_their_allocated_bytes(self) -> None:
        """Allocated bytes, not the length: a sparse file does not weigh its length."""
        (self.area / "sub").mkdir()
        (self.area / "sub" / "data.bin").write_bytes(bytes(100_000))
        usage = self.walk.measure([str(self.area)])
        self.assertEqual(usage.inodes, 3, "folder, sub folder and file")
        self.assertGreaterEqual(usage.allocated_bytes, 100_000, "at least what was written")

    def test_a_hard_link_is_counted_once(self) -> None:
        """Two names of one inode weigh and count as one file."""
        first = self.area / "a.bin"
        first.write_bytes(bytes(50_000))
        before = self.walk.measure([str(self.area)])
        os.link(first, self.area / "b.bin")
        after = self.walk.measure([str(self.area)])
        self.assertEqual(after, before, "the second name adds nothing")

    def test_a_symlink_to_a_big_outside_file_is_not_followed(self) -> None:
        """The link itself is an inode; the file it names is not part of the area."""
        big = self.outside / "big.bin"
        big.write_bytes(bytes(2_000_000))
        before = self.walk.measure([str(self.area)])
        (self.area / "link").symlink_to(big)
        after = self.walk.measure([str(self.area)])
        self.assertEqual(after.inodes, before.inodes + 1, "the link is one inode")
        self.assertLess(after.allocated_bytes - before.allocated_bytes, 100_000, "not the target")

    def test_a_symlink_to_a_folder_is_not_descended(self) -> None:
        """A link to a folder outside must not pull its content in."""
        (self.outside / "one.txt").write_text("x", encoding="utf-8")
        (self.area / "dir-link").symlink_to(self.outside)
        self.assertEqual(self.walk.measure([str(self.area)]).inodes, 2, "folder and the link")

    def test_two_roots_that_overlap_count_shared_inodes_once(self) -> None:
        """The sandbox holds the worktree: measuring both must not double the worktree."""
        (self.area / "worktree").mkdir()
        (self.area / "worktree" / "f.txt").write_text("x", encoding="utf-8")
        alone = self.walk.measure([str(self.area)])
        both = self.walk.measure([str(self.area), str(self.area / "worktree")])
        self.assertEqual(both, alone, "the overlap adds nothing")

    def test_a_folder_the_walk_cannot_read_is_an_error_not_a_silent_zero(self) -> None:
        """What cannot be measured must be reported, so that the run is stopped."""
        if os.geteuid() == 0:
            self.skipTest("root can read every folder")
        locked = self.area / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaises(OSError, msg="unreadable folder"):
            self.walk.measure([str(self.area)])


if __name__ == "__main__":
    unittest.main()
