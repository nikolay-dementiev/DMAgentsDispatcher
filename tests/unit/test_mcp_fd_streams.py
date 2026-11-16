"""Tests of the descriptor adapters, on real pipes."""

import contextlib
import os
import threading
import time
import unittest

from agents_dispatcher.adapters.system.mcp_stdio import FdInput, FdOutput

WAIT_S = 10.0


def read_all(fd: int, size: int) -> bytes:
    """Read exactly `size` bytes from `fd`."""
    chunks: list[bytes] = []
    total = 0
    while total < size:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


class FdInputTests(unittest.TestCase):
    """Reading with a timeout."""

    def setUp(self) -> None:
        """Make a pipe."""
        self.read_fd, self.write_fd = os.pipe()

    def tearDown(self) -> None:
        """Close what is still open."""
        for fd in (self.read_fd, self.write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)

    def test_it_returns_bytes_none_on_timeout_and_empty_at_the_end(self) -> None:
        """The three answers of the port."""
        source = FdInput(self.read_fd)
        self.assertIsNone(source.read(0.01), msg="nothing yet")
        os.write(self.write_fd, b"abc")
        self.assertEqual(source.read(WAIT_S), b"abc", msg="bytes")
        os.close(self.write_fd)
        self.assertEqual(source.read(WAIT_S), b"", msg="end of input")

    def test_one_read_is_bounded_by_the_chunk_size(self) -> None:
        """A flood is taken piece by piece."""
        source = FdInput(self.read_fd, chunk_bytes=4)
        os.write(self.write_fd, b"abcdefgh")
        self.assertEqual(source.read(WAIT_S), b"abcd", msg="first chunk")
        self.assertEqual(source.read(WAIT_S), b"efgh", msg="second chunk")


class FdOutputTests(unittest.TestCase):
    """Writing with a stall timeout."""

    def setUp(self) -> None:
        """Make a pipe."""
        self.read_fd, self.write_fd = os.pipe()

    def tearDown(self) -> None:
        """Close what is still open."""
        for fd in (self.read_fd, self.write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)

    def test_a_large_write_arrives_whole_while_a_reader_drains(self) -> None:
        """More than a pipe holds."""
        data = os.urandom(300_000)
        received: list[bytes] = []
        reader = threading.Thread(target=lambda: received.append(read_all(self.read_fd, len(data))))
        reader.start()
        self.assertTrue(FdOutput(self.write_fd).write(data, WAIT_S), msg="written")
        reader.join(WAIT_S)
        self.assertEqual(received, [data], msg="all bytes, in order")

    def test_a_reader_that_never_reads_makes_the_write_fail_after_the_timeout(self) -> None:
        """A stalled pipe is a failure, not a hang."""
        started = time.monotonic()
        written = FdOutput(self.write_fd).write(b"x" * 2_000_000, 0.3)
        self.assertFalse(written, msg="reported as failed")
        self.assertLess(time.monotonic() - started, 5.0, msg="within a few timeouts")

    def test_a_closed_reader_makes_the_write_fail(self) -> None:
        """EPIPE is a failure, not an exception."""
        os.close(self.read_fd)
        self.assertFalse(FdOutput(self.write_fd).write(b"hello\n", 1.0), msg="broken pipe")


if __name__ == "__main__":
    unittest.main()
