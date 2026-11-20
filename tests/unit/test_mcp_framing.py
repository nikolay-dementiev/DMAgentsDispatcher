"""Tests of the MCP frame splitter and the bounded frame parser."""

import time
import unittest

from agents_dispatcher.interfaces.mcp.framing import (
    INVALID_REQUEST,
    PARSE_ERROR,
    Frame,
    FrameError,
    FrameLimits,
    FrameSplitter,
    Refused,
    parse_frame,
)

IN_BYTES = 64
PARTIAL_S = 5.0
LIMITS = FrameLimits(in_bytes=IN_BYTES, depth=4, nodes=6, numeric_token_bytes=8)


def splitter() -> FrameSplitter:
    """Build a splitter with small bounds."""
    return FrameSplitter(in_bytes=IN_BYTES, partial_timeout_s=PARTIAL_S)


def code_of(data: bytes, limits: FrameLimits = LIMITS) -> int:
    """Return the JSON-RPC code `parse_frame` refuses `data` with."""
    try:
        parse_frame(data, limits)
    except FrameError as error:
        return error.code
    message = f"{data!r} was accepted"
    raise AssertionError(message)


class SplitterTests(unittest.TestCase):
    """Newline delimited frames, counted while they arrive."""

    def test_frames_in_one_chunk_and_a_frame_over_two_chunks(self) -> None:
        """The split is by newline whatever the chunking."""
        s = splitter()
        self.assertEqual(
            s.feed(b'{"a":1}\n{"b":2}\n', 0.0),
            [Frame(b'{"a":1}'), Frame(b'{"b":2}')],
            msg="two frames from one chunk",
        )
        self.assertEqual(s.feed(b'{"c"', 1.0), [], msg="a partial frame yields nothing")
        self.assertEqual(s.feed(b":3}\n", 1.1), [Frame(b'{"c":3}')], msg="completed later")

    def test_blank_lines_are_skipped(self) -> None:
        """An empty line is not a frame."""
        self.assertEqual(
            splitter().feed(b"\n\n{}\n\n", 0.0), [Frame(b"{}")], msg="only the real frame"
        )

    def test_a_frame_of_exactly_the_bound_is_accepted_and_one_more_is_refused(self) -> None:
        """The boundary: in_bytes passes, in_bytes + 1 does not."""
        exact = b"x" * IN_BYTES
        self.assertEqual(splitter().feed(exact + b"\n", 0.0), [Frame(exact)], msg="exact bound")
        events = splitter().feed(exact + b"x\n", 0.0)
        self.assertEqual(len(events), 1, msg="one refusal and no frame")
        self.assertIsInstance(events[0], Refused, msg="refused")
        assert isinstance(events[0], Refused)  # noqa: S101 - narrows the type
        self.assertEqual(events[0].error.code, INVALID_REQUEST, msg="invalid request")

    def test_an_oversize_line_is_refused_while_it_arrives_and_the_next_line_is_served(self) -> None:
        """Garbage without a newline never grows the buffer; the stream recovers at the newline."""
        s = splitter()
        refused = s.feed(b"z" * (IN_BYTES + 1), 0.0)
        self.assertEqual(len(refused), 1, msg="refused before any newline arrived")
        self.assertEqual(s.feed(b"z" * 1000, 0.1), [], msg="the rest is discarded silently")
        self.assertEqual(s.buffered(), 0, msg="nothing is kept while discarding")
        self.assertEqual(s.feed(b"zz\n{}\n", 0.2), [Frame(b"{}")], msg="served after the newline")

    def test_a_line_in_small_pieces_is_refused_when_the_pieces_add_up_over_the_bound(self) -> None:
        """Each piece fits; the line does not: refused before its newline, next line served."""
        s = splitter()
        piece = b"y" * (IN_BYTES // 2 + 1)
        self.assertEqual(s.feed(piece, 0.0), [], msg="the first piece alone fits")
        events = s.feed(piece, 0.1)
        self.assertEqual(len(events), 1, msg="refused as soon as the sum passes the bound")
        self.assertIsInstance(events[0], Refused, msg="a refusal, not a frame")
        self.assertEqual(s.buffered(), 0, msg="nothing is kept")
        self.assertEqual(s.feed(b"rest\n{}\n", 0.2), [Frame(b"{}")], msg="served after the newline")

    def test_a_stalled_partial_frame_is_dropped_like_an_oversize_one(self) -> None:
        """No byte for `partial_timeout_s`: one refusal, the rest of the line is discarded."""
        s = splitter()
        s.feed(b'{"a"', 0.0)
        self.assertEqual(s.poll(PARTIAL_S - 0.1), [], msg="not stalled yet")
        events = s.poll(PARTIAL_S)
        self.assertEqual(len(events), 1, msg="one refusal")
        self.assertEqual(s.poll(PARTIAL_S + 10), [], msg="refused once only")
        self.assertEqual(s.feed(b":1}\n{}\n", PARTIAL_S + 11), [Frame(b"{}")], msg="recovers")

    def test_bytes_that_keep_arriving_are_not_stalled(self) -> None:
        """The timer counts from the last byte, not from the first."""
        s = splitter()
        s.feed(b'{"a"', 0.0)
        s.feed(b":", 4.0)
        self.assertEqual(s.poll(8.0), [], msg="4 s after the last byte")
        self.assertEqual(s.feed(b"1}\n", 8.5), [Frame(b'{"a":1}')], msg="completed")

    def test_nothing_is_stalled_between_frames(self) -> None:
        """An idle connection with no partial frame never times out."""
        s = splitter()
        s.feed(b"{}\n", 0.0)
        self.assertEqual(s.poll(1000.0), [], msg="no partial frame, no refusal")


class ParseTests(unittest.TestCase):
    """Bounds are enforced on the bytes before the parser sees them."""

    def test_a_valid_object_is_returned(self) -> None:
        """The decoded value."""
        self.assertEqual(parse_frame(b'{"a":[1,2]}', LIMITS), {"a": [1, 2]}, msg="decoded")

    def test_invalid_utf8_and_bad_syntax_are_parse_errors(self) -> None:
        """-32700 for what is not JSON text."""
        self.assertEqual(code_of(b'{"a":"\xff"}'), PARSE_ERROR, msg="invalid UTF-8")
        self.assertEqual(code_of(b'{"a":'), PARSE_ERROR, msg="truncated")
        self.assertEqual(code_of(b'{"a":"x'), PARSE_ERROR, msg="unterminated string")

    def test_non_finite_numbers_are_parse_errors(self) -> None:
        """NaN and Infinity are not JSON."""
        self.assertEqual(code_of(b"[NaN]"), PARSE_ERROR, msg="NaN")
        self.assertEqual(code_of(b"[Infinity]"), PARSE_ERROR, msg="Infinity")
        self.assertEqual(code_of(b"[-Infinity]"), PARSE_ERROR, msg="-Infinity")

    def test_duplicate_keys_are_invalid_requests_at_any_depth(self) -> None:
        """Two readings of one frame are refused."""
        self.assertEqual(code_of(b'{"a":1,"a":2}'), INVALID_REQUEST, msg="top level")
        self.assertEqual(code_of(b'{"a":{"b":1,"b":2}}'), INVALID_REQUEST, msg="nested")

    def test_depth_at_the_bound_passes_and_one_more_is_refused(self) -> None:
        """Depth 4 is allowed, depth 5 is not."""
        self.assertEqual(parse_frame(b"[[[[1]]]]", LIMITS), [[[[1]]]], msg="depth 4 passes")
        self.assertEqual(code_of(b"[[[[[1]]]]]"), INVALID_REQUEST, msg="depth 5 refused")

    def test_the_bounds_are_checked_before_the_parser_runs(self) -> None:
        """Too deep and also not valid JSON: the bound answers, not the syntax."""
        self.assertEqual(code_of(b"[" * 40), INVALID_REQUEST, msg="deep garbage is a bound hit")

    def test_node_count_at_the_bound_passes_and_one_more_is_refused(self) -> None:
        """An array with five numbers has six nodes."""
        self.assertEqual(parse_frame(b"[1,1,1,1,1]", LIMITS), [1] * 5, msg="6 nodes pass")
        self.assertEqual(code_of(b"[1,1,1,1,1,1]"), INVALID_REQUEST, msg="7 nodes refused")

    def test_strings_count_as_nodes(self) -> None:
        """Many short strings inside the byte bound still exhaust the node bound."""
        self.assertEqual(
            parse_frame(b'["a","a","a","a","a"]', LIMITS), ["a"] * 5, msg="6 nodes pass"
        )
        self.assertEqual(code_of(b'["a","a","a","a","a","a"]'), INVALID_REQUEST, msg="7 refused")

    def test_sibling_arrays_inside_the_depth_bound_are_accepted(self) -> None:
        """A run of closers closes that many levels, so siblings do not add up to depth."""
        wide = FrameLimits(in_bytes=IN_BYTES, depth=4, nodes=50, numeric_token_bytes=8)
        data = b"[[[1]],[[1]],[[1]],[[1]]]"
        self.assertEqual(parse_frame(data, wide), [[[1]]] * 4, msg="depth 3, four siblings")

    def test_a_long_numeric_token_is_refused_at_the_bound(self) -> None:
        """8 characters pass, 9 do not."""
        self.assertEqual(parse_frame(b"[12345678]", LIMITS), [12345678], msg="8 pass")
        self.assertEqual(code_of(b"[123456789]"), INVALID_REQUEST, msg="9 refused")
        self.assertEqual(code_of(b"[1.2345e+67]"), INVALID_REQUEST, msg="exponent forms count")

    def test_brackets_and_digits_inside_strings_do_not_count(self) -> None:
        """Strings and escapes are skipped as one token."""
        text = b'{"k":"[[[[[[[[ 123456789012 \\" ]]]]"}'
        self.assertEqual(parse_frame(text, LIMITS), {"k": '[[[[[[[[ 123456789012 " ]]]]'}, msg="ok")


class ReviewFindingsTests(unittest.TestCase):
    """Defects the reviews found in the bounded parser."""

    def test_utf16_text_cannot_slip_past_the_scan(self) -> None:
        """The parser must read the same text the scanner measured, so UTF-16 is not JSON here."""
        text = '{"id":1,"n":' + "1" * 40 + "}"
        self.assertEqual(code_of(text.encode("utf-16-le")), PARSE_ERROR, msg="UTF-16 LE refused")
        self.assertEqual(code_of(text.encode("utf-32-le")), PARSE_ERROR, msg="UTF-32 LE refused")

    def test_a_frame_of_junk_is_refused_quickly(self) -> None:
        """Cost must follow the counted nodes, not the byte count."""
        big = FrameLimits(in_bytes=9_000_000, depth=32, nodes=100_000, numeric_token_bytes=128)
        for junk in (b"@", b"]", b"}", b"\\"):
            started = time.monotonic()
            with self.assertRaises(FrameError, msg=f"{junk!r} junk"):
                parse_frame(junk * 4_000_000, big)
            self.assertLess(time.monotonic() - started, 1.5, msg=f"{junk!r} scan time")

    def test_closers_that_outnumber_openers_are_not_hidden(self) -> None:
        """Depth never goes below zero, so a flood of closers cannot buy room for openers."""
        data = b"]" * 20 + b"[" * 5
        self.assertEqual(code_of(data), INVALID_REQUEST, msg="five openers exceed depth 4")


if __name__ == "__main__":
    unittest.main()
