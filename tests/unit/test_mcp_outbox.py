"""Tests of the outbox: priority frames first, with a burst bound so replies are not starved."""

import threading
import unittest

from agents_dispatcher.interfaces.mcp.outbox import Outbox


def drain(outbox: Outbox) -> list[bytes]:
    """Take everything that is ready, without waiting."""
    items: list[bytes] = []
    while (item := outbox.get(timeout_s=0.0)) is not None:
        items.append(item)
    return items


class OutboxTests(unittest.TestCase):
    """Order, bound and closing."""

    def test_frames_of_one_kind_keep_their_order(self) -> None:
        """First in, first out within a class."""
        outbox = Outbox(max_bytes=100, priority_burst=8)
        for text in (b"a", b"b", b"c"):
            outbox.put(text, priority=False)
        self.assertEqual(drain(outbox), [b"a", b"b", b"c"], msg="fifo")

    def test_priority_frames_go_first(self) -> None:
        """A protocol or control answer overtakes queued tool results."""
        outbox = Outbox(max_bytes=100, priority_burst=8)
        outbox.put(b"n1", priority=False)
        outbox.put(b"p1", priority=True)
        outbox.put(b"n2", priority=False)
        self.assertEqual(drain(outbox), [b"p1", b"n1", b"n2"], msg="priority first")

    def test_a_burst_of_priority_frames_lets_one_normal_frame_through(self) -> None:
        """After `priority_burst` priority frames in a row, a waiting normal frame is served."""
        outbox = Outbox(max_bytes=1000, priority_burst=2)
        outbox.put(b"n", priority=False)
        for index in range(4):
            outbox.put(f"p{index}".encode(), priority=True)
        self.assertEqual(
            drain(outbox), [b"p0", b"p1", b"n", b"p2", b"p3"], msg="normal not starved"
        )

    def test_the_byte_bound_refuses_more(self) -> None:
        """A client that never reads cannot make the outbox grow without limit."""
        outbox = Outbox(max_bytes=5, priority_burst=8)
        self.assertTrue(outbox.put(b"abc", priority=False), msg="fits")
        self.assertFalse(outbox.put(b"def", priority=True), msg="over the bound")
        self.assertEqual(drain(outbox), [b"abc"], msg="the refused frame was not kept")
        self.assertTrue(outbox.put(b"def", priority=True), msg="room after draining")

    def test_get_waits_for_a_frame_put_from_another_thread(self) -> None:
        """The writer blocks until there is something to write."""
        outbox = Outbox(max_bytes=100, priority_burst=8)
        got: list[bytes | None] = []
        reader = threading.Thread(target=lambda: got.append(outbox.get(timeout_s=10.0)))
        reader.start()
        outbox.put(b"x", priority=False)
        reader.join(timeout=10)
        self.assertEqual(got, [b"x"], msg="woken by put")

    def test_get_times_out_with_none(self) -> None:
        """Nothing to write."""
        self.assertIsNone(Outbox(max_bytes=10, priority_burst=8).get(timeout_s=0.01), msg="empty")

    def test_closing_wakes_a_waiting_reader_and_refuses_new_frames(self) -> None:
        """After close, remaining frames can still be taken, then get returns None at once."""
        outbox = Outbox(max_bytes=100, priority_burst=8)
        outbox.put(b"last", priority=False)
        outbox.close()
        self.assertFalse(outbox.put(b"late", priority=False), msg="closed refuses")
        self.assertEqual(outbox.get(timeout_s=5.0), b"last", msg="remaining frame still served")
        self.assertIsNone(outbox.get(timeout_s=5.0), msg="then None without waiting")


if __name__ == "__main__":
    unittest.main()
